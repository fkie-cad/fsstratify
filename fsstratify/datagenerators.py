import pathlib
from itertools import chain, repeat, cycle
from pathlib import Path
from random import randbytes
from typing import Optional

from fsstratify.errors import SimulationError


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


class RandomDataGenerator(DataGenerator):
    def _generate(self, size: int) -> bytes:
        return randbytes(size)


class StaticStringGenerator(DataGenerator):
    def __init__(self, s: str, size_hint: Optional[int] = None):
        super().__init__(size_hint)
        self.pattern = s
        self._value = cycle(bytes(s, encoding="utf-8"))

    def _generate(self, size: int) -> bytes:
        return bytes(next(self._value) for _ in range(size))


class PatternGenerator(DataGenerator):
    def __init__(
        self,
        s: str,
        size_hint: Optional[int] = None,
        filename: Path = "",
        pattern_chunk_size: Optional[int] = 512,
        include_pattern_chunk_counter: Optional[bool] = True,
        pattern_string: str = True,
    ):
        super().__init__(size_hint)
        self.pattern = s
        self._value = cycle(bytes(s, encoding="utf-8"))
        self._pattern_chunk_size = pattern_chunk_size
        self._filename = filename
        self._pattern_chunk_counter = 0
        self._include_pattern_chunk_counter = include_pattern_chunk_counter
        self.pattern_string = pattern_string

    def _generate(self, size: int) -> bytes:
        data = bytes()
        while len(data) < size:
            data += self._generate_pattern()
            self._pattern_chunk_counter += 1
        return data[:size]

    def _generate_pattern(self):
        """
        Returns chunk data based on the pattern format specifier.
        E.g.:
        %s - repeated pattern without any IDs included
        %f%c%s - file name and chunk counter are included at the beginning of each chunk, the rest filled with pattern (refined paper version)
        %f - repeated filename
        %f%c - repeated filename-chunk counter combination
        %c - repeated counter
        """
        chunk_id = self._create_pattern_chunk_id()
        if self.pattern == "":
            return self._repeat(chunk_id, self._pattern_chunk_size)
        pattern_data_size = self._pattern_chunk_size - len(chunk_id)
        pattern_data = self._repeat(self.pattern, pattern_data_size)
        return chunk_id.encode("utf-8") + pattern_data

    def _create_pattern_chunk_id(self) -> str:
        """
        Creates chunk id based on specified pattern.
        E.g. %f%c -> test.txt2 (filename+counter)
        If these specifiers are missing, chunk id is empty string.
        """
        chunk_id = self._filename.name
        if self._include_pattern_chunk_counter:
            chunk_id = f"{chunk_id}{self._pattern_chunk_counter}"
        return chunk_id

    @staticmethod
    def _repeat(s, size):
        value = cycle(bytes(s, encoding="utf-8"))
        return bytes(next(value) for _ in range(size))


if __name__ == "__main__":
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
