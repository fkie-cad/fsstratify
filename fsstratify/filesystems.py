"""This module contains the various file system classes."""
import datetime
import logging
import math
import random
import shutil
import stat as stat_module
from collections import namedtuple
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import List, Tuple, Optional, Set, Iterable

import dissect.ntfs.exceptions
from attr import define, field
from dissect.ntfs import NTFS
from dissect.ntfs.mft import MftRecord
from dissect.extfs import ExtFS
from dissect.extfs.exceptions import FileNotFoundError as ExtFileNotFoundError
from dissect.fat import FATFS
from dissect.fat.exceptions import FileNotFoundError as FatFileNotFoundError

from fsstratify.configuration import FSSTRATIFY_BLOCK_SIZE
from fsstratify.errors import SimulationError, ConfigurationError
from fsstratify.utils import get_random_string, merge_overlapping_fragments
from fsstratify.volumes import Volume

MAX_FILE_NAME_LENGTH = 255

_SIMULATION_MOUNT_POINT: Optional[Path] = None
_MAX_TRIES_FOR_NONEXISTENT_PATH = 100000

_LOGGER = logging.getLogger(__name__)


def _byte_range_to_block_range(start_byte: int, length_byte: int) -> Tuple[int, int]:
    """Convert an absolute byte extent into an inclusive 512-byte block range.

    All stratum block ranges use ``FSSTRATIFY_BLOCK_SIZE`` (512) byte units and are
    inclusive on both ends, i.e. ``(first_block, last_block)``.
    """
    first = start_byte // FSSTRATIFY_BLOCK_SIZE
    last = (start_byte + length_byte - 1) // FSSTRATIFY_BLOCK_SIZE
    return first, last


def _clusters_to_block_range(
    start_lcn: int, count: int, cluster_size: int
) -> Tuple[int, int]:
    """Convert an NTFS cluster run into an inclusive 512-byte block range.

    Funnels cluster runs through :func:`_byte_range_to_block_range` so that NTFS
    ``allocated_areas`` and ``fs_areas`` share the same unit and interval convention.
    """
    return _byte_range_to_block_range(start_lcn * cluster_size, count * cluster_size)


def _normalize_timestamps(created, modified, changed, accessed) -> dict:
    """Return a file-system-agnostic timestamp dict with ISO-formatted values.

    Fields that the underlying file system does not provide are reported as ``None``.
    """

    def _iso(value):
        return value.isoformat() if value is not None else None

    return {
        "created": _iso(created),
        "modified": _iso(modified),
        "changed": _iso(changed),
        "accessed": _iso(accessed),
    }


def set_simulation_mount_point(path: Optional[Path]) -> None:
    """Set the simulation mount point.

    This function sets or unsets the mount point where the operations of the simulation
    are carried out.
    """
    global _SIMULATION_MOUNT_POINT
    if path is not None:
        _SIMULATION_MOUNT_POINT = path.resolve()
    else:
        _SIMULATION_MOUNT_POINT = None


def get_path_in_mount_point(path: Path) -> Path:
    if _SIMULATION_MOUNT_POINT is None:
        raise SimulationError("No simulation mount point set.")
    if path.is_absolute():
        return _SIMULATION_MOUNT_POINT / path.relative_to("/")
    return _SIMULATION_MOUNT_POINT / path


class FileType(Enum):
    """This enum represents the different file type."""

    REGULAR = 1
    DIRECTORY = 2


@define(frozen=True)
class File:
    type: FileType
    path: Path = field(
        converter=Path
    )  # TODO below is a hack to have consistent posix path since dissect uses Windows paths everywhere
    # path: Path = field(converter=lambda s: Path(PureWindowsPath(s)))


