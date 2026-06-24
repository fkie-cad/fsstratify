"""Tests for non-NTFS read-back (audit finding C1).

Before the fix, ``get_file_system_parser`` only supported NTFS, so any ext*/FAT
simulation crashed at VFS construction and produced no strata. These tests show that
ext2/3/4 and FAT12/16/32 are now parsed: files are enumerated, sizes/timestamps are
read, and the reported allocated block ranges actually point at the file's data.

The filesystem images are built at test time without root using ``mke2fs -d`` (ext)
and ``mkfs.fat`` + mtools (FAT); tests are skipped if those tools are unavailable.
"""

import math
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from fsstratify.configuration import Configuration
from fsstratify.errors import ConfigurationError
from fsstratify.filesystems import (
    ExtParser,
    FatParser,
    FileType,
    NtfsParser,
    SimulationVirtualFileSystem,
    get_file_system_parser,
)
from fsstratify.volumes import FileSystem

FSSTRATIFY_BLOCK = 512


def _pattern(n: int) -> bytes:
    """Deterministic, non-trivial byte pattern of length ``n``."""
    return bytes((i * 7 + 3) % 256 for i in range(n))


# Known on-disk layout written into every fixture image.
FILES = {
    "/alpha.bin": _pattern(9000),  # multi-block regular file
    "/beta.txt": b"hello fsstratify\n",  # tiny regular file
    "/sub/gamma.bin": _pattern(5000),  # nested regular file
}
EXPECTED_REGULAR = {"/alpha.bin", "/beta.txt", "/sub/gamma.bin"}
EXPECTED_DIRS = {"/sub"}

# (mkfs flag, image size in bytes) per supported FAT variant.
_FAT_VARIANTS = {"fat12": ("12", 2 * 1024**2), "fat16": ("16", 32 * 1024**2)}
_FAT_VARIANTS["fat32"] = ("32", 64 * 1024**2)


class _ImageVolume:
    """Minimal ``Volume`` stand-in that exposes a real image file to the parsers.

    Mirrors ``Volume.get_filesystem`` (``volumes.py``): a fresh, offset-aware file
    handle per call, which is what the parsers consume inside a ``with`` block.
    """

    def __init__(self, path: Path, fs_offset: int = 0):
        self.path = Path(path)
        self._fs_offset = fs_offset

    def get_filesystem(self) -> FileSystem:
        return FileSystem(self.path, self._fs_offset)


def _require(*tools: str) -> None:
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        pytest.skip(f"required tool(s) not available: {', '.join(missing)}")


def _build_ext4(img: Path, workdir: Path) -> None:
    _require("mke2fs")
    src = workdir / "src"
    (src / "sub").mkdir(parents=True)
    for path, content in FILES.items():
        (src / path.lstrip("/")).write_bytes(content)
    # 1 KiB blocks make block-size != 512 explicit; 8 MiB => 8192 blocks.
    subprocess.run(
        ["mke2fs", "-t", "ext4", "-b", "1024", "-d", str(src), "-F", "-q",
         str(img), "8192"],
        check=True,
        capture_output=True,
    )


def _build_fat(img: Path, workdir: Path, flag: str, size: int) -> None:
    _require("mkfs.fat", "mmd", "mcopy")
    with img.open("wb") as fh:
        fh.truncate(size)
    subprocess.run(
        ["mkfs.fat", "-F", flag, "-n", "FSSTRAT", str(img)],
        check=True,
        capture_output=True,
    )
    subprocess.run(["mmd", "-i", str(img), "::/sub"], check=True, capture_output=True)
    for path, content in FILES.items():
        tmp = workdir / Path(path).name
        tmp.write_bytes(content)
        subprocess.run(
            ["mcopy", "-i", str(img), str(tmp), f"::{path}"],
            check=True,
            capture_output=True,
        )


@pytest.fixture(
    scope="module", params=["ext4", "fat12", "fat16", "fat32"]
)
def fs_image(request, tmp_path_factory):
    fstype = request.param
    workdir = tmp_path_factory.mktemp(fstype)
    img = workdir / f"{fstype}.img"
    if fstype == "ext4":
        _build_ext4(img, workdir)
    else:
        flag, size = _FAT_VARIANTS[fstype]
        _build_fat(img, workdir, flag, size)
    return SimpleNamespace(path=img, fstype=fstype, size=img.stat().st_size)


@pytest.fixture
def parser(fs_image):
    return get_file_system_parser(fs_image.fstype, _ImageVolume(fs_image.path))


def _read_blocks(img: Path, ranges) -> bytes:
    """Concatenate the bytes of the given inclusive 512-byte block ranges."""
    out = bytearray()
    with img.open("rb") as fh:
        for first, last in ranges:
            fh.seek(first * FSSTRATIFY_BLOCK)
            out += fh.read((last - first + 1) * FSSTRATIFY_BLOCK)
    return bytes(out)


# --------------------------------------------------------------------------- #
# Dispatch + config gate (no external tools required)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "fs_type, expected",
    [
        ("ntfs", NtfsParser),
        ("ext2", ExtParser),
        ("ext3", ExtParser),
        ("ext4", ExtParser),
        ("EXT4", ExtParser),  # case-insensitive
        ("fat12", FatParser),
        ("fat16", FatParser),
        ("fat32", FatParser),
    ],
)
def test_dispatch_returns_expected_parser(fs_type, expected):
    assert isinstance(get_file_system_parser(fs_type, _ImageVolume(Path("x"))), expected)


