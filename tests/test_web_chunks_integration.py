"""Exercise proxy-sized uploads over actual HTTP sockets, without ASR downloads."""

import hashlib
import json
import socket
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from linguoai.domain import BatchResult, JobResult
from linguoai.web_api import create_app
from linguoai.web_settings import ServerSettings, password_hash
from linguoai.web_worker import UPLOAD_CHUNK_BYTES, RemoteWorker

INPUT_BYTES = b"i" * (UPLOAD_CHUNK_BYTES + 197)
OUTPUT_BYTES = b"v" * (UPLOAD_CHUNK_BYTES + 321)
PASSWORD = "chunk-integration-password"


class LargeResultPipeline:
    def __init__(self, config, *, event_sink, cancel_event):
        self.cancel_event = cancel_event

    def cancel(self):
        self.cancel_event.set()

    def run_path(self, source, target):
        assert source.read_bytes() == INPUT_BYTES
        target.write_bytes(OUTPUT_BYTES)
        original = target.parent / "original.srt"
        translated = target.parent / "translated.srt"
        original.write_bytes(b"original subtitles")
        translated.write_bytes(b"translated subtitles")
        return BatchResult(completed=(JobResult(source, target, original, translated, "en", 1),))


@pytest.fixture
def live_api(tmp_path, monkeypatch):
    # Keep production disk reservation checks deterministic on small CI volumes.
    monkeypatch.setattr(
        "linguoai.web_api.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=20 * 1024**3, used=0, free=20 * 1024**3),
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    settings = ServerSettings(
        state_directory=tmp_path / "state",
        users={"alice": password_hash(PASSWORD), "bob": password_hash(PASSWORD)},
        session_secret="s" * 48,
        worker_token="w" * 48,
        public_url=origin,
        max_upload_bytes=32 * 1024**2,
        max_result_bytes=32 * 1024**2,
    )
    app = create_app(settings)
    ready = threading.Event()

    class TestServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started:
                ready.set()

    server = TestServer(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        assert ready.wait(10), "Uvicorn did not start"
        yield SimpleNamespace(origin=origin, settings=settings, app=app, directory=tmp_path)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
    assert not thread.is_alive()


@contextmanager
def authenticated_browser(api, username="alice"):
    with httpx.Client(
        base_url=api.origin,
        headers={"X-LinguoAI-Request": "1", "Origin": api.origin},
        timeout=10,
    ) as client:
        login = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
        assert login.status_code == 200, login.text
        yield client


def assert_public_session(row):
    assert (
        not {"principal", "fence_tag", "storage_ref", "finalization_token", "lease_token"}
        & row.keys()
    )
    assert row["chunk_bytes"] == UPLOAD_CHUNK_BYTES


def create_input(browser, data, request_id):
    response = browser.post(
        "/api/uploads",
        headers={"X-Upload-Request-ID": request_id},
        json={
            "filename": "input.mp4",
            "size_bytes": len(data),
            "options": {"target_language": "de", "asr_engine": "whisper"},
        },
    )
    assert response.status_code == 201, response.text
    row = response.json()
    assert_public_session(row)
    assert row["status"] == "open"
    return row


def append_input(browser, row, data, *, duplicate_first=False):
    url = f"/api/uploads/{row['id']}"
    requests = []
    for offset in range(0, len(data), UPLOAD_CHUNK_BYTES):
        chunk = data[offset : offset + UPLOAD_CHUNK_BYTES]
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest(),
        }
        response = browser.put(url, params={"offset": offset}, headers=headers, content=chunk)
        assert response.status_code == 200, response.text
        assert response.json()["offset"] == offset + len(chunk)
        assert_public_session(response.json())
        requests.append(len(chunk))
        if offset == 0 and duplicate_first:
            duplicate = browser.put(url, params={"offset": offset}, headers=headers, content=chunk)
            assert duplicate.status_code == 200, duplicate.text
            assert duplicate.json()["offset"] == len(chunk)
    complete = browser.post(url + "/complete")
    assert complete.status_code in {200, 202}, complete.text
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = browser.get(url)
        assert status.status_code == 200, status.text
        result = status.json()
        assert_public_session(result)
        if result["status"] == "completed":
            assert result["job"]["status"] == "queued"
            return result, requests
        assert result["status"] in {"ready", "finalizing"}, result
        time.sleep(0.01)
    pytest.fail("Input finalization did not finish")


class ObservedHTTPTransport(httpx.BaseTransport):
    """Forward to real Uvicorn; inject loss only after the server committed a chunk."""

    def __init__(self, metrics):
        self.inner = httpx.HTTPTransport()
        self.metrics = metrics

    def handle_request(self, request):
        is_chunk = request.method == "PUT" and "/uploads/" in request.url.path
        if is_chunk:
            payload = request.read()
            assert 0 < len(payload) <= UPLOAD_CHUNK_BYTES
            with self.metrics.lock:
                self.metrics.puts.append((int(request.url.params["offset"]), len(payload)))
        response = self.inner.handle_request(request)
        with self.metrics.lock:
            if is_chunk and self.metrics.lose_ack and not self.metrics.lost:
                assert response.status_code == 200
                self.metrics.lost = True
                response.read()
                response.close()
                raise httpx.ReadError("Committed chunk acknowledgement was lost", request=request)
            if (
                request.method == "GET"
                and "/uploads/" in request.url.path
                and self.metrics.stale_recovery
                and self.metrics.lost
                and not self.metrics.stale_sent
            ):
                assert response.status_code == 200
                row = json.loads(response.read())
                response.close()
                row["offset"] = 0
                self.metrics.stale_sent = True
                return httpx.Response(200, json=row, request=request)
        return response

    def close(self):
        self.inner.close()


@pytest.mark.parametrize("recovery", ["normal", "lost_ack", "duplicate"])
def test_real_http_browser_chunk_upload_worker_chunk_result_and_private_downloads(
    live_api, recovery
):
    api = live_api
    metrics = SimpleNamespace(
        lock=threading.Lock(),
        puts=[],
        lose_ack=recovery != "normal",
        lost=False,
        stale_recovery=recovery == "duplicate",
        stale_sent=False,
    )

    def client_factory(**kwargs):
        return httpx.Client(transport=ObservedHTTPTransport(metrics), **kwargs)

    with authenticated_browser(api) as browser:
        capabilities = browser.get("/api/capabilities").json()
        assert capabilities["upload_protocol"] == "chunks-v1"
        assert capabilities["upload_chunk_bytes"] == UPLOAD_CHUNK_BYTES
        row = create_input(browser, INPUT_BYTES, "large-input")
        queued, sizes = append_input(browser, row, INPUT_BYTES, duplicate_first=True)
        assert sizes == [UPLOAD_CHUNK_BYTES, 197]
        job_id = queued["job_id"]
        temp_directory = api.directory / "worker"
        temp_directory.mkdir()
        with RemoteWorker(
            api.origin,
            api.settings.worker_token,
            "worker-1",
            pipeline_factory=LargeResultPipeline,
            client_factory=client_factory,
            temp_directory=temp_directory,
            environment={},
            heartbeat_seconds=0.05,
            request_timeout_seconds=10,
            upload_poll_seconds=0.01,
            upload_retry_base_seconds=0,
        ) as remote:
            assert remote.run_once() is True
            assert remote.run_once() is False
        completed = browser.get(f"/api/jobs/{job_id}")
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "completed", completed.text
        assert completed.json()["available_at"] is not None
        expected = {
            "video": OUTPUT_BYTES,
            "original_srt": b"original subtitles",
            "translated_srt": b"translated subtitles",
        }
        assert {artifact["name"] for artifact in completed.json()["artifacts"]} == expected.keys()
        for artifact in completed.json()["artifacts"]:
            download = browser.get(artifact["download_url"])
            assert download.status_code == 200, download.text
            assert download.content == expected[artifact["name"]]
            assert "attachment" in download.headers["content-disposition"]
            assert download.headers["cache-control"] == "no-store"
        with (
            httpx.Client(base_url=api.origin) as anonymous,
            authenticated_browser(api, "bob") as other,
        ):
            url = f"/api/jobs/{job_id}/artifacts/video"
            assert anonymous.get(url).status_code == 401
            assert other.get(url).status_code == 404
        assert list(temp_directory.iterdir()) == []
        assert list((api.settings.state_directory / "uploads").glob("*.part")) == []
    offsets = [offset for offset, _size in metrics.puts]
    assert offsets == (
        [0, 0, UPLOAD_CHUNK_BYTES] if recovery == "duplicate" else [0, UPLOAD_CHUNK_BYTES]
    )
    assert all(size <= UPLOAD_CHUNK_BYTES for _offset, size in metrics.puts)
    assert metrics.lost is (recovery != "normal")


def claim_worker(client, worker_id="worker-1"):
    claimed = client.post("/api/worker/claim", json={"worker_id": worker_id})
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["job"] is not None
    return claimed.json()["job"]


def test_real_http_chunk_sessions_enforce_owner_fence_job_and_cancellation(live_api):
    api = live_api
    with (
        authenticated_browser(api) as browser,
        authenticated_browser(api, "bob") as other,
        httpx.Client(
            base_url=api.origin,
            headers={"Authorization": "Bearer " + api.settings.worker_token},
            timeout=10,
        ) as operator,
    ):
        row = create_input(browser, b"uploaded-video", "owner-input")
        input_url = f"/api/uploads/{row['id']}"
        assert other.get(input_url).status_code == 404
        assert other.delete(input_url).status_code == 404
        forbidden = browser.put(
            input_url,
            params={"offset": 0},
            content=b"x",
            headers={
                "Origin": "https://attacker.example",
                "X-Chunk-SHA256": hashlib.sha256(b"x").hexdigest(),
            },
        )
        assert forbidden.status_code == 403
        oversized = b"x" * (UPLOAD_CHUNK_BYTES + 1)
        rejected = browser.put(
            input_url,
            params={"offset": 0},
            content=oversized,
            headers={"X-Chunk-SHA256": hashlib.sha256(oversized).hexdigest()},
        )
        assert rejected.status_code == 413
        assert browser.get(input_url).json()["offset"] == 0
        queued, _sizes = append_input(browser, row, b"uploaded-video")
        job = claim_worker(operator)
        assert job["id"] == queued["job_id"]
        ownership = {"worker_id": "worker-1", "lease_token": job["lease_token"]}
        created = operator.post(
            f"/api/worker/jobs/{job['id']}/uploads/video",
            json={**ownership, "filename": "output.mp4", "size_bytes": UPLOAD_CHUNK_BYTES + 6},
        )
        assert created.status_code == 201, created.text
        artifact = created.json()
        assert_public_session(artifact)
        artifact_url = f"/api/worker/jobs/{job['id']}/uploads/{artifact['id']}"
        worker_headers = {"X-Worker-ID": "worker-1", "X-Job-Lease": job["lease_token"]}
        wrong_fence = operator.get(
            artifact_url, headers={**worker_headers, "X-Job-Lease": "z" * 48}
        )
        assert wrong_fence.status_code == 409
        wrong_worker = operator.get(
            artifact_url, headers={**worker_headers, "X-Worker-ID": "worker-2"}
        )
        assert wrong_worker.status_code == 409
        row2 = create_input(browser, b"other-video", "other-input")
        append_input(browser, row2, b"other-video")
        other_job = claim_worker(operator)
        assert other_job["id"] != job["id"]
        wrong_job_url = f"/api/worker/jobs/{other_job['id']}/uploads/{artifact['id']}"
        wrong_job = operator.get(
            wrong_job_url,
            headers={"X-Worker-ID": "worker-1", "X-Job-Lease": other_job["lease_token"]},
        )
        assert wrong_job.status_code == 404
        assert operator.get(artifact_url, headers=worker_headers).json()["offset"] == 0
        first = b"r" * UPLOAD_CHUNK_BYTES
        appended = operator.put(
            artifact_url,
            params={"offset": 0},
            content=first,
            headers={**worker_headers, "X-Chunk-SHA256": hashlib.sha256(first).hexdigest()},
        )
        assert appended.status_code == 200, appended.text
        assert appended.json()["offset"] == len(first)
        cancelled = browser.post(f"/api/jobs/{job['id']}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["cancel_requested"] is True
        assert operator.get(artifact_url, headers=worker_headers).status_code == 409
        assert operator.post(artifact_url + "/complete", json=ownership).status_code == 409
        assert (
            operator.put(
                artifact_url,
                params={"offset": len(first)},
                content=b"result",
                headers={**worker_headers, "X-Chunk-SHA256": hashlib.sha256(b"result").hexdigest()},
            ).status_code
            == 409
        )
        assert operator.delete(artifact_url, headers=worker_headers).status_code == 200
        finished = operator.post(f"/api/worker/jobs/{job['id']}/complete", json=ownership)
        assert finished.status_code == 200, finished.text
        public = browser.get(f"/api/jobs/{job['id']}").json()
        assert public["status"] == "cancelled"
        assert public["artifacts"] == []
        assert browser.get(f"/api/jobs/{job['id']}/artifacts/video").status_code == 404
