import pytest
from unittest.mock import patch

from fsstratify.platforms import Platform, get_current_platform
from fsstratify.errors import SimulationError


class TestGetCurrentPlatform:
    def test_that_linux_is_detected(self):
        with patch("platform.system", return_value="Linux"):
            assert get_current_platform() == Platform.LINUX

    def test_that_windows_is_detected(self):
        with patch("platform.system", return_value="Windows"):
            assert get_current_platform() == Platform.WINDOWS

    def test_that_unsupported_platform_raises_error(self):
        with patch("platform.system", return_value="Darwin"):
            with pytest.raises(SimulationError):
                get_current_platform()

        with patch("platform.system", return_value="Unknown"):
            with pytest.raises(SimulationError):
                get_current_platform()
