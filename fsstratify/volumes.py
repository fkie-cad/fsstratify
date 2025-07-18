"""This module contains the different volumes types."""

import os
import shutil
import subprocess
from abc import ABC
from io import FileIO, SEEK_SET
from pathlib import Path
from subprocess import CalledProcessError

from fsstratify.configuration import Configuration
from fsstratify.platforms import Platform, get_current_platform

if get_current_platform() == Platform.LINUX:
    from fallocate import fallocate

from fsstratify.errors import VolumeError, SimulationError
from fsstratify.utils import (
    run_diskpart_script,
    parse_size_definition,
    run_powershell_script,
)


class FileSystem(FileIO):
    def __init__(self, path: Path, file_system_offset: int):
        super().__init__(path)
        self.file_system_offset = file_system_offset

    def seek(self, offset: int, whence: int = SEEK_SET) -> int:
        return super().seek(offset + self.file_system_offset, whence)


class Volume:
    """Volume base class."""

    def __init__(self, config: Configuration):
        self.path = None
        self.mount_point = config["mount_point"]
        self._config = config["volume"]
        self._dirty = self._config.get("dirty", False)
        self._fp = None
        self._fs_offset = 0

    def flush(self):  # pragma: no cover
        """Flush the write-cache.

        This method is supposed to implement the required steps to flush any caches and
        buffers so that changes made to the file system are actually written.
        """
        raise NotImplementedError

    def get_fs_offset(self):
        return self._fs_offset

    def get_rel_space_usg(self) -> float:
        """Get relative space usage as value between 0 and 1."""
        total, used, _ = shutil.disk_usage(self.mount_point)
        return used / total

    def __enter__(self):  # pragma: no cover
        raise NotImplementedError

    def __exit__(self, exc_type, exc_val, exc_tb):  # pragma: no cover
        raise NotImplementedError

    def has_mnt_dir(self, mnt_dir: Path):  # pragma: no cover
        """Check if volume is mounted at specified path."""
        raise NotImplementedError

    def get_filesystem(self) -> FileSystem:
        return FileSystem(self.path, self._fs_offset)


class FileBasedVolume(Volume, ABC):
    """Base class for file based volumes."""

    def __init__(self, config: Configuration):
        super().__init__(config)
        self._set_path()
        self._existing = False
        self._force_overwrite = self._config.get("force_overwrite", False)
        self._check_if_volume_exists()

    def __enter__(self):
        if not self._existing or (self._existing and self._force_overwrite):
            if self._force_overwrite:
                self.path.unlink(missing_ok=True)
                self._existing = False
            if not self._existing:
                self._create()
        elif self._existing and not (self._force_overwrite or self._dirty):
            raise VolumeError(
                f"Volume {self.path} already exists and neither force_overwrite nor dirty is not set."
            )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self._config["keep"]:
            self.path.unlink(missing_ok=True)

    def _set_path(self):
        vol_path = self._config.get("path")
        if vol_path:
            self.path = Path(vol_path).resolve()
        else:
            self.path = Path(self._config["directory"]).resolve() / "fs.img"

    def _check_if_volume_exists(self):
        if self.path.exists():
            self._existing = True

    def flush(self):  # pragma: no cover
        raise NotImplementedError

    def _create(self):  # pragma: no cover
        raise NotImplementedError


class LinuxRawDiskImage(FileBasedVolume):
    """Linux raw disk file implementation.

    The LinuxRawDiskImage uses fallocate to create a new file based image file. If the
    file already exists, it is left unmodified.
    """

    def __init__(self, config: Configuration):
        super().__init__(config)

    def flush(self):
        # TODO: can we make this more efficient?
        subprocess.run("sync")

    def _create(self):
        with self.path.open("wb") as self._fp:
            fallocate(self._fp, offset=0, len=self._config["size"])
            self._fp.flush()
            os.fsync(self._fp.fileno())
        self.flush()

    def has_mnt_dir(self, mnt_dir: Path):
        if not mnt_dir.is_mount():
            return False
        sub = subprocess.run(
            ["findmnt", "--target", f"{mnt_dir}", "--output", "SOURCE"],
            encoding="utf8",
            capture_output=True,
            check=True,
        )
        if "SOURCE" not in sub.stdout:
            return False
        dev = sub.stdout.split("SOURCE")[1].strip()
        if dev == "":
            return False
        sub = subprocess.run(
            ["losetup", f"{dev}", "--output", "BACK-FILE"],
            encoding="utf8",
            capture_output=True,
            check=True,
        )
        if "BACK-FILE" not in sub.stdout:
            return False
        image_path = sub.stdout.split("BACK-FILE")[1].strip()
        if image_path == "":
            return False
        if Path(image_path) != self.path:
            return False
        return True


class WindowsRawDiskImage(FileBasedVolume):
    """Windows raw disk file implementation.

    The WindowsRawDiskImage uses diskpart to create a new file based image file. If the file already exists, it is left
    unmodified.
    """

    def _set_path(self):
        vol_path = self._config.get("path")
        if vol_path:
            self.path = Path(vol_path).resolve()
        else:
            self.path = Path(self._config["directory"]).resolve() / "fs.vhd"

    def __init__(self, config: Configuration):
        super().__init__(config)
        self.drive_letter = ""
        image_size = self._config["size"]
        self._fs_offset = (
            parse_size_definition("64KiB")
            if image_size <= parse_size_definition("4GiB")
            else parse_size_definition("1MiB")
        )  # set to Windows default partition alignment

    def flush(self):
        run_powershell_script(
            f"Write-VolumeCache -DriveLetter {self.drive_letter} | Out-Null", check=True
        )

    def _create(self):
        size = int(self._config["size"] / 1024**2)
        diskpart_script = (
            f"CREATE VDISK FILE='{self.path}' MAXIMUM={size + 2} TYPE=FIXED\n"
            f"SELECT VDISK FILE='{self.path}'\n"
            "ATTACH VDISK\n"
            f"CREATE PARTITION PRIMARY SIZE={size}\n"
            "ACTIVE\n"
            "DETACH VDISK"
        )
        try:
            run_diskpart_script(
                diskpart_script
                # f"CREATE VDISK FILE='{self.path}' MAXIMUM={int(self._config['size'] / (1024 * 1024)) + 2}"
            )  # +2 MiB extra space
        except CalledProcessError as err:
            raise SimulationError("Error: Unable to create virtual disk:", err)

    def has_mnt_dir(self, mnt_dir: Path):
        sub = run_powershell_script(
            f"Write-Output (Get-Volume -FilePath '{mnt_dir}' | Get-DiskImage).ImagePath"
        )
        if sub.returncode != 0:
            return False
        image_path = Path(sub.stdout[:-1])
        return image_path == self.path