class SimulationVirtualFileSystem:
    def __init__(self, volume: Volume, mount_point: Path, fs_type: str):
        self._volume: Volume = volume
        self._file_system = get_file_system_parser(fs_type, self._volume)
        self._mount_point: Path = mount_point.resolve().absolute()

    def empty(self) -> bool:
        """Returns True is the file system has no active files or directories."""
        return len(self._file_system.get_files()) == 0

    def get_count_of(self, filetype: FileType) -> int:
        """Return the number of active files of the given type."""
        return len(self._get_files_by_type(filetype))

    def get_files(self, file_type: Optional[FileType] = None) -> List[File]:
        """Get a list of active files.

        The list will contain all active files currently available on the file system.
        """
        if file_type is None:
            files = self._file_system.get_files()
            return files
        if file_type == FileType.REGULAR:
            return self._get_files_by_type(FileType.REGULAR)
        if file_type == FileType.DIRECTORY:
            return self._get_files_by_type(FileType.DIRECTORY)
        return []

    def get_size_of(self, path: Path) -> int:
        """Return the size of the given file in bytes."""
        return self._file_system.get_size_of(path)

    def get_timestamps_for_file(self, path: Path) -> dict[str:str]:
        """Return the four ntfs-timestamps from standard-information for the given file."""
        return self._file_system.get_timestamps_for_file(path)

    def get_allocated_fragments_for_file(self, path: Path) -> List[Tuple[int, int]]:
        """Return a list of blocks allocated for the given file."""
        return self._file_system.get_allocated_fragments_for_file(path)

    def get_usage(self) -> namedtuple:
        return shutil.disk_usage(self._mount_point)

    def get_free_space(self) -> int:
        """Return the free space in bytes."""
        return self.get_usage().free

    def get_file_system_areas(self) -> List[Tuple[int, int]]:
        return self._file_system.get_metadata_blocks()

    def get_random_file(
        self,
        file_type: Optional[FileType] = None,
        files_to_filter_out: Set[File] | None = None,
        file_filter=None,
    ) -> File | None:
        """Return the path of a random active file."""
        if file_filter is not None:
            files = list(self._get_files_by_type(FileType.REGULAR))
            files.extend(self._get_files_by_type(FileType.DIRECTORY))
            files = file_filter(self._file_system, files)
            if len(files) == 0:
                return None
            else:
                return random.choice(files)
        if file_type in (FileType.REGULAR, FileType.DIRECTORY):
            files = self._get_files_by_type(file_type)
            if len(files) == 0:
                if file_type == FileType.REGULAR:
                    msg = "Error: there are no regular files on this file system."
                else:
                    msg = "Error: there are no directories on this file system."
                raise SimulationError(msg)
            files = self._get_filtered_files(files, files_to_filter_out)
            if not files:
                return None
            return random.choice(files)
        elif file_type is None:
            files = list(self._get_files_by_type(FileType.REGULAR))
            files.extend(self._get_files_by_type(FileType.DIRECTORY))
            if len(files) == 0:
                raise SimulationError(
                    "Error: there are no files or directories on this file system."
                )
            files = self._get_filtered_files(files, files_to_filter_out)
            if not files:
                return None
            return random.choice(files)

    @staticmethod
    def _get_filtered_files(
        all_files: List[File], files_to_filter_out: Set[File] | None
    ) -> List[File]:
        """Return filtered list of files."""
        if not files_to_filter_out:
            return all_files
        return list(filter(lambda f: f not in files_to_filter_out, all_files))

    def get_files_below(self, path: Path):
        """If path is a directory, return the list of the files in it.
        If path is a file, return [file]
        """
        return self._file_system.get_files_below(path)

    def get_nonexistent_path(
        self, length: int = 8, skip_dir: File | None = None
    ) -> Path:
        """Return a valid but nonexistent path.

        This method return a file path somewhere in the file system (i.e. it may
        include sub-folders) which does not exist yet. This is useful to get the path
        for a new file or directory.

        Args:
            length: Length of the file name.
            skip_dir: do not generate subdirs of it.
        """
        if length > MAX_FILE_NAME_LENGTH:
            raise SimulationError(
                f'Error: requested file name length of {length} "\
                f"exceeds max. file name length ({MAX_FILE_NAME_LENGTH}).'
            )
        i = 0
        while True:
            path = self._get_random_directory(skip_dir=skip_dir) / get_random_string(
                length
            )
            if not get_path_in_mount_point(path).exists():
                return path
            if i >= _MAX_TRIES_FOR_NONEXISTENT_PATH:
                raise SimulationError(
                    "Error: unable to generate a nonexistent files"
                    f"after {_MAX_TRIES_FOR_NONEXISTENT_PATH} tries."
                )
            i += 1

    def collect_immutable_files(self) -> None:
        """Create and store a list of immutable files.

        This method fill mark all files currently active in the file system as immutable.
        """
        self._file_system.collect_immutable_files()

    def _get_files_by_type(self, filetype: FileType) -> List[File]:
        return list(f for f in self._file_system.get_files() if f.type == filetype)

    def _get_random_directory(self, skip_dir: File | None = None) -> Path:
        """Return the path of a random active directory."""
        dirs = self._get_files_by_type(FileType.DIRECTORY)
        # in case of cp or mv, we want to avoid copying a parent directory to its subdirectory
        # so we may want to skip all the subdirs of skip_dir during random dst generation
        if skip_dir:
            dirs = self._skip_relative_paths(dirs, skip_dir)
        if len(dirs) == 0:
            return Path("/")
        return random.choice(dirs).path

    def _skip_relative_paths(self, dirs: List[File], path: File):
        """Return the list of directories without subdirectories of the path."""
        return list(
            filter(lambda dir_path: not self.is_subdirectory_of(dir_path, path), dirs)
        )

    @staticmethod
    def is_subdirectory_of(path: File, other: File) -> bool:
        """Return whether the path is subdir of the other path."""
        return path.path.is_relative_to(other.path)


