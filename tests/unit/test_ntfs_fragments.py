"""Tests for NTFS read-back unit consistency.

Before the fix, ``NtfsParser.get_metadata_blocks`` emitted raw NTFS *cluster* numbers
while ``get_allocated_fragments_for_file`` emitted *512-byte block* indices, so a single
stratum mixed two address spaces (8x apart at the 4 KiB default cluster) that could not
be overlaid. These tests pin the fix: both now use inclusive 512-byte block ranges.

They run on any platform because parsing is detached from any mount/OS: the existing
NTFS ``.vhd`` fixture is read directly through a ``FileSystem`` handle at the partition
offset, exactly as ``Volume.get_filesystem`` does at run time.
"""

import logging
from pathlib import Path

import pytest

from fsstratify.filesystems import (
    NtfsParser,
    _byte_range_to_block_range,
    _clusters_to_block_range,
    _to_utc_iso,
    get_file_system_parser,
)
from fsstratify.volumes import FileSystem

FSSTRATIFY_BLOCK = 512
# The fixture vhd is a small NTFS volume aligned at the Windows default 64 KiB offset
# (WindowsRawDiskImage uses 64 KiB for images <= 4 GiB).
_NTFS_FS_OFFSET = 64 * 1024


class _ImageVolume:
    """Minimal ``Volume`` stand-in exposing the fixture image at its partition offset.

    Mirrors ``Volume.get_filesystem`` (``volumes.py``): a fresh, offset-aware file
    handle per call, which is what the parser consumes inside its ``with`` block.
    """

    def __init__(self, path: Path, fs_offset: int = _NTFS_FS_OFFSET):
        self.path = Path(path)
        self._fs_offset = fs_offset

    def get_filesystem(self) -> FileSystem:
        return FileSystem(self.path, self._fs_offset)


@pytest.fixture
def ntfs_image(test_data_path):
    path = test_data_path / "test_windows_ntfs.vhd"
    # Block count of the file-system region (after the partition offset).
    fs_blocks = (path.stat().st_size - _NTFS_FS_OFFSET) // FSSTRATIFY_BLOCK
    return path, fs_blocks


@pytest.fixture
def parser(ntfs_image):
    path, _ = ntfs_image
    return get_file_system_parser("ntfs", _ImageVolume(path))


def _read_blocks(path: Path, ranges) -> bytes:
    """Concatenate the bytes of the given inclusive 512-byte block ranges."""
    out = bytearray()
    with FileSystem(path, _NTFS_FS_OFFSET) as fh:
        for first, last in ranges:
            fh.seek(first * FSSTRATIFY_BLOCK)
            out += fh.read((last - first + 1) * FSSTRATIFY_BLOCK)
    return bytes(out)


def _regular_files(parser):
    from fsstratify.filesystems import FileType

    return [f for f in parser.get_files() if f.type == FileType.REGULAR]


def _resident_regular_files(parser, path):
    """Return the regular files whose data is resident in their MFT record."""
    from dissect.ntfs import NTFS

    out = []
    with FileSystem(path, _NTFS_FS_OFFSET) as fh:
        ntfs = NTFS(fh)
        for f in _regular_files(parser):
            if ntfs.mft.get(str(f.path)).resident:
                out.append(f)
    return out


# --------------------------------------------------------------------------- #
# Pure helper: the single unit/interval convention (no image needed)
# --------------------------------------------------------------------------- #


def test_clusters_to_block_range_is_inclusive_512_blocks():
    # 2 clusters * 4096 bytes = 8192 bytes = 16 blocks, inclusive => (80, 95).
    assert _clusters_to_block_range(10, 2, 4096) == (80, 95)
    # A single 512-byte cluster maps to exactly one block.
    assert _clusters_to_block_range(3, 1, 512) == (3, 3)


def test_clusters_to_block_range_matches_byte_helper():
    # The cluster helper must be exactly the byte helper applied to the run extent.
    assert _clusters_to_block_range(7, 5, 4096) == _byte_range_to_block_range(
        7 * 4096, 5 * 4096
    )


def test_dispatch_returns_ntfs_parser():
    assert isinstance(get_file_system_parser("ntfs", _ImageVolume(Path("x"))), NtfsParser)


# --------------------------------------------------------------------------- #
# Read-back against the real NTFS fixture image
# --------------------------------------------------------------------------- #


