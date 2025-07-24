"""This module contains the different execution environments."""

from __future__ import annotations
import os
import shutil
import subprocess
from contextlib import contextmanager, ExitStack
from logging import Logger
from pathlib import Path
from subprocess import run, CalledProcessError, PIPE, STDOUT
from typing import TYPE_CHECKING

from fsstratify.errors import ConfigurationError, SimulationError
from fsstratify.filesystems import (
    SimulationVirtualFileSystem,
    set_simulation_mount_point,
)
from fsstratify.operations import Operation
from fsstratify.platforms import Platform, get_current_platform

if TYPE_CHECKING:
    from fsstratify.configuration import Configuration
from fsstratify.utils import (
    get_logger,
    run_diskpart_script,
    format_mkfs_error,
    run_powershell_script,
)
from fsstratify.volumes import (
    LinuxRawDiskImage,
    WindowsRawDiskImage,
)


class ExecutionEnvironment:
    """Execution environment base class.

    This class defines the interface an execution environment has to implement.
    Moreover, it already implements some basic functionality that is common for almost
    all execution environments.
    """

    def __init__(self, config: Configuration):
        self._config: Configuration = config
        self._image = None
        self._context_stack: ExitStack = ExitStack()
        self._logger: Logger = get_logger(
            name="ExecutionEnvironment",
            loglevel=self._config["log_level"],
            logfile=self._config["simulation_log"],
        )

    def __del__(self):
        for h in list(self._logger.handlers):
            h.close()
            self._logger.removeHandler(h)

    def execute(self, operation: Operation):  # pragma: no cover
        """Execute the given operation.

        This method has to be implemented by the child classes of the
        ExecutionEnvironment.
        """
        raise NotImplementedError

    def get_simulation_vfs(self) -> SimulationVirtualFileSystem:
        """Return an instance of the simulation virtual file system."""
        return SimulationVirtualFileSystem(
            self._image,
            self._config["mount_point"],
            self._config["file_system"]["type"].lower(),
        )

    def flush_simulation_vfs(self) -> None:
        """Flush the simulation file system."""
        self._image.flush()

    def __enter__(self):  # pragma: no cover
        raise NotImplementedError

    def __exit__(self, exc_type, exc_val, exc_tb):  # pragma: no cover
        raise NotImplementedError


