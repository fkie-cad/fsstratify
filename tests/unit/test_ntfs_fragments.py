"""Tests for NTFS read-back unit consistency (audit finding C2).

Before the fix, ``NtfsParser.get_metadata_blocks`` emitted raw NTFS *cluster* numbers
while ``get_allocated_fragments_for_file`` emitted *512-byte block* indices, so a single
stratum mixed two address spaces (8x apart at the 4 KiB default cluster) that could not
be overlaid. These tests pin the fix: both now use inclusive 512-byte block ranges.

They run on any platform because parsing is detached from any mount/OS: the existing
NTFS ``.vhd`` fixture is read directly through a ``FileSystem`` handle at the partition
offset, exactly as ``Volume.get_filesystem`` does at run time.
"""

from pathlib import Path

import pytest

from fsstratify.filesystems import (
    NtfsParser,
    _byte_range_to_block_range,
    _clusters_to_block_range,
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


def test_metadata_and_allocated_areas_share_one_address_space(parser, ntfs_image):
    """C2 proof: fs_areas and allocated_areas live in the same 512-byte block space.

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
