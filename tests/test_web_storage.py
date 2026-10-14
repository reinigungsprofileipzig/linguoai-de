import io
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock

import pytest

from linguoai.errors import ConfigurationError, FileOperationError
from linguoai.web_storage import DriveStorage, LocalStorage


def test_local_storage_opaque_unique_refs_and_roundtrip(tmp_path):
    source = tmp_path / "input.mp4"
    source.write_bytes(b"private video")
    storage = LocalStorage(tmp_path / "store")
    with ThreadPoolExecutor(max_workers=4) as executor:
        refs = list(
            executor.map(
                lambda _index: storage.upload(source, job_id="job_1", name="input.mp4"), range(8)
            )
        )
    assert len(set(refs)) == 8
    assert all(len(ref) == 32 and "/" not in ref for ref in refs)
    target = tmp_path / "output.mp4"
    storage.download(refs[0], target)
    assert target.read_bytes() == b"private video"
    storage.delete(refs[0])
    storage.delete(refs[0])
    assert not (storage.root / refs[0]).exists()


@pytest.mark.parametrize("ref", ["..", "../secret", "C:\\secret", "/secret", "", "a" * 32 + "/"])
def test_local_storage_rejects_untrusted_paths(tmp_path, ref):
    storage = LocalStorage(tmp_path / "store")
    with pytest.raises(FileOperationError):
        storage.download(ref, tmp_path / "out")
    with pytest.raises(FileOperationError):
        storage.delete(ref)


def test_local_symlink_reference_cannot_read_or_delete_outside(tmp_path):
    storage = LocalStorage(tmp_path / "store")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    link = storage.root / ("a" * 32)
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is not enabled on this Windows host.")
    with pytest.raises(FileOperationError):
        storage.download(link.name, tmp_path / "out")
    with pytest.raises(FileOperationError):
        storage.delete(link.name)
    assert outside.read_text() == "secret"


def test_local_failed_upload_and_download_leave_no_partials(tmp_path):
    storage = LocalStorage(tmp_path / "store")
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    with (
        mock.patch("linguoai.web_storage.shutil.copyfileobj", side_effect=OSError("disk full")),
        pytest.raises(OSError),
    ):
        storage.upload(source, job_id="job", name="video.mp4")
    assert list(storage.root.iterdir()) == []
    ref = storage.upload(source, job_id="job", name="video.mp4")
    output = tmp_path / "output.mp4"
    output.write_bytes(b"previous")
    with (
        mock.patch("linguoai.web_storage.shutil.copyfileobj", side_effect=OSError("read failed")),
        pytest.raises(OSError),
    ):
        storage.download(ref, output)
    assert output.read_bytes() == b"previous"
    assert not list(tmp_path.glob(".linguoai-download-*"))


def test_local_file_path_allows_direct_streaming_only_of_existing_private_files(tmp_path):
    storage = LocalStorage(tmp_path / "store")
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    ref = storage.upload(source, job_id="job", name="video.mp4")
    path = storage.file_path(ref)
    assert path == storage.root / ref
    assert path.read_bytes() == b"video"
    assert list(storage.root.iterdir()) == [path]
    with pytest.raises(FileNotFoundError):
        storage.file_path("0" * 32)
    with pytest.raises(FileOperationError):
        storage.file_path("../video.mp4")
    storage.delete(ref)
    with pytest.raises(FileNotFoundError):
        storage.file_path(ref)


class FakeRequest:
    def __init__(self, value=None):
        self.value = value
        self.retries = []

    def execute(self, *, num_retries):
        self.retries.append(num_retries)
        return self.value

    def next_chunk(self, *, num_retries):
        self.retries.append(num_retries)
        return None, self.value


class FakeDrive:
    def __init__(self, *, parent="private-folder", public=False, shared=True):
        self.parent = parent
        self.public = public
        self.shared = shared
        self.requests = []
        self.created = []
        self.deleted = []
        self.media_reads = []
        self.closed = False

    def files(self):
        return self

    def get(self, **kwargs):
        self.requests.append(kwargs)
        if kwargs["fileId"] == "private-folder":
            value = {
                "mimeType": "application/vnd.google-apps.folder",
                "permissions": [{"type": "anyone"}] if self.public else [],
            }
            if self.shared:
                value["driveId"] = "workspace-drive"
        else:
            value = {
                "parents": [self.parent],
                "appProperties": {"linguoai": "1"},
                "mimeType": "video/mp4",
            }
        return FakeRequest(value)

    def create(self, **kwargs):
        self.created.append(kwargs)
        self.upload_request = FakeRequest({"id": "new-file"})
        return self.upload_request

    def get_media(self, **kwargs):
        self.media_reads.append(kwargs)
        return FakeRequest(b"drive video")

    def delete(self, **kwargs):
        self.deleted.append(kwargs)
        return FakeRequest({})

    def close(self):
        self.closed = True


