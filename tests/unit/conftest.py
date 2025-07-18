from pathlib import Path

import pytest

HERE = Path(__file__).absolute().parent
DATA_FILES = HERE.parent / "data"


@pytest.fixture
def test_data_path():
    return Path(DATA_FILES)