def test_allocated_fragments_are_inclusive_and_in_bounds(parser, ntfs_image):
    _, fs_blocks = ntfs_image
    for f in _regular_files(parser):
        for first, last in parser.get_allocated_fragments_for_file(f.path):
            assert 0 <= first <= last < fs_blocks


def test_metadata_blocks_are_inclusive_and_in_bounds(parser, ntfs_image):
    _, fs_blocks = ntfs_image
    md = parser.get_metadata_blocks()
    assert md, "no metadata blocks reported"
    for first, last in md:
        assert 0 <= first <= last < fs_blocks


# --------------------------------------------------------------------------- #
# A FileNotFoundError raised while reading a metadata attribute's data runs
# must be surfaced as a warning, not silently swallowed (which would leave
# fs_areas quietly incomplete).
# --------------------------------------------------------------------------- #


_INCOMPLETE_MSG = "fs_areas may be incomplete"


def test_unreadable_metadata_runs_emit_warning(parser, monkeypatch, caplog):
    """Pre-fix this path ``pass``ed silently; now it logs a WARNING and degrades.

    The genuine error is hard to provoke against a healthy fixture, so force every
    metadata attribute's ``dataruns()`` to raise the dissect ``FileNotFoundError``.
    ``get_metadata_blocks`` must not crash, must return a list, and must warn.
    """
    import dissect.ntfs.exceptions
    from dissect.ntfs.attr import Attribute

    def _raise(*_args, **_kwargs):
        raise dissect.ntfs.exceptions.FileNotFoundError("forced for test")

    monkeypatch.setattr(Attribute, "dataruns", _raise)

    with caplog.at_level(logging.WARNING, logger="fsstratify.filesystems"):
        result = parser.get_metadata_blocks()

    assert isinstance(result, list)
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and _INCOMPLETE_MSG in r.getMessage()
    ]
    assert warnings, "expected a warning when metadata data runs are unreadable"


def test_healthy_metadata_runs_emit_no_warning(parser, caplog):
    """A normal read produces metadata blocks and no incomplete-fs_areas warning."""
    with caplog.at_level(logging.WARNING, logger="fsstratify.filesystems"):
        result = parser.get_metadata_blocks()

    assert result, "fixture should yield metadata blocks"
    assert not [
        r for r in caplog.records if _INCOMPLETE_MSG in r.getMessage()
    ]


def test_metadata_and_allocated_areas_share_one_address_space(parser, ntfs_image):
    """fs_areas and allocated_areas live in the same 512-byte block space.

    Pre-fix, metadata used cluster numbers (~8x smaller), so its max block fell far
    below the file-data blocks. Post-fix both reach into the same high-block region.
    """
    _, fs_blocks = ntfs_image
    md = parser.get_metadata_blocks()
    file_blocks = [
        last
        for f in _regular_files(parser)
        for _, last in parser.get_allocated_fragments_for_file(f.path)
    ]
    assert file_blocks, "fixture has no allocated regular files"
    md_max = max(last for _, last in md)
    # If metadata were still in cluster units it would top out around fs_blocks/8;
    # in 512-byte blocks it reaches the same magnitude as the file data.
    assert md_max > fs_blocks // 4
    assert md_max >= max(file_blocks) // 2


def test_metadata_blocks_point_at_real_mft_records(parser, ntfs_image):
    """Reading the reported metadata ranges yields NTFS ``FILE`` records.

    This fails on the pre-fix cluster-unit output (the byte offsets land elsewhere and
    only a handful of incidental ``FILE`` byte patterns appear).
    """
    path, _ = ntfs_image
    data = _read_blocks(path, parser.get_metadata_blocks())
    assert data.count(b"FILE") >= 16


# --------------------------------------------------------------------------- #
# Resident files use the same inclusive 512-byte-block convention as
# non-resident files (pre-fix the resident branch used an exclusive-style end).
# --------------------------------------------------------------------------- #


