"""This module contains various helper and utility functions."""

import ctypes
import logging
import os
import random
import re
import string
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile
from textwrap import indent
from typing import Collection, List, Tuple, Optional

import numpy as np
import portion

from fsstratify.platforms import get_current_platform, Platform


def get_random_string(length: int) -> str:
    return "".join(random.choice(string.ascii_lowercase) for _ in range(length))


def parse_size_definition(size_str: str) -> int:
    """Parse a size definition string and return the number of bytes.

    This functions parses size string like "13 GiB" and returns the corresponding
    number of bytes. Supported size strings are:
      - just numbers
      - k, kB -> number * 1000
      - M, MB -> number * 1000^2
      - G, GB -> number * 1000^3
      - T, TB -> number * 1000^4
      - Ki, KiB -> number * 1024
      - Mi, MiB -> number * 1024^2
      - Gi, GiB -> number * 1024^3
      - Ti, TiB -> number * 1024^4

    Args:
        size_str: The string to parse.
    """
    suffixes = {
        "k": "kB",
        "kB": "kB",
        "M": "MB",
        "MB": "MB",
        "G": "GB",
        "GB": "GB",
        "T": "TB",
        "TB": "TB",
        "Ki": "KiB",
        "KiB": "KiB",
        "Mi": "MiB",
        "MiB": "MiB",
        "Gi": "GiB",
        "GiB": "GiB",
        "Ti": "TiB",
        "TiB": "TiB",
    }
    suffix_to_factor = {
        "kB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
        "TiB": 1024**4,
    }
    tokens = re.split(r"(\d+)", size_str.strip())
    if len(tokens) != 3 or tokens[0] != "":
        raise ValueError(f"Invalid size definition: {size_str}")
    value = int(tokens[1])
    unit = tokens[2].strip()
    if unit == "":
        return value
    if unit not in suffixes:
        raise ValueError(f"Invalid unit for size definition: {unit}")
    return value * suffix_to_factor[suffixes[unit]]


def parse_duration_string(duration_str: str) -> int:
    """Parse a duration string and return the number of seconds.

    This functions duration string like "1m" and returns the corresponding
    number of seconds. Supported strings are:
      - just numbers
      - s -> number
      - min -> number * 60
      - h -> number * 3600

    Args:
        duration_str: The string to parse.
    """
    suffix_factors = {
        "s": 1,
        "min": 60,
        "h": 3600,
    }
    tokens = re.split(r"(\d+)", duration_str.strip())
    if len(tokens) != 3 or tokens[0] != "":
        raise ValueError(f"Invalid size definition: {duration_str}")
    value = int(tokens[1])
    unit = tokens[2].strip()
    if unit == "":
        return value
    if unit not in suffix_factors:
        raise ValueError(f"Invalid unit for size definition: {unit}")
    return value * suffix_factors[unit]


def parse_boolean_string(bool_str: str) -> bool:
    """Parse a string indicating a True or False value."""
    try:
        bool_str = bool_str.lower()
    except AttributeError:
        raise ValueError(f'"{bool_str}" is neither a value for True nor for False.')
    if bool_str in ("true", "yes", "y"):
        return True
    if bool_str in ("false", "no", "n"):
        return False
    raise ValueError(f'"{bool_str}" is neither a value for True nor for False.')


def merge_blocks_to_fragments(block_list: Collection) -> list:
    fragments = []
    if len(block_list) == 0:
        return fragments
    for fragment in np.split(block_list, np.where(np.diff(block_list) != 1)[0] + 1):
        if len(fragment) > 1:
            fragments.append((fragment[0], fragment[-1]))
        else:
            fragments.append((fragment[0], fragment[0]))
    return fragments


def merge_overlapping_fragments(
    fragments: List[Tuple[int, int]],
) -> List[Tuple[int, int]]:
    """Merge overlapping and consecutive intervals."""
    if len(fragments) == 0:
        return fragments
    intervals = []
    for frag in sorted(fragments):
        if len(intervals) > 0:
            if frag[0] == intervals[-1][1] + 1:
                intervals.append((frag[0] - 1, frag[1]))
            else:
                intervals.append(frag)
        else:
            intervals.append(frag)

    intervals = portion.Interval(*(portion.closed(*i) for i in intervals))

    return [(i.lower, i.upper) for i in intervals]


