from functools import partial
from itertools import cycle, islice
from pathlib import Path
from random import randbytes
from typing import Optional

from fsstratify.errors import SimulationError, PlaybookError
from fsstratify.utils import (
    extract_from_parentheses,
    split_on_first_and_last,
    parse_format_string,
)


class DataGenerator:
    def __init__(self, size_hint: Optional[int] = None):
        self._size_hint = size_hint

    def generate(self, size: int) -> bytes:
        if size < 0:
            raise SimulationError(
                "Error: Number of bytes to generate must not be negative."
            )
        return self._generate(size)

    def _generate(self, size: int) -> bytes:
        raise NotImplementedError

    def as_playbook_string(self) -> str:
        raise NotImplementedError


class RandomDataGenerator(DataGenerator):
    def _generate(self, size: int) -> bytes:
        return randbytes(size)

    def as_playbook_string(self) -> str:
        return "random()"


class PatternGenerator(DataGenerator):
    def __init__(
        self,
        pattern_width: int,
        format_str: str,
        static_str: str,
        filename: Optional[Path] = None,
        size_hint: Optional[int] = 512,
    ):
        super().__init__(size_hint)
        self._pattern_width = pattern_width
        self._format_str = format_str
        self._static_str = static_str
        self._filename = filename

        self._pattern_counter = 0
        self._specifiers = parse_format_string(self._format_str)
        self._filler_str_present = any(item[2] == "%S" for item in self._specifiers)
        self._chunk = bytes()
        self._chunk_index = 0

        if (
            any(item[2].lower() == "%f" for item in self._specifiers)
            and self._filename is None
        ):
            raise SimulationError("Error: %for %F in format string but no path given.")

    def as_playbook_string(self) -> str:
        return f"pattern({self._pattern_width},{self._format_str},{self._static_str})"

    def _generate(self, size: int) -> bytes:
        data = ""
        while len(data) < size:
            if self._chunk_index >= len(self._chunk):
                self._chunk = self._generate_pattern()
                self._chunk_index = 0
            bytes_needed = size - len(data)
            data += self._chunk[self._chunk_index : bytes_needed]
            self._chunk_index += bytes_needed
        return bytes(data, encoding="utf-8")

    def _interpolate_static_format_str_parts(self) -> str:
        segments = []
        last_pos = 0
        for start, end, spec in parse_format_string(self._format_str):
            segments.append(self._format_str[last_pos:start])
            if spec == "%c":
                segments.append(str(self._pattern_counter))
            elif spec == "%f":
                segments.append(self._filename.name)
            elif spec == "%F":
                segments.append(self._filename)
            elif spec == "%s":
                segments.append(self._static_str)
            elif spec == "%S":
                segments.append("%S")
            last_pos = end
        segments.append(self._format_str[last_pos:])
        return "".join(segments)

    def _generate_pattern(self):
        if self._filler_str_present:
            s = self._interpolate_static_format_str_parts()
            filler_pos = s.find("%S")
            filler_space = self._pattern_width - (len(s) - 2)
            if filler_space <= len(self._static_str):
                s = "".join((s[:filler_pos], self._static_str, s[filler_pos + 2 :]))
            else:
                filler_str = "".join(islice(cycle(self._static_str), filler_space))
                s = "".join((s[:filler_pos], filler_str, s[filler_pos + 2 :]))
            self._pattern_counter += 1
            return s[: self._pattern_width]
        else:
            s = self._interpolate_static_format_str_parts()
            while len(s) < self._pattern_width:
                s += self._interpolate_static_format_str_parts()
            self._pattern_counter += 1
            return s[: self._pattern_width]

    @classmethod
    def from_playbook_string(cls, playbook_str: str, filename: Optional[Path] = None):
        parameters = extract_from_parentheses(playbook_str)
        pattern_width, format_str, static_str = split_on_first_and_last(parameters, ",")
        pattern_width = int(pattern_width)
        return partial(cls, pattern_width, format_str, static_str, filename)


def from_playbook_string(
    data_generator_str: str, filename: Path
) -> partial[PatternGenerator]:
    if data_generator_str.startswith("pattern("):
        return PatternGenerator.from_playbook_string(data_generator_str, filename)
    else:
        raise PlaybookError(
            f"Error: Unsupported data generator definition: '{data_generator_str}'."
        )


if __name__ == "__main__":
    dg = PatternGenerator(512, "%f_%c_%s", "O", Path("test.txt"))
    print(dg.generate(40))
    print(dg.generate(40))
    print(dg.generate(28))

    dg = PatternGenerator(512, "%f_%c_%S", "O", Path("test.txt"))
    print(dg.generate(40))
    print(dg.generate(40))
    print(dg.generate(28))

    dg = PatternGenerator(11, "%S_%c|", "IG", Path("test.txt"))
    print(dg.generate(40))
    print(dg.generate(40))
    print(dg.generate(28))
"""
        pattern_width: int,
        format_str: str,
        static_str: str,
        filename: Optional[str] = "",
        size_hint: Optional[int] = None,


    dg = PatternGenerator("A", filename=pathlib.Path("test.txt"), pattern_chunk_size=20)
    print(dg.generate(40))
    print(dg.generate(40))
    print(dg.generate(28))

    dg = PatternGenerator("", filename=pathlib.Path("test.txt"), pattern_chunk_size=20)
    print(dg.generate(40))
    print(dg.generate(40))
    print(dg.generate(28))

    dg = PatternGenerator(
        "",
        filename=pathlib.Path("test.txt"),
        pattern_chunk_size=20,
        include_pattern_chunk_counter=False,
    )
    print(dg.generate(40))
    print(dg.generate(40))
    print(dg.generate(28))

    dg = PatternGenerator("", filename=pathlib.Path(""), pattern_chunk_size=20)
    print(dg.generate(40))
    print(dg.generate(40))
    print(dg.generate(28))

    dg = PatternGenerator(
        "ABC",
        filename=pathlib.Path(""),
        pattern_chunk_size=20,
        include_pattern_chunk_counter=False,
    )
    print(dg.generate(40))
    print(dg.generate(40))
    print(dg.generate(28))

    dg = PatternGenerator(
        "",
        filename=pathlib.Path("tests/test.txt"),
        pattern_chunk_size=20,
        include_pattern_chunk_counter=False,
    )
    print(dg.generate(40))
    print(dg.generate(40))
    print(dg.generate(28))
"""
