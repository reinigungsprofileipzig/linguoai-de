"""Authorization and durable-finalization regressions for resumable uploads."""

import hashlib
import threading
import time
from collections import namedtuple
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from linguoai.web_api import create_app
from linguoai.web_settings import ServerSettings, password_hash
from linguoai.web_uploads import MAX_CHUNK_BYTES

HEADERS = {"X-LinguoAI-Request": "1"}


@pytest.fixture
def app_client(tmp_path):
    settings = ServerSettings(
        tmp_path,
        {
            "alice": password_hash("integration-password"),
            "bob": password_hash("integration-password"),
        },
        "s" * 48,
        "w" * 48,
        max_upload_bytes=20 * 1024**2,
    )
    disk = namedtuple("usage", "total used free")(20 * 1024**3, 0, 20 * 1024**3)
    with patch("linguoai.web_api.shutil.disk_usage", return_value=disk):
        app = create_app(settings)
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            login(client)
            yield app, client


def login(client, username="alice"):
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "integration-password"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text


def create(client, payload=b"example-video", key="same-request", **overrides):
    return client.post(
        "/api/uploads",
        json={
            "filename": "video.mp4",
            "size_bytes": len(payload),
            "options": {"target_language": "de"},
            **overrides,
        },
        headers={**HEADERS, "X-Upload-Request-ID": key},
    )


def append(client, session, payload, offset=0, **headers):
    return client.put(
        f"/api/uploads/{session['id']}?offset={offset}",
        content=payload,
        headers={**HEADERS, "X-Chunk-SHA256": hashlib.sha256(payload).hexdigest(), **headers},
    )


