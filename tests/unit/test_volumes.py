"""Unit tests for the volume flush barrier.

These tests are root-free: they construct the volume objects with a minimal
config dict and stub out the actual ``sync`` / ``Write-VolumeCache`` calls, so
they assert *which* barrier command is issued and that it is checked, without
ever formatting or mounting anything.
"""

import subprocess
from pathlib import Path

import pytest

from fsstratify import volumes
from fsstratify.errors import VolumeError
from fsstratify.utils import parse_size_definition
from fsstratify.volumes import LinuxRawDiskImage, WindowsRawDiskImage


def _linux_config(tmp_path: Path) -> dict:
    return {
        "mount_point": tmp_path / "mnt",
        "volume": {"directory": str(tmp_path), "keep": True},
    }


def _windows_config(tmp_path: Path) -> dict:
    return {
        "mount_point": tmp_path / "mnt",
        "volume": {
            "directory": str(tmp_path),
            "keep": True,
            "size": parse_size_definition("20MiB"),
        },
    }


@pytest.fixture
def capture_run(monkeypatch):
    """Capture calls to ``subprocess.run`` made from the volumes module."""
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(volumes.subprocess, "run", fake_run)
    return calls


def test_linux_flush_targets_mounted_filesystem(tmp_path, monkeypatch, capture_run):
    """When mounted, flush issues a checked ``sync -f <mount_point>``."""
    volume = LinuxRawDiskImage(_linux_config(tmp_path))
    monkeypatch.setattr(Path, "is_mount", lambda self: True)

    volume.flush()

    assert len(capture_run) == 1
    args, kwargs = capture_run[0]
    assert args[0] == ["sync", "-f", str(volume.mount_point)]
    assert kwargs.get("check") is True


def test_linux_flush_global_sync_before_mount(tmp_path, capture_run):
    """Before the file system is mounted, flush falls back to a checked global sync."""
    volume = LinuxRawDiskImage(_linux_config(tmp_path))
    # The mount point does not exist yet, so is_mount() is False.
    assert not Path(volume.mount_point).is_mount()

    volume.flush()

    assert len(capture_run) == 1
    args, kwargs = capture_run[0]
    assert args[0] == ["sync"]
    assert kwargs.get("check") is True


def test_linux_flush_propagates_sync_failure(tmp_path, monkeypatch):
    """A failed sync must surface, not be swallowed (avoids a stale stratum)."""
    volume = LinuxRawDiskImage(_linux_config(tmp_path))

    def failing_run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(volumes.subprocess, "run", failing_run)

    with pytest.raises(subprocess.CalledProcessError):
        volume.flush()


def test_windows_flush_requires_drive_letter(tmp_path, monkeypatch):
    """Flushing before a drive letter is assigned raises instead of issuing a
    malformed ``Write-VolumeCache -DriveLetter`` command."""
    volume = WindowsRawDiskImage(_windows_config(tmp_path))
    assert volume.drive_letter == ""

    called = []
    monkeypatch.setattr(
        volumes, "run_powershell_script", lambda *a, **k: called.append((a, k))
    )

    with pytest.raises(VolumeError):
        volume.flush()
    assert called == []


def test_windows_flush_uses_drive_letter(tmp_path, monkeypatch):
    """With a drive letter set, flush issues a checked Write-VolumeCache for it."""
    volume = WindowsRawDiskImage(_windows_config(tmp_path))
    volume.drive_letter = "S"

    calls = []
    monkeypatch.setattr(
        volumes, "run_powershell_script", lambda *a, **k: calls.append((a, k))
    )

    volume.flush()

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert "Write-VolumeCache -DriveLetter S" in args[0]
    assert kwargs.get("check") is True