class FileSystemParser:
    def __init__(self, volume: Volume):
        self._volume: Volume = volume
        self._allocated_areas: list = []
        self._immutable_files: set = set()

    def get_timestamps_for_file(self, path: Path) -> dict[str:str]:
        """Return the timestamps for the given file.
        Args:
            path: The path to the desired file within the simulation volume.
        """
        raise NotImplementedError

    def get_allocated_fragments_for_file(
        self, path: Path
    ) -> List[Tuple[int, int]]:  # pragma: no cover
        """Get the file system areas allocated for a given file.

        Args:
            path: The path to the desired file within the simulation volume.
        """
        raise NotImplementedError

    def get_files(self) -> List[File]:
        """Get a list of active files.

        The list will contain all active and mutable files currently available on the file system.
        """
        active_files = self._get_files()
        return [f for f in active_files if f not in self._immutable_files]

    def get_size_of(self, path: Path) -> int:
        """Return the size of the given file in bytes."""
        raise NotImplementedError

    def _get_files(self) -> List[File]:  # pragma: no cover
        raise NotImplementedError

    def collect_immutable_files(self) -> None:
        """Create and store a list of immutable files.

        This method marks all files currently active in the file system as immutable.
        """
        self._immutable_files = set(self._get_files())

    def get_files_below(self, directory: Path):
        raise NotImplementedError

    def get_metadata_blocks(self) -> List[Tuple[int, int]]:
        """Return file-system metadata areas as inclusive 512-byte block ranges.

        Parsers that do not expose metadata areas return an empty list.
        """
        return []