def wait_completed(client, session, *, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/uploads/{session['id']}")
        assert response.status_code == 200, response.text
        row = response.json()
        assert row["status"] in {"finalizing", "completed"}, row
        if row["status"] == "completed":
            return row
        time.sleep(0.02)
    raise AssertionError("Finalization did not complete")


def test_create_and_chunk_retries_do_not_duplicate_or_queue_early(app_client):
    _app, client = app_client
    data = b"example-video"
    session = create(client, data).json()
    duplicate = create(client, data).json()
    assert duplicate["id"] == session["id"]
    assert duplicate["job_id"] == session["job_id"]
    assert len(client.get("/api/jobs").json()["jobs"]) == 1
    assert create(client, data, filename="other.mp4").status_code == 409
    assert client.post(f"/api/uploads/{session['id']}/complete", headers=HEADERS).status_code == 409
    assert client.get(f"/api/jobs/{session['job_id']}").json()["status"] == "uploading"
    first = append(client, session, data)
    assert first.status_code == 200, first.text
    assert append(client, session, data).json()["offset"] == len(data)
    finished = client.post(f"/api/uploads/{session['id']}/complete", headers=HEADERS)
    assert finished.status_code == 202, finished.text
    completed = wait_completed(client, session)
    assert completed["job"]["status"] == "queued"
    assert completed["job"]["id"] == session["job_id"]
    for private in ("principal", "fence_tag", "finalization_token", "storage_ref", "file_path"):
        assert private not in completed
    assert client.post(f"/api/uploads/{session['id']}/complete", headers=HEADERS).status_code == 200
    cancelled = client.delete(f"/api/uploads/{session['id']}", headers=HEADERS).json()
    assert cancelled["job"]["status"] == "cancelled"
    assert cancelled["status"] == "completed"


def test_role_owner_csrf_and_origin_checks_before_accepting_bytes(app_client):
    app, client = app_client
    session = create(client).json()
    assert client.put(f"/api/uploads/{session['id']}?offset=0", content=b"x").status_code == 403
    assert (
        append(client, session, b"example-video", Origin="https://other.example").status_code == 403
    )
    login(client, "bob")
    assert client.get(f"/api/uploads/{session['id']}").status_code == 404
    assert append(client, session, b"example-video").status_code == 404
    assert client.delete(f"/api/uploads/{session['id']}", headers=HEADERS).status_code == 404
    client.post("/api/auth/logout", headers=HEADERS)
    assert create(client).status_code == 401
    assert (
        client.put(
            f"/api/uploads/{session['id']}?offset=0", content=b"x", headers=HEADERS
        ).status_code
        == 401
    )
    assert app.state.chunk_uploads.uploads.get(session["id"], "user:alice")["offset"] == 0


def test_bad_checksum_and_oversize_block_never_change_committed_offset(app_client):
    app, client = app_client
    session = create(client).json()
    bad = append(client, session, b"example-video", **{"X-Chunk-SHA256": "0" * 64})
    assert bad.status_code == 409
    malformed = append(client, session, b"example-video", **{"X-Chunk-SHA256": "g" * 64})
    assert malformed.status_code == 422
    large = client.put(
        f"/api/uploads/{session['id']}?offset=0",
        content=b"x" * (MAX_CHUNK_BYTES + 1),
        headers={**HEADERS, "X-Chunk-SHA256": "0" * 64},
    )
    assert large.status_code == 413
    assert app.state.chunk_uploads.uploads.get(session["id"], "user:alice")["offset"] == 0
    assert create(client, size_bytes=21 * 1024**2, key="too-large").status_code == 413


def test_abort_before_finish_cancels_job_and_reclaims_staging(app_client):
    app, client = app_client
    session = create(client).json()
    assert append(client, session, b"example-video").status_code == 200
    aborted = client.delete(f"/api/uploads/{session['id']}", headers=HEADERS)
    assert aborted.status_code == 200, aborted.text
    assert aborted.json()["status"] == "aborted"
    assert aborted.json()["job"]["status"] == "cancelled"
    assert client.post(f"/api/uploads/{session['id']}/complete", headers=HEADERS).status_code == 409
    app.state.chunk_uploads.cleanup()
    assert client.get(f"/api/uploads/{session['id']}").status_code == 404
    assert not list((app.state.settings.state_directory / "uploads").glob("*.part"))


def test_failure_after_job_attachment_recovers_without_copying_again(app_client):
    app, client = app_client
    session = create(client).json()
    append(client, session, b"example-video")
    service = app.state.chunk_uploads
    original_finish = service.uploads.finish
    original_upload = service.storage.upload
    calls = {"finish": 0, "upload": 0}

    def interrupted_finish(*args, **kwargs):
        calls["finish"] += 1
        if calls["finish"] == 1:
            raise RuntimeError("simulated crash after job attachment")
        return original_finish(*args, **kwargs)

    def counted_upload(*args, **kwargs):
        calls["upload"] += 1
        return original_upload(*args, **kwargs)

    with (
        patch.object(service.uploads, "finish", interrupted_finish),
        patch.object(service.storage, "upload", counted_upload),
    ):
        assert (
            client.post(f"/api/uploads/{session['id']}/complete", headers=HEADERS).status_code
            == 202
        )
        completed = wait_completed(client, session)
    assert calls == {"finish": 2, "upload": 1}
    assert completed["job"]["status"] == "queued"
    assert len(list(service.storage.root.iterdir())) == 1


def test_cancel_during_slow_storage_cannot_release_job_or_leak_file(app_client):
    app, client = app_client
    session = create(client).json()
    append(client, session, b"example-video")
    service = app.state.chunk_uploads
    entered, release = threading.Event(), threading.Event()
    original_upload = service.storage.upload

    def slow_upload(*args, **kwargs):
        entered.set()
        assert release.wait(5), "Test failed to release the storage operation"
        return original_upload(*args, **kwargs)

    with patch.object(service.storage, "upload", slow_upload):
        response = client.post(f"/api/uploads/{session['id']}/complete", headers=HEADERS)
        assert response.status_code == 202
        assert entered.wait(2)
        try:
            assert (
                client.delete(f"/api/uploads/{session['id']}", headers=HEADERS).json()["job"][
                    "status"
                ]
                == "cancelled"
            )
        finally:
            release.set()
        deadline = time.monotonic() + 3
        while service.tasks and time.monotonic() < deadline:
            time.sleep(0.02)
    assert not service.tasks
    assert client.get(f"/api/jobs/{session['job_id']}").json()["status"] == "cancelled"
    assert not list(service.storage.root.iterdir())


def test_low_capacity_rejects_metadata_before_job_creation(app_client):
    _app, client = app_client
    disk = namedtuple("usage", "total used free")(20 * 1024**3, 0, 512 * 1024**2)
    with patch("linguoai.web_chunk_api.shutil.disk_usage", return_value=disk):
        response = create(client)
    assert response.status_code == 507
    assert client.get("/api/jobs").json()["jobs"] == []
