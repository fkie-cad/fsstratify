"""Unit tests for ``Simulation._get_stratum``.

Before the fix, per-file timestamps were written to a single stratum-level
``timestamps`` key that was overwritten on every loop iteration, so a multi-file
operation kept only the *last* file's timestamps and they were not attached to the
per-file record. These tests pin the fixed behaviour: timestamps live inside each
``affected_files`` entry, keyed implicitly to that entry's ``path``.

``_get_stratum`` is exercised in isolation against a fake VFS — no root, no real image.
"""

from datetime import datetime
from pathlib import Path

import pytest

from fsstratify.operations import Remove
from fsstratify.simulation import Simulation


class FakeVfs:
    """Minimal stand-in exposing only what ``_get_stratum`` calls.

    Returns a *distinct* allocation and timestamp dict per path so the test can prove
    each ``affected_files`` entry carries its own data (no overwrite / mis-keying).
    """

    def __init__(self, files):
        self._files = list(files)

    def get_files_below(self, target):
        return list(self._files)

    def get_allocated_fragments_for_file(self, path):
        return [(hash(str(path)) % 1000, hash(str(path)) % 1000 + 1)]

    def get_timestamps_for_file(self, path):
        return {"modified": f"ts-for-{path}"}

    def get_file_system_areas(self):
        return [(0, 7)]


class FakeMultiFileOp:
    """A non-Remove/Time operation touching several files (e.g. a cp of a tree)."""

    def __init__(self, target):
        self.target = target

    def as_dict(self):
        return {"command": "cp", "src": "/src", "dst": str(self.target)}


def _make_simulation(vfs, write_timestamps=True):
    sim = Simulation.__new__(Simulation)  # bypass __init__; isolate _get_stratum
    sim._vfs = vfs
    sim._config = {"write_timestamps": write_timestamps}
    return sim


THREE_FILES = [Path("/dir/a.txt"), Path("/dir/b.txt"), Path("/dir/sub/c.txt")]


def test_multifile_op_keeps_every_files_timestamps_keyed_to_path():
    """Each affected_files entry has its own correct timestamps."""
    vfs = FakeVfs(THREE_FILES)
    sim = _make_simulation(vfs, write_timestamps=True)

    stratum = sim._get_stratum(FakeMultiFileOp(Path("/dir")))

    entries = stratum["affected_files"]
    assert len(entries) == len(THREE_FILES)
    for entry in entries:
        # The timestamps must belong to *this* entry's path, not the last file's.
        assert entry["timestamps"] == {"modified": f"ts-for-{entry['path']}"}
    # All three are distinct -> nothing was overwritten.
    assert len({e["timestamps"]["modified"] for e in entries}) == 3


def test_no_stratum_level_timestamps_key():
    vfs = FakeVfs(THREE_FILES)
    sim = _make_simulation(vfs, write_timestamps=True)

    stratum = sim._get_stratum(FakeMultiFileOp(Path("/dir")))

    assert "timestamps" not in stratum  # only per-entry, never stratum-level


def test_write_timestamps_disabled_omits_timestamps():
    vfs = FakeVfs(THREE_FILES)
    sim = _make_simulation(vfs, write_timestamps=False)

    stratum = sim._get_stratum(FakeMultiFileOp(Path("/dir")))

    for entry in stratum["affected_files"]:
        assert set(entry) == {"path", "allocated_areas"}
        assert "timestamps" not in entry


def test_remove_records_removed_at_and_no_affected_files():
    vfs = FakeVfs(THREE_FILES)
    sim = _make_simulation(vfs, write_timestamps=True)

    stratum = sim._get_stratum(Remove(Path("/dir/a.txt")))

    assert "affected_files" not in stratum
    assert "timestamp" not in stratum  # old, ambiguous key is gone
    assert "timestamps" not in stratum
    # New, clearly-named wall-clock key: tz-aware UTC ISO-8601.
    parsed = datetime.fromisoformat(stratum["removed_at"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_remove_without_timestamps_has_no_removed_at():
    vfs = FakeVfs(THREE_FILES)
    sim = _make_simulation(vfs, write_timestamps=False)

    stratum = sim._get_stratum(Remove(Path("/dir/a.txt")))

    assert "removed_at" not in stratum


def test_status_defaults_to_ok():
    """A clean step is flagged ok so it is distinguishable from a partial one."""
    vfs = FakeVfs(THREE_FILES)
    sim = _make_simulation(vfs)

    stratum = sim._get_stratum(FakeMultiFileOp(Path("/dir")))

    assert stratum["status"] == "ok"


def test_status_disk_full_is_recorded():
    """A step continued after ENOSPC is marked disk_full."""
    vfs = FakeVfs(THREE_FILES)
    sim = _make_simulation(vfs)

    stratum = sim._get_stratum(FakeMultiFileOp(Path("/dir")), status="disk_full")

    assert stratum["status"] == "disk_full"


def test_status_is_present_on_remove_strata_too():
    """The status field is uniform across every stratum, not just write ops."""
    vfs = FakeVfs(THREE_FILES)
    sim = _make_simulation(vfs)

    stratum = sim._get_stratum(Remove(Path("/dir/a.txt")), status="disk_full")

    assert stratum["status"] == "disk_full"
