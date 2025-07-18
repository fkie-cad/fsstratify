import logging
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from string import ascii_uppercase

import pytest

from fsstratify.execenv import get_execution_environment
from fsstratify.simulation import Configuration


def list_available_drive_letters():
    if platform.system() != "Windows":
        return []
    res = []
    for letter in ascii_uppercase:
        if not Path(f"{letter}:\\").exists():
            res.append(letter)
    return res


def conf_str_from_args(conf_args):
    return f"""---
seed: {conf_args['seed']}
write_playbook: {conf_args['write_playbook']}

file_system:
  type: {conf_args['file_system']['type']}
  formatting_parameters: "{conf_args['file_system']['formatting_parameters']}"

volume:
  type: {conf_args['volume']['type']}
  keep: {conf_args['volume']['keep']}
  size: {conf_args['volume']['size']}
  force_overwrite: {conf_args['volume']['force_overwrite']}
  win_drive_letter: {conf_args['volume']['win_drive_letter']}

usage_model:
  type: {conf_args['usage_model']['type']}
  parameters: {conf_args['usage_model']['parameters']}
"""


@pytest.fixture(scope="function")
def empty_sim_dir():
    sim_dir = tempfile.mkdtemp()
    yield Path(sim_dir).resolve()
    shutil.rmtree(sim_dir)


@pytest.fixture
def sim_dir_with_dummy_conf(empty_sim_dir):
    conf_args = {
        "seed": "",
        "write_playbook": "",
        "log_level": "warning",
        "file_system": {"type": "", "formatting_parameters": ""},
        "volume": {
            "type": "file",
            "keep": "",
            "size": "",
            "force_overwrite": "",
            "win_drive_letter": "",
        },
        "usage_model": {"type": "", "parameters": ""},
    }
    sim_dir = empty_sim_dir
    conf_str = conf_str_from_args(conf_args)
    conf_path = sim_dir / "simulation.yml"
    conf_path.write_text(conf_str)
    return sim_dir


win_drive_letter = "S"
if platform.system() == "Windows":
    letters = list_available_drive_letters()
    if len(letters) > 0:
        win_drive_letter = letters[0]
arg1 = {
    "seed": "0",
    "write_playbook": "no",
    "file_system": {"type": "ntfs", "formatting_parameters": ""},
    "volume": {
        "type": "file",
        "keep": "no",
        "size": "20MiB",
        "force_overwrite": "no",
        "win_drive_letter": win_drive_letter,
    },
    "usage_model": {"type": "DUMMY_VAL", "parameters": "DUMMY_VAL"},
}
arg2 = dict(arg1)
arg2["keep"] = "no"


# TODO
@pytest.mark.parametrize("conf_args", [arg1, arg2])
def xtest_execenv_setup_and_cleanup(empty_sim_dir, conf_args):
    sim_dir = empty_sim_dir
    conf_str = conf_str_from_args(conf_args)
    conf = Configuration()
    conf.load_str(conf_str, sim_dir)
    mnt = sim_dir / "mnt"

    if platform.system() == "Windows":
        letter = conf["volume"]["win_drive_letter"]
        if Path(f"{letter}:\\").exists():
            raise RuntimeError(f"Requested drive letter {letter} not available")
        disk_image = sim_dir / "fs.vhd"
    elif platform.system() == "Linux":
        disk_image = sim_dir / "fs.img"
    else:
        raise RuntimeError("Invalid plattform!")

    env = get_execution_environment(conf)

    # check logger handlers
    execenv_log_hdlrs = list(logging.getLogger("ExecutionEnvironment").handlers)
    # assert that the two logger handlers are used for each execution environment
    assert len(execenv_log_hdlrs) == 2
    # assert that one log handler is FileHandler for log file in simulation directory
    log_path = (sim_dir / "simulation.log").resolve()
    assert any(
        isinstance(h, logging.FileHandler) and h.baseFilename == str(log_path)
        for h in execenv_log_hdlrs
    )

    with env as execenv:
        # assert that persistent env elements exist
        assert mnt.exists()
        assert disk_image.exists()

        if platform.system() == "Windows":
            # assert vhd and mnt are connected as desired
            sub = subprocess.run(
                (
                    "powershell",
                    "-Command",
                    f'Write-Output (Get-Volume -FilePath "{mnt}" | Get-DiskImage).ImagePath',
                ),
                check=True,
                capture_output=True,
                encoding="utf8",
            )
            assert Path(sub.stdout[:-1]) == disk_image
        elif platform.system() == "Linux":
            # check if sim_dir is mounted
            assert sim_dir.is_mount()

        # assert that executing any file operation in mnt is possible
        try:
            Path(mnt / "testfile").touch()
        except RuntimeError:
            assert False

    # assert that mnt is removed
    assert not mnt.exists()

    if conf["volume"]["keep"]:
        # assert that image still exists
        assert disk_image.exists()
        # assert that image can be removed without problems
        try:
            disk_image.unlink()
        except RuntimeError:
            assert False
    # assert that image does not exist
    assert not disk_image.exists()
