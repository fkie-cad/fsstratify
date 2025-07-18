import pytest

from pathlib import Path
import shutil
import platform

from attr import define, field
from enum import Enum

from fsstratify.volumes import WindowsRawDiskImage
from fsstratify.filesystems import SimulationVirtualFileSystem, FileType, File

num_rand_tests = 20  # number of times assertions are checked for randomized functions


class ControlFileType(Enum):
    """This enum represents the different test file type."""

    REGULAR = 1
    DIRECTORY = 2


@define
class ControlFile:
    size: int
    type: ControlFileType
    path: Path = field(converter=Path)

    def createFile(self):
        """This function transforms a test file to a file as being used in fsstratify and should be the only dependence of test code on the classes File and FileType"""
        if self.type == ControlFileType.REGULAR:
            t = FileType.REGULAR
        else:
            t = FileType.DIRECTORY
        return File(type=t, path=self.path)


def transformControlFiles(files):
    return [f.createFile() for f in files]


@pytest.fixture
def windows_ntfs_volume_detached_dummy(test_data_path, tmp_path):
    """returns dict {'volume':WindowsRawDiskImage, 'active_user_files': [_]}
    with WindowsRawDiskImage-object being based upon the test image test_windows_ntfs.vhd in data folder. This fixture does not create a whole execution environment, instead the image remains completely detached from the system. This makes the test code iself less error-prone and avoids reimplementing functions from fsstratify that create an execution environment. All info about the test image is hardcoded in this fixture, also to avoid reimplementing fsstratify functions that retrieve those information from the image. Therefore, the test image info in this fixture should always be up-to-date with changes that were applied to the test image itself. F.e. if someone mounts the test image and adds new files, the list about active files in this fixture must be updated before any test is performed. Otherwise, the whole test code breaks as fsstratify works with the updated image, whereas the test code does not know anything about the updates.
    """
    vol_name = "test_windows_ntfs.vhd"
    original_vhd_path = test_data_path / vol_name
    shutil.copy(original_vhd_path, tmp_path)
    vhd_path = tmp_path / vol_name
    vol_conf = {
        "type": "file",
        "keep": True,
        "size": 19853312,
        "force_overwrite": True,
        "drive_letter": "S",
        "directory": tmp_path,
        "path": vhd_path,
    }

    active_user_files = [
        ControlFile(path="testfile_1.txt", type=ControlFileType.REGULAR, size=18),
        ControlFile(path="testfile_2.txt", type=ControlFileType.REGULAR, size=18),
        ControlFile(path="testfile_3.txt", type=ControlFileType.REGULAR, size=167200),
        ControlFile(path="directory_1", type=ControlFileType.DIRECTORY, size=18),
        ControlFile(
            path="directory_1/testfile_4.txt", type=ControlFileType.REGULAR, size=18
        ),
    ]
    deleted_user_files = [
        ControlFile(
            path="deleted_testfile_1.txt", type=ControlFileType.REGULAR, size=10
        )
    ]
    yield {
        "volume": WindowsRawDiskImage(vol_conf),
        "active_user_files": active_user_files,
        "deleted_user_files": deleted_user_files,
    }
    shutil.rmtree(tmp_path)


@pytest.fixture
def unused_windows_ntfs_volume_detached_dummy(test_data_path, tmp_path):
    """Same concept as described in fixture windows_ntfs_volume_detached_dummy, but tested filesystem is unused and empty."""
    vol_name = "test_windows_ntfs_unused.vhd"
    original_vhd_path = test_data_path / vol_name
    shutil.copy(original_vhd_path, tmp_path)
    vhd_path = tmp_path / vol_name
    vol_conf = {
        "type": "file",
        "keep": True,
        "size": 4124672,
        "force_overwrite": True,
        "drive_letter": "S",
        "directory": tmp_path,
        "path": vhd_path,
    }
    active_user_files = []
    deleted_user_files = []
    yield {
        "volume": WindowsRawDiskImage(vol_conf),
        "active_user_files": active_user_files,
        "deleted_user_files": deleted_user_files,
    }
    shutil.rmtree(tmp_path)