class NtfsParser(FileSystemParser):
    MFT_RECORD_SIZE = 1024

    def __init__(self, volume: Volume):
        super().__init__(volume)

    def get_timestamps_for_file(self, path: Path) -> dict:
        with self._volume.get_filesystem() as fs:
            ntfs = NTFS(fs)
            try:
                record = ntfs.mft.get(str(path))
                standard_information_attr = record.attributes[16][0]
                file_name_attr = record.attributes[48][0]
            except FileNotFoundError:
                raise
            if record.is_dir():
                return {}
            return {
                "standard_information_attribute": {
                    "creation_time": standard_information_attr.creation_time.isoformat(),
                    "modified_time": standard_information_attr.last_modification_time.isoformat(),
                    "change_time": standard_information_attr.last_change_time.isoformat(),
                    "access_time": standard_information_attr.last_access_time.isoformat(),
                },
                "file_name_attribute": {
                    "creation_time": file_name_attr.creation_time.isoformat(),
                    "modified_time": file_name_attr.last_modification_time.isoformat(),
                    "change_time": file_name_attr.last_change_time.isoformat(),
                    "access_time": file_name_attr.last_access_time.isoformat(),
                },
            }

    def get_allocated_fragments_for_file(self, path: Path) -> List[Tuple[int, int]]:
        with self._volume.get_filesystem() as fs:
            ntfs = NTFS(fs)
            try:
                record = ntfs.mft.get(str(path))
            except FileNotFoundError:
                raise
            if record.is_dir():
                return []
            if record.resident:
                start_byte = (
                    record.offset + ntfs.boot_sector.MftStartLcn * ntfs.cluster_size
                )
                return [_byte_range_to_block_range(start_byte, self.MFT_RECORD_SIZE)]
            else:
                return [
                    _clusters_to_block_range(run[0], run[1], ntfs.cluster_size)
                    for run in record.dataruns()
                ]

    def get_size_of(self, path: Path) -> int:
        """Return the size of the given file in bytes."""
        with self._volume.get_filesystem() as fs:
            ntfs = NTFS(fs)
            try:
                return ntfs.mft.get(str(path)).size()
            except FileNotFoundError:
                raise SimulationError(f'Error: file "{path}" does not exist.')

    def get_files_below(self, path: Path) -> List[Path]:
        """If path is a directory, return the list of the files in it.
        If path is a file, return [file]
        """
        files = []
        with self._volume.get_filesystem() as fs:
            ntfs = NTFS(fs)
            try:
                record = ntfs.mft.get(str(path))
                if record.is_file():
                    return [path]
                if record.is_dir():
                    for item in record.listdir():
                        child_path = path / item
                        child = ntfs.mft.get(str(child_path))
                        if child.is_dir():
                            files.extend(self.get_files_below(child_path))
                        if child.is_file():
                            files.append(child_path)
                return files
            except FileNotFoundError:
                raise SimulationError(f'Error: path "{path}" does not exist.')

    def _get_files(self) -> List[File]:
        files = []
        with self._volume.get_filesystem() as fs:
            ntfs = NTFS(fs)
            for segment in ntfs.mft.segments():
                if (
                    not segment.filename
                    or self._is_metadata_file(segment)
                    or self._is_deleted(segment)
                ):
                    continue
                if segment.is_file():
                    files.append(File(path=segment.full_path(), type=FileType.REGULAR))
                if segment.is_dir():
                    files.append(
                        File(path=segment.full_path(), type=FileType.DIRECTORY)
                    )
            return files

    def _is_deleted(self, record: MftRecord):
        """If the last bit of flags is set to 1, the record is in use, o/w is deleted."""
        return record.header.Flags % 2 == 0

    def get_metadata_blocks(self) -> List[Tuple[int, int]]:
        file_system_areas = []
        with self._volume.get_filesystem() as fs:
            ntfs = NTFS(fs)
            for record in ntfs.mft.segments():
                # TODO: records w/o full_path
                if record.full_path() and self._is_metadata_file(record):
                    for attribute in record.attributes.values():
                        for attr in attribute:
                            if attr.resident:
                                continue  # resident data is already covered by the $MFT file
                            try:
                                for run in attr.dataruns():
                                    if None not in run:
                                        file_system_areas.append(
                                            _clusters_to_block_range(
                                                run[0], run[1], ntfs.cluster_size
                                            )
                                        )
                            except dissect.ntfs.exceptions.FileNotFoundError:
                                # TODO: generate a warning
                                pass
            return file_system_areas

    @staticmethod
    def _is_metadata_file(record: MftRecord) -> bool:
        if record.segment <= 16:
            return True
        path = record.full_path()
        if path.startswith("$") or path.startswith("System Volume Information"):
            return True
        return False