class WindowsEnvironment(ExecutionEnvironment):
    def execute(self, operation: Operation):
        operation.execute()
        self._image.flush()

    @contextmanager
    def _attach_vdisk(self):
        self._logger.info("Attaching image file to system")
        ps_script = f"""
            $ErrorActionPreference = [System.Management.Automation.ActionPreference]::Stop;
            Mount-DiskImage -ImagePath '{self._image.path}' | Out-Null;
            $diskImage = Get-DiskImage -ImagePath '{self._image.path}';
            $diskNumber = $diskImage.Number;
            $partition = Get-Partition -DiskNumber $diskNumber | Where-Object {{ $_.PartitionNumber -eq 1 }};
            Add-PartitionAccessPath -InputObject $partition -AssignDriveLetter  | Out-Null;
            $volume = Get-Volume -Partition $partition;
            Write-Host $volume.DriveLetter;
        """
        try:
            res = run_powershell_script(ps_script)
            if res.returncode != 0:
                raise
            self._image.drive_letter = res.stdout.strip()
            yield
        except CalledProcessError as err:
            raise SimulationError(f"Unable to attach image as vdisk. {err}") from err

        finally:
            self._logger.info("Detaching image file from system")
            try:
                run_powershell_script(
                    f"Dismount-DiskImage -ImagePath '{self._image.path}' | Out-Null;",
                    check=True,
                )
            except CalledProcessError as err:
                raise SimulationError(
                    f"Unable to detach image from system. {err}"
                ) from err

    def _format_file_system(self):
        self._logger.info("Formatting file system")

        ps_script = f"""
            $ErrorActionPreference = [System.Management.Automation.ActionPreference]::Stop;
            $ProgressPreference = 'SilentlyContinue';
            Mount-DiskImage -ImagePath '{self._image.path}' -NoDriveLetter | Out-Null;
            $diskimage = Get-DiskImage -ImagePath '{self._image.path}';
            $disk = Get-Disk -Number $diskimage.Number;
            $part = Get-Partition -DiskNumber $disk.Number | Where-Object {{$_.PartitionNumber -eq 1}}
            {self._get_format_command()}
            """
        try:
            run_powershell_script(ps_script, check=True)
        except CalledProcessError as err:
            err_msg = "\n".join(("Unable to format file system. Error message:", err))
            raise SimulationError(err_msg) from err
        finally:
            ps_script = f"""
            Dismount-DiskImage -ImagePath '{self._image.path}' -ErrorAction SilentlyContinue | Out-Null;
            """
            run_powershell_script(ps_script, check=True)

    def _get_format_command(self) -> str:
        filesystem = self._config["file_system"]["type"]
        try:
            format_fs_name = {"ntfs": "NTFS"}[filesystem]
        except KeyError:
            msg = (
                f'File system "{filesystem}" is currently not supported by the Windows'
                " execution environment"
            )
            raise ConfigurationError(msg)
        command = f"Format-Volume -Confirm:$false -Force:$true -Partition $part -FileSystem {format_fs_name}"
        if "label" in self._config["file_system"]["formatting_parameters"]:
            label = self._config["file_system"]["formatting_parameters"][
                "label"
            ].strip()
            command = " ".join((command, f"-NewFileSystemLabel '{label}'"))
        return " | ".join((command, "Out-Null;"))

    @contextmanager
    def _mount_file_system(self):
        ps_script_mount = f"""
            $diskimage = Get-DiskImage -ImagePath '{self._image.path}';
            $disk = Get-Disk | Where-Object {{ $_.Number -eq $diskimage.Number }};
            $part = Get-Partition -DiskNumber $disk.Number | Where-Object {{$_.PartitionNumber -eq 1}};
            Add-PartitionAccessPath -DiskNumber $disk.Number -PartitionNumber $part.PartitionNumber -AccessPath '{self._config["mount_point"]}' | Out-Null; 
            """
        ps_script_unmount = f"""
            $diskimage = Get-DiskImage -ImagePath '{self._image.path}';
            $disk = Get-Disk | Where-Object {{ $_.Number -eq $diskimage.Number }};
            $part = Get-Partition -DiskNumber $disk.Number | Where-Object {{$_.PartitionNumber -eq 1}};
            Remove-PartitionAccessPath -DiskNumber $disk.Number -PartitionNumber $part.PartitionNumber -AccessPath '{self._config["mount_point"]}' | Out-Null;
            """
        try:
            self._logger.info("Mounting file system")
            run_powershell_script(ps_script_mount, check=True)
            set_simulation_mount_point(self._config["mount_point"])
            yield
        finally:
            self._logger.info("Unmounting file system")
            set_simulation_mount_point(None)
            try:
                run_powershell_script(ps_script_unmount, check=True)
            except CalledProcessError as err:
                raise SimulationError(f"Unable to unmount image. {err}") from err

    @contextmanager
    def _create_mount_point(self):
        self._logger.info("Creating mount point")
        self._config["mount_point"].mkdir()
        yield
        self._logger.info("Removing mount point")
        shutil.rmtree(self._config["mount_point"])

    def __enter__(self):
        self._logger.info("Setting up the execution environment")
        volume_type = self._config["volume"]["type"]
        if volume_type not in [
            "file",
        ]:
            raise ConfigurationError(
                f'Unsupported volume type "{volume_type}" for the Windows execution environment.'
            )

        if volume_type == "file":  # build context for file-based volume
            self._image = self._context_stack.enter_context(
                WindowsRawDiskImage(self._config)
            )

            if not self._config["volume"]["dirty"]:
                self._format_file_system()
            self._context_stack.enter_context(self._attach_vdisk())
            self._context_stack.enter_context(self._create_mount_point())
            self._context_stack.enter_context(self._mount_file_system())
            self._image.flush()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._logger.info("Cleaning up the execution environment")
        self._context_stack.close()


class BsdEnvironment(ExecutionEnvironment):
    """Base class for BSD execution environments."""