def test_drive_upload_download_metadata_and_bounded_retries(tmp_path):
    service = FakeDrive()
    storage = DriveStorage("private-folder", shared_drive=True, service=service, retries=2)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    media_stream = io.BytesIO()
    media = SimpleNamespace(stream=lambda: media_stream)
    with mock.patch("linguoai.web_storage._media_upload", return_value=media):
        ref = storage.upload(source, job_id="job_1", name="video.mp4")
    assert ref == "drive:new-file"
    assert media_stream.closed
    assert service.created[0]["body"] == {
        "name": "video.mp4",
        "parents": ["private-folder"],
        "appProperties": {"linguoai": "1", "job_id": "job_1", "name": "video.mp4"},
    }
    assert service.created[0]["supportsAllDrives"] is True
    assert service.upload_request.retries == [2]

    class Download:
        def __init__(self, output, request):
            self.output = output
            self.request = request

        def next_chunk(self, *, num_retries):
            assert num_retries == 2
            self.output.write(self.request.value)
            return None, True

    with mock.patch("linguoai.web_storage._media_download", side_effect=Download):
        storage.download(ref, tmp_path / "result.mp4")
    assert (tmp_path / "result.mp4").read_bytes() == b"drive video"
    storage.delete(ref)
    assert service.deleted == [{"fileId": "new-file", "supportsAllDrives": True}]
    storage.close()
    assert service.closed


def test_drive_rejects_arbitrary_outside_ids_before_media_read_or_delete(tmp_path):
    service = FakeDrive(parent="someone-elses-folder")
    storage = DriveStorage("private-folder", service=service)
    for operation in (
        lambda: storage.download("drive:secret", tmp_path / "out"),
        lambda: storage.delete("drive:secret"),
    ):
        with pytest.raises(FileOperationError):
            operation()
    assert service.media_reads == []
    assert service.deleted == []
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("ref", ["drive:private-folder", "drive:root", "secret", "drive:../secret"])
def test_drive_rejects_folder_and_malformed_refs(ref):
    service = FakeDrive()
    storage = DriveStorage("private-folder", service=service)
    with pytest.raises(FileOperationError):
        storage.delete(ref)
    assert not service.requests


def test_drive_checks_real_shared_drive_and_private_folder(tmp_path):
    source = tmp_path / "input.mp4"
    source.write_bytes(b"video")
    for service in (FakeDrive(shared=False), FakeDrive(public=True)):
        storage = DriveStorage("private-folder", shared_drive=True, service=service)
        with pytest.raises(ConfigurationError):
            storage.upload(source, job_id="job", name="video.mp4")
        assert not service.created


def test_service_account_cannot_use_mydrive_even_before_optional_imports(tmp_path):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"type": "service_account", "private_key": "secret"}))
    with pytest.raises(ConfigurationError, match="Shared Drive"):
        DriveStorage("private-folder", credentials)


def test_drive_download_failure_preserves_existing_file_and_cleans_partial(tmp_path):
    service = FakeDrive()
    storage = DriveStorage("private-folder", service=service)
    output = tmp_path / "output.mp4"
    output.write_bytes(b"existing")
    downloader = mock.Mock()
    downloader.next_chunk.side_effect = OSError("network broke")
    with (
        mock.patch("linguoai.web_storage._media_download", return_value=downloader),
        pytest.raises(OSError),
    ):
        storage.download("drive:input", output)
    assert output.read_bytes() == b"existing"
    assert not list(tmp_path.glob(".linguoai-download-*"))


def test_drive_failed_resumable_upload_closes_owned_file(tmp_path):
    service = FakeDrive()
    original_create = service.create

    def create(**kwargs):
        request = original_create(**kwargs)
        request.next_chunk = mock.Mock(side_effect=OSError("upload connection lost"))
        return request

    service.create = create
    storage = DriveStorage("private-folder", service=service)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    stream = io.BytesIO(b"video")
    with (
        mock.patch(
            "linguoai.web_storage._media_upload",
            return_value=SimpleNamespace(stream=lambda: stream),
        ),
        pytest.raises(OSError),
    ):
        storage.upload(source, job_id="job_1", name="video.mp4")
    assert stream.closed