_LOG_MESSAGE_FORMAT = "%(asctime)-15s - %(name)s - %(levelname)s - %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_LOG_LEVELS = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "info": logging.INFO,
}


def get_logger(name: str, loglevel: str, logfile: Path) -> logging.Logger:
    """Returns a readily configured logger instance.

    The logger instance will be configured with two handlers: one will log to a log
    file and the other to the terminal. The log level will only be used to configure
    the terminal logger, the log file handler always logs with log level "info".

    Args:
        name: Name of the logger instance.
        loglevel: The log level to use for the logger instance.
        logfile: Path of the log file to log to.
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    file_log_formatter = logging.Formatter(
        fmt=_LOG_MESSAGE_FORMAT, datefmt=_LOG_DATE_FORMAT
    )
    file_log = logging.FileHandler(logfile)
    file_log.setLevel(logging.INFO)
    file_log.setFormatter(file_log_formatter)

    console_formatter = logging.Formatter(
        fmt="[+] %(message)s", datefmt=_LOG_DATE_FORMAT
    )
    console_log = logging.StreamHandler()
    console_log.setLevel(_LOG_LEVELS[loglevel])
    console_log.setFormatter(console_formatter)

    logger.addHandler(file_log)
    logger.addHandler(console_log)
    return logger


def run_diskpart_script(cmds: str):
    with NamedTemporaryFile(mode="w+", delete=False) as script:
        script_path = Path(script.name)
        script.write(cmds)
        script.flush()
        os.fsync(script.fileno())
        sub = subprocess.run(f'diskpart /s "{script.name}"', shell=True, check=True)
    script_path.unlink()


def run_powershell_script(script: str, check=False) -> subprocess.CompletedProcess:
    """Run the given string as PowerShell script."""
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        check=check,
        capture_output=True,
        text=True,
    )


def is_user_admin():
    if get_current_platform() == Platform.LINUX:
        return os.getuid() == 0
    elif get_current_platform() == Platform.WINDOWS:
        return ctypes.windll.shell32.IsUserAnAdmin()
    else:
        raise NotImplementedError


def format_mkfs_error(mkfs_command: str, error: subprocess.CalledProcessError) -> str:
    return indent(
        "\n".join((str(error), f"{mkfs_command} output:\n{error.stdout.strip()}")),
        "    ",
    )


def parse_pattern_format_string(format_string: str) -> Tuple[str, str, str] | None:
    match = re.match(r"pattern\((\d+),\s*(.+?),\s*(.*?)\)", format_string)

    if match:
        try:
            number = int(match.group(1))
            format_string = match.group(2)
            text = match.group(3)
            return number, format_string, text
        except ValueError:
            # Handle cases where the first group isn't a valid integer
            return None
    else:
        return None


def split_on_first_and_last(s: str, sep: str) -> Tuple[str, str, str]:
    first = s.find(sep)
    last = s.rfind(sep)
    if first == -1 or last == -1 or first == last:
        raise ValueError(f"Error: Not enough occurrences of separator '{sep}'.")
    part1 = s[:first]
    part2 = s[first + 1 : last]
    part3 = s[last + 1 :]
    return part1, part2, part3


def extract_from_parentheses(s: str) -> str:
    left = s.find("(")
    right = s.rfind(")")
    if left == -1 or right == -1:
        raise ValueError("Error: String must contain at least one '(' and one ')'.")
    if left >= right:
        raise ValueError("Error: Parentheses are unbalanced or in the wrong order.")
    return s[left + 1 : right]


def parse_format_string(fmt: str):
    if fmt.count("%S") > 1:
        raise ValueError("Error: '%S' is allowed only once.")
    pattern = r"%(?!%)[%cfFsS]"
    matches = list(re.finditer(pattern, fmt))
    for match in matches:
        specifier = match.group()
        if specifier not in ("%c", "%f", "%F", "%s", "%S"):
            raise ValueError(f"Error: Invalid format specifier: {specifier}.")
    return [(m.start(), m.end(), m.group()) for m in matches]