class ExtParser(FileSystemParser):
    """Read-back parser for ext2/ext3/ext4 file systems (via ``dissect.extfs``).

    All returned block ranges are inclusive 512-byte block indices, file-system
    relative (the underlying ``Volume`` already accounts for any partition offset).
    """

    _RESERVED_ROOT_NAMES = {".", "..", "lost+found"}

    @staticmethod
    def _is_dir(inode) -> bool:
        return stat_module.S_ISDIR(inode.filetype)

    @staticmethod
    def _is_regular(inode) -> bool:
        return stat_module.S_ISREG(inode.filetype)

    def get_timestamps_for_file(self, path: Path) -> dict:
        with self._volume.get_filesystem() as fh:
            fs = ExtFS(fh)
            try:
                inode = fs.get(str(path))
            except ExtFileNotFoundError:
                raise FileNotFoundError(str(path))
            if self._is_dir(inode):
                return {}
            return _normalize_timestamps(
                created=inode.crtime,
                modified=inode.mtime,
                changed=inode.ctime,
                accessed=inode.atime,
            )

    def get_allocated_fragments_for_file(self, path: Path) -> List[Tuple[int, int]]:
        with self._volume.get_filesystem() as fh:
            fs = ExtFS(fh)
            try:
                inode = fs.get(str(path))
            except ExtFileNotFoundError:
                raise FileNotFoundError(str(path))
            if self._is_dir(inode):
                return []
            block_size = fs.block_size
            fragments = []
            for start_block, count in inode.dataruns():
                if start_block is None:  # sparse run - not allocated on disk
                    continue
                fragments.append(
                    _byte_range_to_block_range(
                        start_block * block_size, count * block_size
                    )
                )
            return merge_overlapping_fragments(fragments)

    def get_size_of(self, path: Path) -> int:
        with self._volume.get_filesystem() as fh:
            fs = ExtFS(fh)
            try:
                return fs.get(str(path)).size
            except ExtFileNotFoundError:
                raise SimulationError(f'Error: file "{path}" does not exist.')

    def get_files_below(self, path: Path) -> List[Path]:
        path = Path(path)
        with self._volume.get_filesystem() as fh:
            fs = ExtFS(fh)
            try:
                inode = fs.get(str(path))
            except ExtFileNotFoundError:
                raise SimulationError(f'Error: path "{path}" does not exist.')
            return self._files_below(inode, path)

    def _files_below(self, inode, path: Path) -> List[Path]:
        if self._is_regular(inode):
            return [path]
        files = []
        for name, child in inode.listdir().items():
            if name in (".", ".."):
                continue
            child_path = path / name
            if self._is_dir(child):
                files.extend(self._files_below(child, child_path))
            elif self._is_regular(child):
                files.append(child_path)
        return files

    def _get_files(self) -> List[File]:
        with self._volume.get_filesystem() as fh:
            fs = ExtFS(fh)
            return self._walk(fs.get("/"), Path("/"), is_root=True)

    def _walk(self, inode, path: Path, is_root: bool = False) -> List[File]:
        files = []
        for name, child in inode.listdir().items():
            if name in (".", ".."):
                continue
            if is_root and name in self._RESERVED_ROOT_NAMES:
                continue
            child_path = path / name
            if self._is_dir(child):
                files.append(File(path=child_path, type=FileType.DIRECTORY))
                files.extend(self._walk(child, child_path))
            elif self._is_regular(child):
                files.append(File(path=child_path, type=FileType.REGULAR))
        return files

    def get_metadata_blocks(self) -> List[Tuple[int, int]]:
        with self._volume.get_filesystem() as fh:
            fs = ExtFS(fh)
            block_size = fs.block_size
            # The primary superblock is always at byte offset 1024, length 1024.
            areas = [_byte_range_to_block_range(1024, 1024)]
            try:
                gdt_start_block = fs.sb.s_first_data_block + 1
                gdt_bytes = fs.groups_count * fs._group_desc_size
                areas.append(
                    _byte_range_to_block_range(gdt_start_block * block_size, gdt_bytes)
                )
                inode_table_blocks = math.ceil(
                    fs.sb.s_inodes_per_group * fs.sb.s_inode_size / block_size
                )
                for group in range(fs.groups_count):
                    gd = fs._read_group_desc(group)
                    block_bitmap = (
                        getattr(gd, "bg_block_bitmap_hi", 0) << 32
                    ) | gd.bg_block_bitmap_lo
                    inode_bitmap = (
                        getattr(gd, "bg_inode_bitmap_hi", 0) << 32
                    ) | gd.bg_inode_bitmap_lo
                    inode_table = (
                        getattr(gd, "bg_inode_table_hi", 0) << 32
                    ) | gd.bg_inode_table_lo
                    areas.append(
                        _byte_range_to_block_range(block_bitmap * block_size, block_size)
                    )
                    areas.append(
                        _byte_range_to_block_range(inode_bitmap * block_size, block_size)
                    )
                    areas.append(
                        _byte_range_to_block_range(
                            inode_table * block_size,
                            inode_table_blocks * block_size,
                        )
                    )
            except Exception as err:  # pragma: no cover - defensive
                _LOGGER.warning(
                    "Could not fully enumerate ext metadata blocks (%s); "
                    "fs_areas may be incomplete.",
                    err,
                )
            # The journal is intentionally not enumerated yet.
            _LOGGER.debug("ext fs_areas do not include journal blocks.")
            return areas


