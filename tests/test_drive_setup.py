import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

SPEC = importlib.util.spec_from_file_location(
    "linguoai_drive_setup", Path(__file__).parents[1] / "scripts" / "connect_drive.py"
)
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class Request:
    def __init__(self, value):
        self.value = value

    def execute(self, *, num_retries):
        assert num_retries == 3
        return self.value


class FakeDrive:
    def __init__(self, pages=None, metadata=None):
        self.pages = list(pages or [{"files": []}])
        self.metadata = metadata or {
            "mimeType": setup.FOLDER_MIME,
            "permissions": [{"type": "user", "role": "owner"}],
        }
        self.calls = []
        self.closed = False

    def files(self):
        return self

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        return Request(self.pages.pop(0))

    def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        return Request({"id": "private_folder"})

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return Request(self.metadata)

    def close(self):
        self.closed = True


def private_json(tmp_path, name, value):
    path = tmp_path / name
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def authorized_user():
    return {
        "type": "authorized_user",
        "client_id": "desktop.apps.googleusercontent.com",
        "client_secret": "CLIENT_SECRET_NEVER_PRINT",
        "refresh_token": "REFRESH_TOKEN_NEVER_PRINT",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": [setup.DRIVE_SCOPE],
    }


def test_folder_created_in_personal_drive_without_public_permissions():
    drive = FakeDrive()
    assert setup.ensure_private_folder(drive) == "private_folder"
    created = [kwargs for operation, kwargs in drive.calls if operation == "create"]
    assert created == [
        {
            "body": {
                "name": "linguoai uploads",
                "mimeType": setup.FOLDER_MIME,
                "appProperties": setup.FOLDER_MARKER,
            },
            "fields": "id",
        }
    ]
    assert not any(operation == "permissions" for operation, _kwargs in drive.calls)
    query = drive.calls[0][1]["q"]
    assert "'me' in owners" in query and "appProperties has" in query


def test_folder_retry_reuses_marked_folder_across_paginated_search():
    drive = FakeDrive(
        [{"files": [], "nextPageToken": "page2"}, {"files": [{"id": "existing_folder"}]}]
    )
    assert setup.ensure_private_folder(drive) == "existing_folder"
    assert drive.calls[1][1]["pageToken"] == "page2"
    assert not any(operation == "create" for operation, _kwargs in drive.calls)


@pytest.mark.parametrize(
    "metadata",
    [
        {"mimeType": "video/mp4"},
        {"mimeType": setup.FOLDER_MIME, "trashed": True},
        {"mimeType": setup.FOLDER_MIME, "driveId": "workspace_drive"},
        {"mimeType": setup.FOLDER_MIME, "permissions": [{"type": "anyone", "role": "reader"}]},
        {"mimeType": setup.FOLDER_MIME, "permissions": [{"type": "domain", "role": "reader"}]},
        {"mimeType": setup.FOLDER_MIME, "permissions": [{"type": "user", "role": "writer"}]},
    ],
)
def test_folder_rejects_shared_public_trashed_or_wrong_type(metadata):
    with pytest.raises(setup.SetupError, match="privat"):
        setup.ensure_private_folder(FakeDrive(metadata=metadata), "existing_folder")


@pytest.mark.parametrize("folder_id", ["root", "../outside", "", None, "a" * 201])
def test_untrusted_drive_response_id_is_rejected(folder_id):
    drive = FakeDrive(pages=[{"files": [{"id": folder_id}]}])
    with pytest.raises(setup.SetupError, match="Ordner-ID"):
        setup.ensure_private_folder(drive)
    assert len(drive.calls) == 1


