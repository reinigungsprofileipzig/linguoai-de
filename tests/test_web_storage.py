import io
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock

import pytest

from linguoai.errors import ConfigurationError, FileOperationError
from linguoai.web_storage import DriveStorage, LocalStorage, StorageRouter


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


def test_storage_router_drive_activation_preserves_old_local_files(tmp_path):
    local = LocalStorage(tmp_path / "files")
    source = tmp_path / "video.mp4"
    source.write_bytes(b"old local video")
    old_ref = local.upload(source, job_id="old_job", name="input.mp4")
    service = FakeDrive()
    drive = DriveStorage("private-folder", service=service)
    router = StorageRouter(local, drive)
    source.write_bytes(b"new drive video")
    stream = io.BytesIO()
    with mock.patch(
        "linguoai.web_storage._media_upload", return_value=SimpleNamespace(stream=lambda: stream)
    ):
        new_ref = router.upload(source, job_id="new_job", name="input.mp4")
    assert new_ref == "drive:new-file"
    assert stream.closed
    assert len(service.created) == 1
    assert list(local.root.iterdir()) == [local.root / old_ref]
    old_result = tmp_path / "old-result.mp4"
    router.download(old_ref, old_result)
    assert old_result.read_bytes() == b"old local video"
    assert router.file_path(old_ref) == local.root / old_ref
    assert router.file_path("local:" + old_ref) == local.root / old_ref

    class Download:
        def __init__(self, output, request):
            self.output = output
            self.request = request

        def next_chunk(self, *, num_retries):
            self.output.write(self.request.value)
            return None, True

    with mock.patch("linguoai.web_storage._media_download", side_effect=Download):
        router.download(new_ref, tmp_path / "drive-result.mp4")
    assert (tmp_path / "drive-result.mp4").read_bytes() == b"drive video"
    router.delete("local:" + old_ref)
    router.delete(old_ref)
    assert not list(local.root.iterdir())
    router.delete(new_ref)
    assert service.deleted == [{"fileId": "new-file", "supportsAllDrives": True}]
    router.close()
    assert service.closed


def test_storage_router_without_drive_keeps_local_uploads_and_rejects_drive_refs(tmp_path):
    local = LocalStorage(tmp_path / "files")
    router = StorageRouter(local)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    ref = router.upload(source, job_id="job", name="input.mp4")
    assert len(ref) == 32 and router.is_local_ref(ref)
    router.download("local:" + ref, tmp_path / "copy.mp4")
    assert (tmp_path / "copy.mp4").read_bytes() == b"video"
    for operation in (
        lambda: router.download("drive:" + ref, tmp_path / "wrong.mp4"),
        lambda: router.delete("drive:" + ref),
    ):
        with pytest.raises(ConfigurationError, match="not configured"):
            operation()
    assert not (tmp_path / "wrong.mp4").exists()
    assert router.file_path(ref).read_bytes() == b"video"
    router.close()


def test_storage_router_does_not_fallback_to_local_on_drive_upload_failure(tmp_path):
    local = LocalStorage(tmp_path / "files")
    service = FakeDrive()
    drive = DriveStorage("private-folder", service=service)
    router = StorageRouter(local, drive)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    with (
        mock.patch.object(drive, "upload", side_effect=OSError("Drive unavailable")),
        pytest.raises(OSError, match="Drive unavailable"),
    ):
        router.upload(source, job_id="job", name="input.mp4")
    assert not list(local.root.iterdir())


@pytest.mark.parametrize(
    "ref",
    [
        None,
        12,
        "",
        "../secret",
        "C:\\secret",
        "/secret",
        "local:../secret",
        "local:" + "A" * 32,
        "local:" + "a" * 32 + "/",
        "local:local:" + "a" * 32,
        "drive:../secret",
        "drive:/secret",
        "unknown:" + "a" * 32,
    ],
)
def test_storage_router_rejects_untrusted_refs_before_provider_access(tmp_path, ref):
    local = LocalStorage(tmp_path / "files")
    service = FakeDrive()
    router = StorageRouter(local, DriveStorage("private-folder", service=service))
    for operation in (
        lambda: router.download(ref, tmp_path / "out"),
        lambda: router.delete(ref),
        lambda: router.file_path(ref),
    ):
        with pytest.raises(FileOperationError):
            operation()
    assert service.requests == []
    assert service.media_reads == []
    assert service.deleted == []
    assert not (tmp_path / "out").exists()


def test_storage_router_preserves_drive_folder_boundary_and_cannot_stream_drive_ids(tmp_path):
    local = LocalStorage(tmp_path / "files")
    service = FakeDrive(parent="another-private-folder")
    router = StorageRouter(local, DriveStorage("private-folder", service=service))
    for operation in (
        lambda: router.download("drive:secret", tmp_path / "out"),
        lambda: router.delete("drive:secret"),
        lambda: router.file_path("drive:secret"),
    ):
        with pytest.raises(FileOperationError):
            operation()
    assert not router.is_local_ref("drive:" + "a" * 32)
    assert service.media_reads == []
    assert service.deleted == []
    assert not (tmp_path / "out").exists()


def test_storage_router_prefixed_local_symlinks_cannot_escape_store(tmp_path):
    local = LocalStorage(tmp_path / "files")
    router = StorageRouter(local, DriveStorage("private-folder", service=FakeDrive()))
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"secret")
    link = local.root / ("a" * 32)
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is not enabled on this Windows host.")
    for operation in (
        lambda: router.download("local:" + link.name, tmp_path / "out"),
        lambda: router.delete("local:" + link.name),
        lambda: router.file_path("local:" + link.name),
    ):
        with pytest.raises(FileOperationError):
            operation()
    assert outside.read_bytes() == b"secret"
