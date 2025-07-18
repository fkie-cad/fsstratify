from pathlib import Path

import pytest

from fsstratify.errors import SimulationError, PlaybookError
from fsstratify.filesystems import set_simulation_mount_point
from fsstratify.operations import Sleep


@pytest.mark.parametrize(
    "line,expected",
    (
        ("sleep 5s", {"command": "sleep", "duration": 5}),
        ("sleep 1min", {"command": "sleep", "duration": 60}),
        ("sleep 2min", {"command": "sleep", "duration": 120}),
        ("sleep 1h", {"command": "sleep", "duration": 3600}),
    ),
)
def test_that_a_valid_playbook_line_is_parsed_correctly(line, expected):
    assert Sleep.from_playbook_line(line).as_dict() == expected


@pytest.mark.parametrize(
    "op,expected",
    (
        (
            Sleep(duration=12345),
            {"command": "sleep", "duration": 12345},
        ),
        (
            Sleep(duration=42),
            {"command": "sleep", "duration": 42},
        ),
    ),
)
def test_that_as_dict_works(op, expected):
    assert op.as_dict() == expected


@pytest.mark.parametrize(
    "op,expected",
    (
        (
            Sleep(duration=1),
            "sleep 1",
        ),
        (
            Sleep(duration=42),
            "sleep 42",
        ),
    ),
)
def test_that_as_playbook_line_works(op: Sleep, expected: str):
    assert op.as_playbook_line() == expected


@pytest.mark.parametrize(
    "line",
    (
        "sleep",
        "sleep 5 m",
        "sleep a min",
        "sleep a m",
        "sleep min 5",
    ),
)
def test_that_invalid_parameters_raise(line: str):
    with pytest.raises(PlaybookError):
        Sleep.from_playbook_line(line).as_dict()