def test_loopback_oauth_requests_offline_file_scope_and_writes_no_access_token(
    tmp_path, monkeypatch
):
    client_path = private_json(
        tmp_path,
        "client.json",
        {
            "installed": {
                "client_id": "desktop.apps.googleusercontent.com",
                "client_secret": "CLIENT_SECRET_NEVER_PRINT",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
    )
    credentials = SimpleNamespace(
        **{key: value for key, value in authorized_user().items() if key not in {"type", "scopes"}},
        token="ACCESS_TOKEN_NEVER_PRINT",
        has_scopes=lambda scopes: scopes == [setup.DRIVE_SCOPE],
    )
    flow = mock.Mock()
    flow.run_local_server.return_value = credentials
    flow_type = mock.Mock()
    flow_type.from_client_config.return_value = flow
    monkeypatch.setitem(
        sys.modules, "google_auth_oauthlib.flow", SimpleNamespace(InstalledAppFlow=flow_type)
    )
    destination = tmp_path / "private" / "credentials.json"
    setup.authorize(client_path, destination, timeout=300)
    assert flow_type.from_client_config.call_args.kwargs["scopes"] == [setup.DRIVE_SCOPE]
    arguments = flow.run_local_server.call_args.kwargs
    assert arguments["host"] == "127.0.0.1" and arguments["port"] == 0
    assert arguments["bind_addr"] == "127.0.0.1"
    assert flow_type.from_client_config.call_args.kwargs["autogenerate_code_verifier"] is True
    assert arguments["open_browser"] is True and arguments["access_type"] == "offline"
    assert arguments["prompt"] == "consent" and arguments["timeout_seconds"] == 300
    assert "{url}" not in arguments["authorization_prompt_message"]
    saved = json.loads(destination.read_text("utf-8"))
    assert saved == authorized_user()
    assert "ACCESS_TOKEN_NEVER_PRINT" not in destination.read_text("utf-8")
    if os.name == "posix":
        assert destination.stat().st_mode & 0o777 == 0o600


def test_denied_refresh_access_keeps_previous_credentials(tmp_path, monkeypatch):
    client_path = private_json(
        tmp_path,
        "client.json",
        {
            "installed": {
                "client_id": "desktop.apps.googleusercontent.com",
                "client_secret": "CLIENT_SECRET_NEVER_PRINT",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
    )
    destination = private_json(tmp_path, "credentials.json", authorized_user())
    previous = destination.read_bytes()
    flow = mock.Mock()
    flow.run_local_server.return_value = SimpleNamespace(refresh_token=None)
    flow_type = mock.Mock()
    flow_type.from_client_config.return_value = flow
    monkeypatch.setitem(
        sys.modules, "google_auth_oauthlib.flow", SimpleNamespace(InstalledAppFlow=flow_type)
    )
    with pytest.raises(setup.SetupError, match="dauerhaften"):
        setup.authorize(client_path, destination)
    assert destination.read_bytes() == previous


def test_atomic_write_failure_does_not_replace_or_leave_credentials(tmp_path, monkeypatch):
    destination = private_json(tmp_path, "credentials.json", authorized_user())
    previous = destination.read_bytes()
    monkeypatch.setattr(setup.os, "fsync", mock.Mock(side_effect=OSError("PRIVATE_SECRET")))
    with pytest.raises(setup.SetupError, match="sicher gespeichert") as error:
        setup._private_json_write(destination, {"refresh_token": "REPLACEMENT_SECRET"})
    assert "PRIVATE_SECRET" not in str(error.value)
    assert destination.read_bytes() == previous
    assert not list(tmp_path.glob(".linguoai-drive-*"))


@pytest.mark.parametrize(
    "credentials",
    [
        {**authorized_user(), "type": "service_account"},
        {**authorized_user(), "refresh_token": ""},
        {**authorized_user(), "scopes": ["https://www.googleapis.com/auth/drive"]},
        {**authorized_user(), "token_uri": "https://attacker.invalid/token"},
    ],
)
def test_existing_credentials_require_personal_refresh_access_and_narrow_scope(credentials):
    with pytest.raises(setup.SetupError):
        setup._check_authorized_user(credentials)


def test_connect_reuses_private_credentials_only_publishes_fragment_after_smoke(
    tmp_path, monkeypatch
):
    credentials_path = private_json(tmp_path, "credentials.json", authorized_user())
    drive = FakeDrive(pages=[{"files": [{"id": "existing_folder"}]}])
    monkeypatch.setattr(setup, "_service", lambda _path: drive)
    smoke = mock.Mock()
    monkeypatch.setattr(setup, "smoke_test", smoke)
    fragment_path = tmp_path / "settings.json"
    value = setup.connect_drive(
        credentials_file=credentials_path, config_fragment=fragment_path, skip_oauth=True
    )
    smoke.assert_called_once_with(drive, "existing_folder")
    assert json.loads(fragment_path.read_text("utf-8")) == value
    assert value == {
        "storage_backend": "drive",
        "drive_folder_id": "existing_folder",
        "drive_credentials_path": "/etc/linguoai-web/drive-credentials.json",
        "drive_shared": False,
    }
    assert drive.closed
    assert authorized_user() == json.loads(credentials_path.read_text("utf-8"))


def test_failed_smoke_keeps_previous_fragment_and_closes_service(tmp_path, monkeypatch):
    credentials_path = private_json(tmp_path, "credentials.json", authorized_user())
    fragment_path = private_json(tmp_path, "settings.json", {"storage_backend": "local"})
    drive = FakeDrive()
    monkeypatch.setattr(setup, "_service", lambda _path: drive)
    monkeypatch.setattr(setup, "smoke_test", mock.Mock(side_effect=setup.SetupError("Test failed")))
    with pytest.raises(setup.SetupError, match="Test failed"):
        setup.connect_drive(
            credentials_file=credentials_path, config_fragment=fragment_path, skip_oauth=True
        )
    assert json.loads(fragment_path.read_text("utf-8")) == {"storage_backend": "local"}
    assert drive.closed


def test_smoke_uses_storage_roundtrip_and_deletes_even_failed_download(monkeypatch):
    from linguoai import web_storage

    events = []

    class Storage:
        def __init__(self, folder_id, *, service):
            assert folder_id == "private_folder" and service == "service"

        def upload(self, path, *, job_id, name):
            assert path.read_bytes() == b"LinguoAI private operator storage check\n"
            assert name == "LINGUOAI-STORAGE-CHECK.txt" and job_id.startswith("setup_")
            events.append("upload")
            return "drive:synthetic_check"

        def download(self, ref, destination):
            assert ref == "drive:synthetic_check"
            events.append("download")
            raise RuntimeError("SECRET_TOKEN_IN_PROVIDER_RESPONSE")

        def delete(self, ref):
            assert ref == "drive:synthetic_check"
            events.append("delete")

    monkeypatch.setattr(web_storage, "DriveStorage", Storage)
    with pytest.raises(setup.SetupError, match="Speichertest") as error:
        setup.smoke_test("service", "private_folder")
    assert events == ["upload", "download", "delete"]
    assert "SECRET_TOKEN" not in str(error.value)


def test_smoke_success_verifies_bytes_and_deletion(monkeypatch):
    from linguoai import web_storage

    storage = mock.Mock()
    payload = b"LinguoAI private operator storage check\n"
    storage.upload.return_value = "drive:test"
    storage.download.side_effect = lambda _ref, destination: destination.write_bytes(payload)
    factory = mock.Mock(return_value=storage)
    monkeypatch.setattr(web_storage, "DriveStorage", factory)
    setup.smoke_test("service", "private_folder")
    factory.assert_called_once_with("private_folder", service="service")
    storage.delete.assert_called_once_with("drive:test")


def test_cli_never_prints_provider_exception_or_credentials(tmp_path, monkeypatch, capsys):
    path = private_json(tmp_path, "credentials.json", authorized_user())
    monkeypatch.setattr(
        setup, "_service", mock.Mock(side_effect=RuntimeError("REFRESH_TOKEN_NEVER_PRINT"))
    )
    assert setup.main(["--skip-oauth", "--credentials-file", str(path)]) == 1
    output = capsys.readouterr().out
    assert "nicht abgeschlossen" in output
    assert "REFRESH_TOKEN" not in output and "CLIENT_SECRET" not in output
    assert not (tmp_path / "drive-settings.json").exists()


def test_client_is_required_before_any_setup(tmp_path):
    with pytest.raises(setup.SetupError, match="Client-Datei fehlt"):
        setup.connect_drive(
            credentials_file=tmp_path / "credentials.json",
            config_fragment=tmp_path / "settings.json",
        )
    assert list(tmp_path.iterdir()) == []


def test_sdk_callback_logging_is_suppressed_and_restored_after_error(caplog):
    import logging

    logger = logging.getLogger("google_auth_oauthlib.flow")
    previous = logger.disabled
    logger.disabled = False
    try:
        with (
            caplog.at_level(logging.INFO),
            pytest.raises(RuntimeError),
            setup._silent_oauth_logging(),
        ):
            logger.info("GET /?code=PRIVATE_AUTHORIZATION_CODE")
            raise RuntimeError("denied")
        assert logger.disabled is False
        with caplog.at_level(logging.INFO):
            logger.info("logging restored")
        assert "PRIVATE_AUTHORIZATION_CODE" not in caplog.text
        assert "logging restored" in caplog.text
    finally:
        logger.disabled = previous


def test_smoke_delete_failure_is_reported_without_provider_secret(monkeypatch):
    from linguoai import web_storage

    storage = mock.Mock()
    storage.upload.return_value = "drive:test"
    storage.download.side_effect = lambda _ref, destination: destination.write_bytes(
        b"LinguoAI private operator storage check\n"
    )
    storage.delete.side_effect = RuntimeError("PRIVATE_TOKEN_IN_PROVIDER_RESPONSE")
    monkeypatch.setattr(web_storage, "DriveStorage", mock.Mock(return_value=storage))
    with pytest.raises(setup.SetupError, match="nicht löschen") as error:
        setup.smoke_test("service", "private_folder")
    assert "PRIVATE_TOKEN" not in str(error.value)