@pytest.fixture
def sim_vfs(windows_ntfs_volume_detached_dummy):
    sim_vfs = dict(windows_ntfs_volume_detached_dummy)
    sim_vfs["sim_vfs"] = SimulationVirtualFileSystem(
        windows_ntfs_volume_detached_dummy["volume"]
    )
    return sim_vfs


@pytest.fixture
def unused_sim_vfs(unused_windows_ntfs_volume_detached_dummy):
    unused_sim_vfs = dict(unused_windows_ntfs_volume_detached_dummy)
    unused_sim_vfs["sim_vfs"] = SimulationVirtualFileSystem(
        unused_windows_ntfs_volume_detached_dummy["volume"]
    )
    return unused_sim_vfs


@pytest.mark.skipif(
    platform.system() != "Windows", reason="Only implemented for Windows"
)
class TestSimulationVirtualFilesystemMethods:
    @pytest.fixture(autouse=True, params=["sim_vfs", "unused_sim_vfs"])
    def _request_sim_vfs(self, request):
        self._sim_vfs_info = request.getfixturevalue(request.param)
        self._sim_vfs = self._sim_vfs_info["sim_vfs"]
        self._active_user_files = self._sim_vfs_info["active_user_files"]

    def test_get_files(self):
        output_to_test = self._sim_vfs.get_files()
        files_to_check = transformControlFiles(self._active_user_files)
        assert len(output_to_test) == len(files_to_check)
        for f in output_to_test:
            assert f in files_to_check

    def test_get_allocated_fragments_for_file(self):
        for f in self._active_user_files:
            output_to_test = self._sim_vfs.get_allocated_fragments_for_file(f.path)
            assert output_to_test is not None
            assert type(output_to_test) == list

    def test_get_rel_space_usg(self):
        pass  # TODO

    def test_get_random_file(self):
        active_regular_file_paths = [
            f.path for f in self._active_user_files if f.type == ControlFileType.REGULAR
        ]
        if active_regular_file_paths == []:
            return
        for _ in range(num_rand_tests):
            assert self._sim_vfs.get_random_file() in active_regular_file_paths

    def test_get_random_directory(self):
        active_directory_paths = [
            f.path
            for f in self._active_user_files
            if f.type == ControlFileType.DIRECTORY
        ]
        if active_directory_paths == []:
            return
        for _ in range(num_rand_tests):
            assert self._sim_vfs._get_random_directory() in active_directory_paths

    def test_get_random_path(self):
        active_paths = [f.path for f in self._active_user_files]
        for _ in range(num_rand_tests):
            path_to_test = self._sim_vfs.get_nonexistent_path()
            assert path_to_test not in active_paths
            assert path_to_test.parent in active_paths or path_to_test.parent == Path(
                "/"
            )

    def test_empty(self):
        assert self._sim_vfs.empty() == (len(self._active_user_files) == 0)

    def test_regular_file_count(self):
        assert (
            len(
                [
                    f
                    for f in transformControlFiles(self._active_user_files)
                    if f.type == FileType.REGULAR
                ]
            )
            == self._sim_vfs.file_count()
        )

    def test_directory_count(self):
        assert (
            len(
                [
                    f
                    for f in transformControlFiles(self._active_user_files)
                    if f.type == FileType.DIRECTORY
                ]
            )
            == self._sim_vfs.directory_count()
        )

    def test_get_count_of(self):
        assert len(
            [
                f
                for f in transformControlFiles(self._active_user_files)
                if f.type == FileType.REGULAR
            ]
        ) == self._sim_vfs.get_count_of(FileType.REGULAR)
        assert len(
            [
                f
                for f in transformControlFiles(self._active_user_files)
                if f.type == FileType.DIRECTORY
            ]
        ) == self._sim_vfs.get_count_of(FileType.DIRECTORY)