def test_dispatch_rejects_unsupported_filesystem():
    with pytest.raises(ConfigurationError) as err:
        get_file_system_parser("xfs", _ImageVolume(Path("x")))
    assert "not supported" in str(err.value)
    assert "ntfs" in str(err.value) and "ext" in str(err.value) and "fat" in str(err.value)


@pytest.mark.parametrize("fs_type", ["ext4", "fat32", "fat12", "ntfs"])
def test_config_accepts_supported_filesystems(fs_type):
    conf = (
        "seed: 1\n"
        f"file_system:\n  type: {fs_type}\n"
        "volume:\n  type: file\n  keep: no\n  size: 10M\n"
        "usage_model:\n  type: ProbabilisticModel\n"
    )
    Configuration().load_str(conf, Path())  # must not raise


def test_config_rejects_unsupported_filesystem():
    conf = (
        "seed: 1\n"
        "file_system:\n  type: reiserfs\n"
        "volume:\n  type: file\n  keep: no\n  size: 10M\n"
        "usage_model:\n  type: ProbabilisticModel\n"
    )
    with pytest.raises(ConfigurationError) as err:
        Configuration().load_str(conf, Path())
    assert "reiserfs" in str(err.value)


# --------------------------------------------------------------------------- #
# Read-back against real images (ext4 + FAT12/16/32)
# --------------------------------------------------------------------------- #


def test_get_files_enumerates_user_files(parser):
    found = parser.get_files()
    regular = {str(f.path) for f in found if f.type == FileType.REGULAR}
    dirs = {str(f.path) for f in found if f.type == FileType.DIRECTORY}
    # FAT stores short names upper-cased; compare case-insensitively.
    assert {p.lower() for p in regular} == {p.lower() for p in EXPECTED_REGULAR}
    assert {p.lower() for p in EXPECTED_DIRS} <= {p.lower() for p in dirs}


def test_get_size_of_matches_known_sizes(parser, fs_image):
    for path, content in FILES.items():
        lookup = path.upper() if fs_image.fstype.startswith("fat") else path
        assert parser.get_size_of(Path(lookup)) == len(content)


def test_allocated_fragments_point_at_file_data(parser, fs_image):
    """The core correctness proof: bytes at the reported blocks equal the file."""
    for path, content in FILES.items():
        lookup = path.upper() if fs_image.fstype.startswith("fat") else path
        fragments = parser.get_allocated_fragments_for_file(Path(lookup))
        assert fragments, f"no fragments reported for {path} on {fs_image.fstype}"
        reconstructed = _read_blocks(fs_image.path, fragments)
        assert reconstructed[: len(content)] == content


def test_allocated_fragments_are_inclusive_and_in_bounds(parser, fs_image):
    image_blocks = fs_image.size // FSSTRATIFY_BLOCK
    for path, content in FILES.items():
        lookup = path.upper() if fs_image.fstype.startswith("fat") else path
        fragments = parser.get_allocated_fragments_for_file(Path(lookup))
        covered = 0
        for first, last in fragments:
            assert 0 <= first <= last < image_blocks
            covered += last - first + 1
        # Allocation must cover at least the file's bytes (rounded up to a block).
        assert covered * FSSTRATIFY_BLOCK >= len(content)
        assert covered >= math.ceil(len(content) / FSSTRATIFY_BLOCK)


def test_timestamps_have_normalized_schema(parser, fs_image):
    lookup = "/ALPHA.BIN" if fs_image.fstype.startswith("fat") else "/alpha.bin"
    ts = parser.get_timestamps_for_file(Path(lookup))
    assert set(ts) == {"created", "modified", "changed", "accessed"}
    assert ts["created"] is not None and ts["modified"] is not None
    if fs_image.fstype.startswith("fat"):
        # FAT has no metadata-change time.
        assert ts["changed"] is None
    else:
        assert ts["changed"] is not None


def test_get_files_below_lists_nested_files(parser, fs_image):
    root = parser.get_files_below(Path("/"))
    assert {str(p).lower() for p in root} == {p.lower() for p in EXPECTED_REGULAR}
    sub = "/SUB" if fs_image.fstype.startswith("fat") else "/sub"
    below_sub = parser.get_files_below(Path(sub))
    assert [str(p).lower() for p in below_sub] == ["/sub/gamma.bin"]


def test_metadata_blocks_are_nonempty_and_in_bounds(parser, fs_image):
    image_blocks = fs_image.size // FSSTRATIFY_BLOCK
    md = parser.get_metadata_blocks()
    assert md, f"no metadata blocks reported for {fs_image.fstype}"
    for first, last in md:
        assert 0 <= first <= last < image_blocks


def test_simulation_vfs_constructs_and_lists_for_non_ntfs(parser, fs_image, tmp_path):
    """The exact path that used to crash for non-NTFS (C1): build the VFS and read."""
    vfs = SimulationVirtualFileSystem(
        _ImageVolume(fs_image.path), tmp_path, fs_image.fstype
    )
    assert not vfs.empty()
    regular = {str(f.path).lower() for f in vfs.get_files(FileType.REGULAR)}
    assert regular == {p.lower() for p in EXPECTED_REGULAR}