class FreeBsdEnvironment(BsdEnvironment):
    """FreeBSD execution environment."""

    def execute(self, operation: Operation) -> None:
        operation.execute()
        self._image.flush()

    def __enter__(self):
        self._logger.info("Setting up the execution environment")
        volume_type = self._config["volume"]["type"]
        if volume_type == "file":
            self._image = self._context_stack.enter_context(
                LinuxRawDiskImage(self._config["volume"])
            )
        else:
            raise ConfigurationError(
                f'Unsupported volume type "{volume_type}" for the FreeBSD execution'
                " environment."
            )
        if not self._config["volume"]["dirty"]:
            self._format_file_system(self._image.path)
        self._image.flush()
        self._context_stack.enter_context(self._create_mount_point())
        self._context_stack.enter_context(self._mount_file_system(self._image))
        self._image.mount_point = self._config["mount_point"]
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._logger.info("Cleaning up the execution environment")
        self._context_stack.close()

    def _format_file_system(self, path: Path):
        self._logger.info("Formatting file system")
        mkfs_command = self._get_mkfs_command(path)
        try:
            res = subprocess.run(
                mkfs_command, stdout=PIPE, stderr=STDOUT, text=True, check=True
            )
            self._logger.info("%s output:\n%s", mkfs_command[0], res.stdout.strip())
        except CalledProcessError as err:
            mkfs_err = format_mkfs_error(mkfs_command[0], err)
            err_msg = "\n".join(
                ("Unable to format file system. Error message:", mkfs_err)
            )
            raise SimulationError(err_msg) from err

    @contextmanager
    def _create_mount_point(self):
        self._logger.info("Creating mount point")
        self._config["mount_point"].mkdir()
        yield
        self._logger.info("Removing mount point")
        shutil.rmtree(self._config["mount_point"])

    @contextmanager
    def _mount_file_system(self, volume):
        self._logger.info("Mounting file system")
        filesystem = self._config["file_system"]["type"]
        # mount_options = "loop,dirsync"
        mount_options = "loop"
        if filesystem in ("fat12", "fat16", "fat32", "ntfs"):
            mount_options = f"{mount_options},uid={os.getuid()}"
        try:
            subprocess.run(
                (
                    "mount",
                    "-o",
                    mount_options,
                    volume.path,
                    self._config["mount_point"],
                ),
                capture_output=True,
                text=True,
                check=True,
            )
            subprocess.run(
                ["chown", "-R", f"{os.getuid()}", self._config["mount_point"]],
                check=True,
            )
        except CalledProcessError as err:
            raise SimulationError(f"Unable to mount image. {err}") from err
        set_simulation_mount_point(self._config["mount_point"])
        yield
        self._logger.info("Unmounting file system")
        set_simulation_mount_point(None)
        try:
            subprocess.run(
                [
                    "umount",
                    "--force",
                    "--detach-loop",
                    self._config["mount_point"],
                ],
                check=True,
            )
        except CalledProcessError as err:
            raise SimulationError(f"Unable to unmount image. {err}") from err

    def _get_mkfs_command(self, path: Path):
        filesystem = self._config["file_system"]["type"]
        try:
            command = {
                "ext2": [
                    "mkfs.ext2",
                ],
                "ext3": [
                    "mkfs.ext3",
                ],
                "ext4": [
                    "mkfs.ext4",
                ],
                "fat12": [
                    "mkfs.fat",
                    "-F",
                    "12",
                ],
                "fat16": [
                    "mkfs.fat",
                    "-F",
                    "16",
                ],
                "fat32": [
                    "mkfs.fat",
                    "-F",
                    "32",
                ],
                "ntfs": ["mkfs.ntfs", "--force"],
            }[filesystem]
            return command
        except KeyError:
            msg = (
                f'File system "{filesystem}" is currently not supported by the FreeBSD'
                " execution environment"
            )
            raise ConfigurationError(msg) from None