def test_resident_fragments_are_inclusive_two_block_ranges(parser, ntfs_image):
    """A resident file's range spans exactly MFT_RECORD_SIZE/512 blocks, inclusive.

    Pre-fix the end was ``start + MFT_RECORD_SIZE/512`` (exclusive style), so the span
    was reported as 3 blocks instead of 2; this asserts the inclusive 2-block span.
    """
    path, _ = ntfs_image
    expected_span = NtfsParser.MFT_RECORD_SIZE // FSSTRATIFY_BLOCK
    resident = _resident_regular_files(parser, path)
    assert resident, "fixture has no resident regular files"
    for f in resident:
        fragments = parser.get_allocated_fragments_for_file(f.path)
        assert len(fragments) == 1
        first, last = fragments[0]
        assert last - first + 1 == expected_span


def test_resident_files_do_not_overlap_neighbours(parser, ntfs_image):
    """Distinct resident files occupy disjoint block ranges.

    Pre-fix the exclusive-style end made adjacent resident files share a block at the
    seam (e.g. (13006, 13008) and (13008, 13010) both claim 13008). With the inclusive
    convention the ranges are disjoint.
    """
    path, _ = ntfs_image
    ranges = []
    for f in _resident_regular_files(parser, path):
        ranges.extend(parser.get_allocated_fragments_for_file(f.path))
    ranges.sort()
    for (_, prev_last), (cur_first, _) in zip(ranges, ranges[1:]):
        assert cur_first > prev_last


def test_resident_range_contains_file_content(parser, ntfs_image):
    """Bytes at a resident file's reported range include the file's actual content.

    Confirms the inclusive end still covers the resident data (the fix did not truncate
    the range): resident data lives inside the MFT record spanned by the range.
    """
    from dissect.ntfs import NTFS

    path, _ = ntfs_image
    resident = _resident_regular_files(parser, path)
    target = next(f for f in resident if parser.get_size_of(f.path) > 0)

    region = _read_blocks(path, parser.get_allocated_fragments_for_file(target.path))
    with FileSystem(path, _NTFS_FS_OFFSET) as fh:
        content = NTFS(fh).mft.get(str(target.path)).open().read()
    assert content in region


def test_allocated_areas_reconstruct_file_content(parser, ntfs_image):
    """Bytes at a multi-block file's reported blocks equal the file's actual data."""
    from dissect.ntfs import NTFS

    path, _ = ntfs_image
    target = max(
        _regular_files(parser), key=lambda f: parser.get_size_of(f.path)
    )
    fragments = parser.get_allocated_fragments_for_file(target.path)
    assert fragments, "expected a non-resident multi-block file in the fixture"

    reconstructed = _read_blocks(path, fragments)
    with FileSystem(path, _NTFS_FS_OFFSET) as fh:
        actual = NTFS(fh).mft.get(str(target.path)).open().read()
    assert reconstructed[: len(actual)] == actual


# --------------------------------------------------------------------------- #
# Timestamps are emitted as timezone-unambiguous UTC ISO-8601
# --------------------------------------------------------------------------- #


def test_to_utc_iso_treats_naive_as_utc():
    """A naive datetime is interpreted as UTC and gets an explicit offset."""
    from datetime import datetime, timedelta

    out = _to_utc_iso(datetime(2023, 1, 28, 19, 56, 44))
    parsed = datetime.fromisoformat(out)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    assert out.endswith("+00:00")


def test_to_utc_iso_converts_aware_to_utc():
    """An aware non-UTC datetime is normalized to UTC, same instant."""
    from datetime import datetime, timedelta, timezone

    aware = datetime(2023, 1, 28, 21, 56, 44, tzinfo=timezone(timedelta(hours=2)))
    parsed = datetime.fromisoformat(_to_utc_iso(aware))
    assert parsed.utcoffset() == timedelta(0)
    assert parsed == aware  # same instant, just expressed in UTC


def test_to_utc_iso_passes_none_through():
    assert _to_utc_iso(None) is None


def test_ntfs_timestamps_are_utc_aware(parser):
    """Every NTFS SI/FN timestamp is emitted as UTC-aware ISO-8601."""
    from datetime import datetime, timedelta

    target = next(f for f in _regular_files(parser) if parser.get_size_of(f.path) > 0)
    ts = parser.get_timestamps_for_file(target.path)
    assert set(ts) == {"standard_information_attribute", "file_name_attribute"}
    for attr in ts.values():
        for value in attr.values():
            parsed = datetime.fromisoformat(value)
            assert parsed.tzinfo is not None
            assert parsed.utcoffset() == timedelta(0)