class FatParser(FileSystemParser):
    """Read-back parser for FAT12/16/32 file systems (via ``dissect.fat``).

    All returned block ranges are inclusive 512-byte block indices, file-system
    relative. dissect reports data runs as data-region-relative (0-based) cluster
    numbers, so the absolute byte offset of a run is
    ``first_data_sector * sector_size + cluster * cluster_size``.
    """

    @staticmethod
    def _cluster_run_to_block_range(fs, cluster: int, count: int) -> Tuple[int, int]:
        data_start = fs.first_data_sector * fs.sector_size
        start_byte = data_start + cluster * fs.cluster_size
        return _byte_range_to_block_range(start_byte, count * fs.cluster_size)

    def get_timestamps_for_file(self, path: Path) -> dict:
        with self._volume.get_filesystem() as fh:
            fs = FATFS(fh)
            try:
                entry = fs.get(str(path))
            except FatFileNotFoundError:
                raise FileNotFoundError(str(path))
            if entry.is_directory():
                return {}
            # FAT has no separate metadata-change time; times are local, ~2s res.
            return _normalize_timestamps(
                created=entry.ctime,
                modified=entry.mtime,
                changed=None,
                accessed=entry.atime,
            )

    def get_allocated_fragments_for_file(self, path: Path) -> List[Tuple[int, int]]:
        with self._volume.get_filesystem() as fh:
            fs = FATFS(fh)
            try:
                entry = fs.get(str(path))
            except FatFileNotFoundError:
                raise FileNotFoundError(str(path))
            if entry.is_directory():
                return []
            fragments = [
                self._cluster_run_to_block_range(fs, cluster, count)
                for cluster, count in entry.dataruns()
            ]
            return merge_overlapping_fragments(fragments)

    def get_size_of(self, path: Path) -> int:
        with self._volume.get_filesystem() as fh:
            fs = FATFS(fh)
            try:
                return fs.get(str(path)).size
            except FatFileNotFoundError:
                raise SimulationError(f'Error: file "{path}" does not exist.')

    def get_files_below(self, path: Path) -> List[Path]:
        path = Path(path)
        with self._volume.get_filesystem() as fh:
            fs = FATFS(fh)
            try:
                entry = fs.get(str(path))
            except FatFileNotFoundError:
                raise SimulationError(f'Error: path "{path}" does not exist.')
            return self._files_below(entry, path)

    def _files_below(self, entry, path: Path) -> List[Path]:
        if not entry.is_directory():
            return [path]
        files = []
        for child in entry.iterdir():
            if child.name in (".", "..") or child.is_volume_id():
                continue
            child_path = path / child.name
            if child.is_directory():
                files.extend(self._files_below(child, child_path))
            else:
                files.append(child_path)
        return files

    def _get_files(self) -> List[File]:
        with self._volume.get_filesystem() as fh:
            fs = FATFS(fh)
            return self._walk(fs.get("/"), Path("/"))

    def _walk(self, directory, path: Path) -> List[File]:
        files = []
        for child in directory.iterdir():
            if child.name in (".", "..") or child.is_volume_id():
                continue
            child_path = path / child.name
            if child.is_directory():
                files.append(File(path=child_path, type=FileType.DIRECTORY))
                files.extend(self._walk(child, child_path))
            else:
                files.append(File(path=child_path, type=FileType.REGULAR))
        return files

    def get_metadata_blocks(self) -> List[Tuple[int, int]]:
        with self._volume.get_filesystem() as fh:
            fs = FATFS(fh)
            # Everything before the data region is metadata: the reserved region
            # (boot sector + FSInfo), both FATs, and (FAT12/16) the fixed root dir.
            system_area_bytes = fs.first_data_sector * fs.sector_size
            return [_byte_range_to_block_range(0, system_area_bytes)]


def get_file_system_parser(fs_type: str, volume: Volume) -> FileSystemParser:
    fstype = fs_type.lower()
    if fstype == "ntfs":
        return NtfsParser(volume)
    if fstype in ("ext2", "ext3", "ext4"):
        return ExtParser(volume)
    if fstype in ("fat12", "fat16", "fat32"):
        return FatParser(volume)
    raise ConfigurationError(
        f'File system "{fs_type}" is not supported. Read-back is implemented for: '
        "ntfs, ext2/ext3/ext4, fat12/fat16/fat32."
    )


class FileFilter:
    """This class filters a given set of files based on previously defined filter criteria."""

    def __init__(
        self,
        file_type: FileType = None,
        min_size: int = 0,
        max_size: int = math.inf,
        excluded_paths: Iterable[Path] = None,
    ):
        self._file_type = file_type
        self._min_size = min_size
        self._max_size = max_size
        self._excluded_paths = excluded_paths or []

    def __call__(
        self, vfs: SimulationVirtualFileSystem, files: Iterable[File]
    ) -> List[File]:
        filtered = []
        for f in files:
            if self._file_type is not None and f.type != self._file_type:
                continue
            if f.path in self._excluded_paths:
                continue
            if f.type == FileType.REGULAR:
                if self._min_size <= vfs.get_size_of(f.path) <= self._max_size:
                    filtered.append(f)
        return filtered