class LinuxEnvironment(ExecutionEnvironment):
    """Linux execution environment."""

    def execute(self, operation: Operation) -> None:
        operation.execute()
        self._image.flush()

    def __enter__(self):
        self._logger.info("Setting up the execution environment")
        volume_type = self._config["volume"]["type"]
        if volume_type == "file":
            self._image = self._context_stack.enter_context(
                LinuxRawDiskImage(self._config)
            )
        else:
            raise ConfigurationError(
                f'Unsupported volume type "{volume_type}" for the Linux execution'
                " environment."
            )
        if not self._config["volume"]["dirty"]:
            self._format_file_system(self._image.path)
        self._image.flush()
        self._context_stack.enter_context(self._create_mount_point())
        self._context_stack.enter_context(self._mount_file_system(self._image))
        self._image.mount_point = self._config["mount_point"]
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._logger.info("Cleaning up the execution environment")
        self._context_stack.close()

    def _format_file_system(self, path: Path):
        self._logger.info("Formatting file system")
        mkfs_command = self._get_mkfs_command(path)
        try:
            res = subprocess.run(
                mkfs_command, stdout=PIPE, stderr=STDOUT, text=True, check=True
            )
            self._logger.info("%s output:\n%s", mkfs_command[0], res.stdout.strip())
        except CalledProcessError as err:
            mkfs_err = format_mkfs_error(mkfs_command[0], err)
            err_msg = "\n".join(
                ("Unable to format file system. Error message:", mkfs_err)
            )
            raise SimulationError(err_msg) from err

    @contextmanager
    def _create_mount_point(self):
        self._logger.info("Creating mount point")
        self._config["mount_point"].mkdir()
        yield
        self._logger.info("Removing mount point")
        shutil.rmtree(self._config["mount_point"])

    @contextmanager
    def _mount_file_system(self, volume):
        self._logger.info("Mounting file system")
        filesystem = self._config["file_system"]["type"]
        # mount_options = "loop,dirsync"
        mount_options = "loop"
        if filesystem in ("fat12", "fat16", "fat32", "ntfs"):
            mount_options = f"{mount_options},uid={os.getuid()}"
        try:
            subprocess.run(
                (
                    "mount",
                    "-o",
                    mount_options,
                    volume.path,
                    self._config["mount_point"],
                ),
                capture_output=True,
                text=True,
                check=True,
            )
            subprocess.run(
                ["chown", "-R", f"{os.getuid()}", self._config["mount_point"]],
                check=True,
            )
        except CalledProcessError as err:
            raise SimulationError(f"Unable to mount image. {err}") from err
        set_simulation_mount_point(self._config["mount_point"])
        yield
        self._logger.info("Unmounting file system")
        set_simulation_mount_point(None)
        try:
            subprocess.run(
                [
                    "umount",
                    "--force",
                    "--detach-loop",
                    self._config["mount_point"],
                ],
                check=True,
            )
        except CalledProcessError as err:
            raise SimulationError(f"Unable to unmount image. {err}") from err

    def _get_mkfs_command(self, path: Path):
        filesystem = self._config["file_system"]["type"]
        try:
            command = {
                "ext2": [
                    "mkfs.ext2",
                ],
                "ext3": [
                    "mkfs.ext3",
                ],
                "ext4": [
                    "mkfs.ext4",
                ],
                "fat12": [
                    "mkfs.fat",
                    "-F",
                    "12",
                ],
                "fat16": [
                    "mkfs.fat",
                    "-F",
                    "16",
                ],
                "fat32": [
                    "mkfs.fat",
                    "-F",
                    "32",
                ],
                "ntfs": ["mkfs.ntfs", "--force"],
            }[filesystem]
            command.append(str(path))
            return command
        except KeyError:
            msg = (
                f'File system "{filesystem}" is currently not supported by the Linux'
                " execution environment"
            )
            raise ConfigurationError(msg) from None


def get_execution_environment(config: Configuration) -> ExecutionEnvironment:
    """Return an execution environment depending on the platform the simulation runs on.

    The function gets the platform the simulation is started on and creates and returns
    an instance of the corresponding execution environment.
    """
    system = get_current_platform()
    if system == Platform.LINUX:
        return LinuxEnvironment(config)
    if system == Platform.WINDOWS:
        return WindowsEnvironment(config)
    raise SimulationError(f'Unsupported platform "{system}"')
