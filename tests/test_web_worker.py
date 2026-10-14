import hashlib
import json
import socket
import threading
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest

from linguoai.domain import BatchResult, JobResult, ProgressUpdate, Stage
from linguoai.errors import (
    ConfigurationError,
    DependencyError,
    SpeechSynthesisError,
    TranscriptionError,
    TranslationError,
)
from linguoai.web_worker import (
    UPLOAD_CHUNK_BYTES,
    RemoteWorker,
    build_job_config,
    worker_capabilities,
)


class FakeServer:
    def __init__(self, job=None):
        self.job = job
        self.calls = []
        self.artifacts = []
        self.heartbeats = 0
        self.cancel_after = None
        self.lose_after = None
        self.lock = threading.Lock()
        self.upload_failure = None

    def __call__(self, request):
        assert request.headers["authorization"] == "Bearer worker-secret"
        path = request.url.path
        with self.lock:
            body = request.read()
            self.calls.append((path, body))
            if path.endswith("/claim"):
                return httpx.Response(200, json={"job": self.job})
            if path.endswith("/heartbeat"):
                self.heartbeats += 1
                if self.lose_after and self.heartbeats >= self.lose_after:
                    return httpx.Response(409, json={"detail": "lease expired"})
                return httpx.Response(
                    200,
                    json={
                        "cancel_requested": bool(
                            self.cancel_after and self.heartbeats >= self.cancel_after
                        )
                    },
                )
            if path.endswith("/input"):
                assert request.headers["x-worker-id"] == "worker-1"
                assert request.headers["x-job-lease"] == "lease-secret"
                assert not request.url.query
                return httpx.Response(200, content=b"uploaded-video")
            if "/artifacts/" in path:
                name = path.rsplit("/", 1)[-1]
                assert b"lease-secret" in body
                self.artifacts.append(name)
                if name == self.upload_failure:
                    return httpx.Response(500, json={"detail": "storage unavailable"})
                return httpx.Response(200, json={"name": name})
            return httpx.Response(200, json={"ok": True})


def claim():
    return {
        "id": "job_1",
        "filename": "video.mp4",
        "lease_token": "lease-secret",
        "options": {"target_language": "de", "asr_engine": "whisper"},
    }


class FakePipeline:
    def __init__(self, config, *, event_sink, cancel_event):
        self.config = config
        self.event_sink = event_sink
        self.cancel_event = cancel_event
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        self.cancel_event.set()

    def run_path(self, source, target):
        assert source.read_bytes() == b"uploaded-video"
        self.event_sink(ProgressUpdate(75, Stage.SYNTHESIZING, "Working"))
        original = target.parent / "original.srt"
        translated = target.parent / "translated.srt"
        target.write_bytes(b"dubbed-video")
        original.write_text("source subtitles")
        translated.write_text("translated subtitles")
        self.directory = target.parent
        return BatchResult(completed=(JobResult(source, target, original, translated, "en", 1),))


def worker(server, *, factory=FakePipeline, temp_directory=None, environment=None):
    transport = httpx.MockTransport(server)

    def client_factory(**kwargs):
        return httpx.Client(transport=transport, **kwargs)

    return RemoteWorker(
        "http://localhost:8000",
        "worker-secret",
        "worker-1",
        client_factory=client_factory,
        pipeline_factory=factory,
        heartbeat_seconds=0.01,
        temp_directory=temp_directory,
        environment=environment or {},
    )


def test_worker_roundtrip_all_artifacts_before_completion_and_temp_cleanup(tmp_path):
    server = FakeServer(claim())
    pipelines = []

    def factory(*args, **kwargs):
        pipeline = FakePipeline(*args, **kwargs)
        pipelines.append(pipeline)
        return pipeline

    with worker(server, factory=factory, temp_directory=tmp_path) as remote:
        assert remote.run_once() is True
    assert server.artifacts == ["video", "original_srt", "translated_srt"]
    assert server.calls[-1][0].endswith("/complete")
    assert server.heartbeats >= 2
    assert pipelines[0].config.whisper_model == "small"
    assert pipelines[0].config.parakeet_threads == 4
    assert not pipelines[0].directory.exists()
    assert list(tmp_path.iterdir()) == []


def test_idle_worker_does_not_start_pipeline_or_download():
    server = FakeServer()
    factory = mock.Mock()
    with worker(server, factory=factory) as remote:
        assert remote.run_once() is False
    factory.assert_not_called()
    assert len(server.calls) == 1


def test_incomplete_pipeline_result_cannot_publish(tmp_path):
    server = FakeServer(claim())

    class Incomplete(FakePipeline):
        def run_path(self, source, target):
            result = super().run_path(source, target)
            result.completed[0].translated_subtitles.unlink()
            return result

    with worker(server, factory=Incomplete, temp_directory=tmp_path) as remote:
        remote.run_once()
    assert not server.artifacts
    assert server.calls[-1][0].endswith("/fail")
    assert not any(path.endswith("/complete") for path, _body in server.calls)
    assert list(tmp_path.iterdir()) == []


def test_artifact_upload_failure_cannot_mark_complete():
    server = FakeServer(claim())
    server.upload_failure = "original_srt"
    with worker(server) as remote:
        remote.run_once()
    assert server.artifacts == ["video", "original_srt"]
    assert server.calls[-1][0].endswith("/fail")
    assert not any(path.endswith("/complete") for path, _body in server.calls)


@pytest.mark.parametrize("lose_lease", [False, True])
def test_heartbeat_runs_during_inference_and_cancels_or_discards_stale_results(lose_lease):
    server = FakeServer(claim())
    pipelines = []

    class SlowPipeline(FakePipeline):
        def run_path(self, source, target):
            # Arm the server response only once inference has actually begun.
            with server.lock:
                if lose_lease:
                    server.lose_after = server.heartbeats + 1
                else:
                    server.cancel_after = server.heartbeats + 1
            assert self.cancel_event.wait(5), "Heartbeat failed to interrupt inference"
            # A provider may finish after cancellation; even then nothing can publish.
            return super().run_path(source, target)

    def factory(*args, **kwargs):
        pipeline = SlowPipeline(*args, **kwargs)
        pipelines.append(pipeline)
        return pipeline

    with worker(server, factory=factory) as remote:
        remote.run_once()
    assert server.heartbeats >= 2
    assert pipelines[0].cancelled
    assert not server.artifacts
    assert not any(path.endswith("/complete") for path, _body in server.calls)
    failures = [json.loads(body) for path, body in server.calls if path.endswith("/fail")]
    if lose_lease:
        assert failures == []
    else:
        assert failures[0]["cancelled"] is True


def test_cancel_on_initial_heartbeat_skips_video_download():
    server = FakeServer(claim())
    server.cancel_after = 1
    factory = mock.Mock()
    with worker(server, factory=factory) as remote:
        remote.run_once()
    factory.assert_not_called()
    assert not any(path.endswith("/input") for path, _body in server.calls)
    assert json.loads(server.calls[-1][1])["cancelled"] is True


def test_cancellation_after_uploaded_artifacts_still_prevents_publication():
    server = FakeServer(claim())
    server.cancel_after = 2
    with worker(server) as remote:
        remote.heartbeat_seconds = 60
        remote.run_once()
    assert server.artifacts == ["video", "original_srt", "translated_srt"]
    assert not any(path.endswith("/complete") for path, _body in server.calls)
    assert server.calls[-1][0].endswith("/fail")
    assert json.loads(server.calls[-1][1])["cancelled"] is True


def test_job_cannot_supply_local_paths_or_provider_secrets(tmp_path):
    for options in (
        {"parakeet_model_directory": "C:/secret"},
        {"gemini_api_key": "evil"},
        {"temp_directory": "../outside"},
        {"tts_reference_audio": "secret.wav"},
        {"asr_engine": None},
        {"target_language": 1},
    ):
        with pytest.raises(ConfigurationError):
            build_job_config(options, environment={}, temp_directory=tmp_path)


def test_config_gets_models_and_secrets_only_from_worker_environment(tmp_path):
    config = build_job_config(
        {
            "asr_engine": "parakeet",
            "parakeet_profile": "german-primeline",
            "source_language": "de",
            "target_language": "en",
            "translation_service": "deepl",
        },
        environment={
            "LINGUOAI_PARAKEET_GERMAN_MODEL_DIRECTORY": str(tmp_path),
            "DEEPL_API_KEY": "operator-secret",
        },
    )
    assert config.parakeet_model_directory == tmp_path
    assert config.deepl_api_key == "operator-secret"
    assert config.parakeet_threads == 4
    with pytest.raises(ConfigurationError):
        build_job_config({"asr_engine": "parakeet", "source_language": "auto"}, environment={})


def test_worker_capabilities_require_dependency_and_complete_model(tmp_path):
    environment = {"LINGUOAI_PARAKEET_MODEL_DIRECTORY": str(tmp_path)}
    with mock.patch("linguoai.web_worker.importlib.util.find_spec", return_value=object()):
        assert worker_capabilities(environment)["asr_engines"] == ["whisper"]
        for name in ("encoder.int8.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt"):
            (tmp_path / name).write_bytes(b"model")
        capabilities = worker_capabilities(environment)
        assert capabilities["asr_engines"] == ["whisper", "parakeet"]
        assert capabilities["parakeet_profiles"] == ["multilingual-v3"]
        assert capabilities["tts_engines"] == ["edge", "gtts"]
    with mock.patch("linguoai.web_worker.importlib.util.find_spec", return_value=None):
        assert worker_capabilities(environment)["parakeet_profiles"] == []


def test_worker_failure_redacts_operator_secrets():
    server = FakeServer(claim())

    def factory(*_args, **_kwargs):
        raise RuntimeError("worker-secret provider-secret lease-secret")

    with worker(
        server, factory=factory, environment={"DEEPL_API_KEY": "provider-secret"}
    ) as remote:
        remote.run_once()
    error = json.loads(server.calls[-1][1])["error"]
    assert "worker-secret" not in error
    assert "provider-secret" not in error
    assert "lease-secret" not in error


def test_worker_closes_independent_main_and_heartbeat_clients():
    server = FakeServer(claim())
    clients = []
    transport = httpx.MockTransport(server)

    def client_factory(**kwargs):
        client = httpx.Client(transport=transport, **kwargs)
        clients.append(client)
        return client

    with RemoteWorker(
        "http://localhost:8000",
        "worker-secret",
        "worker-1",
        client_factory=client_factory,
        pipeline_factory=FakePipeline,
        environment={},
    ) as remote:
        remote.run_once()
        assert clients[1].is_closed
        assert not clients[0].is_closed
    assert len(clients) == 2
    assert all(client.is_closed for client in clients)


def test_worker_ignores_job_filename_directory_parts(tmp_path):
    job = claim()
    job["filename"] = "../../outside.mp4"
    server = FakeServer(job)
    with worker(server, temp_directory=tmp_path) as remote:
        remote.run_once()
    assert server.calls[-1][0].endswith("/complete")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "server",
    [
        "http://example.com",
        "https://user:pass@example.com",
        "https://example.com/?secret=1",
        "https://example.com/app/../private",
        "https://example.com/app/%2e%2e/private",
        "https://example.com//app",
        "file:///tmp",
    ],
)
def test_worker_rejects_unsafe_token_destinations(server):
    with pytest.raises(ConfigurationError):
        RemoteWorker(server, "secret", "worker-1")


def test_worker_preserves_safe_remote_server_subpath_for_all_endpoints():
    server = FakeServer(claim())
    requests = []

    def route(request):
        assert request.url.path.startswith("/linguoai/api/")
        requests.append(request.url.path)
        internal = httpx.Request(
            request.method,
            request.url.copy_with(path=request.url.path.removeprefix("/linguoai")),
            headers=request.headers,
            content=request.read(),
        )
        return server(internal)

    transport = httpx.MockTransport(route)

    def client_factory(**kwargs):
        return httpx.Client(transport=transport, **kwargs)

    with RemoteWorker(
        "https://example.com/linguoai/",
        "worker-secret",
        "worker-1",
        client_factory=client_factory,
        pipeline_factory=FakePipeline,
        environment={},
    ) as remote:
        remote.run_once()
    assert requests[-1].endswith("/complete")
    assert server.artifacts == ["video", "original_srt", "translated_srt"]


def test_real_http_api_worker_roundtrip_with_authenticated_downloads(tmp_path, monkeypatch):
    """Exercise the actual socket/multipart/lease/storage boundary with no model downloads."""
    monkeypatch.setattr(
        "linguoai.web_api.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=2**40, used=0, free=2**40),
    )
    uvicorn = pytest.importorskip("uvicorn")
    pytest.importorskip("fastapi")
    from linguoai.web_api import create_app
    from linguoai.web_settings import ServerSettings, password_hash

    ready = threading.Event()

    class TestServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    worker_token = "worker-secret-" + "a" * 40
    settings = ServerSettings(
        state_directory=tmp_path / "state",
        users={"alice": password_hash("integration-password")},
        session_secret="s" * 40,
        worker_token=worker_token,
        public_url=origin,
    )
    app = create_app(settings)
    server = TestServer(uvicorn.Config(app, log_level="error", access_log=False))
    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    server_thread.start()
    try:
        assert ready.wait(10), "Local API did not become ready"
        browser_headers = {"X-LinguoAI-Request": "1", "Origin": origin}
        with httpx.Client(base_url=origin, headers=browser_headers, timeout=10) as browser:
            login = browser.post(
                "/api/auth/login", json={"username": "alice", "password": "integration-password"}
            )
            assert login.status_code == 200, login.text
            upload = browser.post(
                "/api/jobs",
                data={"options": json.dumps({"target_language": "de", "asr_engine": "whisper"})},
                files={"file": ("example.mp4", b"uploaded-video", "video/mp4")},
            )
            assert upload.status_code == 201, upload.text
            job_id = upload.json()["id"]
            assert upload.json()["status"] == "queued"
            with RemoteWorker(
                origin,
                worker_token,
                "worker-1",
                pipeline_factory=FakePipeline,
                environment={},
                request_timeout_seconds=10,
            ) as remote:
                assert remote.run_once() is True
                assert remote.run_once() is False
            status = browser.get(f"/api/jobs/{job_id}")
            assert status.status_code == 200, status.text
            assert status.json()["status"] == "completed", status.text
            expected = {
                "video": b"dubbed-video",
                "original_srt": b"source subtitles",
                "translated_srt": b"translated subtitles",
            }
            assert {artifact["name"] for artifact in status.json()["artifacts"]} == set(expected)
            for name, data in expected.items():
                download = browser.get(f"/api/jobs/{job_id}/artifacts/{name}")
                assert download.status_code == 200, download.text
                assert download.content == data
                assert "attachment" in download.headers["content-disposition"]
            with httpx.Client(base_url=origin, timeout=10) as anonymous:
                assert anonymous.get(f"/api/jobs/{job_id}/artifacts/video").status_code == 401
        assert not list(settings.state_directory.glob("upload-*"))
        assert not list(settings.state_directory.glob("download-*"))
        assert not list(settings.state_directory.glob("artifact-*"))
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
        listener.close()
    assert not server_thread.is_alive()


@pytest.mark.parametrize(
    ("error_type", "code"),
    [
        (TranslationError, "translation_unavailable"),
        (SpeechSynthesisError, "voice_unavailable"),
        (TranscriptionError, "recognition_failed"),
        (DependencyError, "service_unavailable"),
        (RuntimeError, "processing_failed"),
    ],
)
def test_worker_provider_failures_send_safe_typed_code(error_type, code, capsys):
    server = FakeServer(claim())

    def factory(*_args, **_kwargs):
        raise error_type("worker-secret operator-key lease-secret")

    with worker(server, factory=factory, environment={"DEEPL_API_KEY": "operator-key"}) as remote:
        remote.run_once()
    assert server.calls[-1][0].endswith("/fail")
    failure = json.loads(server.calls[-1][1])
    assert failure["failure_code"] == code
    assert failure["cancelled"] is False
    logged = capsys.readouterr().out
    for secret in ("worker-secret", "operator-key", "lease-secret"):
        assert secret not in failure["error"]
        assert secret not in logged


def test_optional_translation_capabilities_require_operator_credentials_and_dependencies():
    with mock.patch("linguoai.web_worker.importlib.util.find_spec", return_value=object()):
        no_keys = worker_capabilities({})["translation_services"]
        assert "deepl" not in no_keys and "gemini" not in no_keys
        configured = worker_capabilities(
            {
                "DEEPL_API_KEY": "operator-deepl-key",
                "GEMINI_API_KEY": "operator-gemini-key",
            }
        )["translation_services"]
        assert "deepl" in configured and "gemini" in configured
    with mock.patch("linguoai.web_worker.importlib.util.find_spec", return_value=None):
        unavailable = worker_capabilities(
            {
                "DEEPL_API_KEY": "operator-deepl-key",
                "GEMINI_API_KEY": "operator-gemini-key",
            }
        )["translation_services"]
        assert "deepl" not in unavailable and "gemini" not in unavailable


class ChunkServer(FakeServer):
    def __init__(self):
        super().__init__(claim())
        self.session = None
        self.buffer = bytearray()
        self.puts = []
        self.accepted = {}
        self.put_errors = []
        self.lost_append = False
        self.stale_recovery = False
        self.lost_create = False
        self.lost_complete = False
        self.creations = 0
        self.finalizations = 0
        self.status_gets = 0
        self.finalizing_polls = 2
        self.never_complete = False
        self.fail_finalizing = False
        self.deleted = 0
        self.interrupt = None
        self.override = None
        self.on_status = None

    def row(self):
        row = {**self.session, "offset": len(self.buffer)}
        if self.override is not None:
            row.update(self.override)
        return row

    def __call__(self, request):
        path = request.url.path
        if "/uploads/" not in path:
            return super().__call__(request)
        assert request.headers["authorization"] == "Bearer worker-secret"
        with self.lock:
            body = request.read()
            self.calls.append((path, b"" if request.method == "PUT" else body))
            if request.method == "POST" and path.endswith("/uploads/video"):
                data = json.loads(body)
                assert data["worker_id"] == "worker-1" and data["lease_token"] == "lease-secret"
                self.creations += 1
                if self.session is None:
                    self.session = {
                        "id": "upload-video",
                        "job_id": self.job["id"],
                        "size_bytes": data["size_bytes"],
                        "chunk_bytes": UPLOAD_CHUNK_BYTES,
                        "status": "uploading",
                        "kind": "artifact",
                        "artifact_name": "video",
                        "filename": data["filename"],
                        "error": None,
                    }
                else:
                    assert self.session["size_bytes"] == data["size_bytes"]
                    assert self.session["filename"] == data["filename"]
                if self.lost_create:
                    self.lost_create = False
                    raise httpx.ReadError("create response lost", request=request)
                return httpx.Response(200, json=self.row())

            if path.endswith("/complete"):
                data = json.loads(body)
                assert data["worker_id"] == "worker-1" and data["lease_token"] == "lease-secret"
                assert len(self.buffer) == self.session["size_bytes"]
                if self.session["status"] == "uploading":
                    self.finalizations += 1
                    self.session["status"] = "finalizing"
                if self.lost_complete:
                    self.lost_complete = False
                    raise httpx.ReadError("complete response lost", request=request)
                return httpx.Response(202, json=self.row())

            assert request.headers["x-worker-id"] == "worker-1"
            assert request.headers["x-job-lease"] == "lease-secret"
            assert path.endswith("/uploads/upload-video")
            if request.method == "DELETE":
                self.deleted += 1
                self.buffer.clear()
                self.session["status"] = "cancelled"
                return httpx.Response(204)
            if request.method == "GET":
                self.status_gets += 1
                if self.on_status is not None:
                    self.on_status()
                if self.session["status"] == "finalizing":
                    if self.fail_finalizing:
                        self.session["status"] = "failed"
                        self.session["error"] = "provider-secret /private/server"
                    elif not self.never_complete:
                        self.finalizing_polls -= 1
                        if self.finalizing_polls <= 0:
                            self.session["status"] = "completed"
                            self.artifacts.append("video")
                row = self.row()
                if self.stale_recovery and len(self.buffer) > 0:
                    self.stale_recovery = False
                    row["offset"] = 0
                return httpx.Response(200, json=row)
            assert request.method == "PUT"
            offset = int(request.url.params["offset"])
            digest = request.headers["x-chunk-sha256"]
            assert request.headers["content-type"] == "application/octet-stream"
            assert digest == hashlib.sha256(body).hexdigest()
            assert 0 < len(body) <= UPLOAD_CHUNK_BYTES
            self.puts.append((offset, len(body)))
            if self.put_errors:
                return httpx.Response(self.put_errors.pop(0), json={"detail": "transfer failed"})
            if offset == len(self.buffer):
                self.buffer.extend(body)
                self.accepted[offset] = digest
            else:
                assert self.accepted[offset] == digest
            if self.interrupt:
                if self.interrupt == "cancel":
                    self.cancel_after = self.heartbeats + 1
                else:
                    self.lose_after = self.heartbeats + 1
                self.interrupt = None
                return httpx.Response(503, json={"detail": "retry while heartbeat interrupts"})
            if self.lost_append:
                self.lost_append = False
                raise httpx.ReadError("append response lost", request=request)
            return httpx.Response(200, json=self.row())


class LargePipeline(FakePipeline):
    size = UPLOAD_CHUNK_BYTES + 321

    def run_path(self, source, target):
        result = super().run_path(source, target)
        target.write_bytes(b"v" * self.size)
        return result


def chunk_worker(server, **kwargs):
    remote = worker(server, factory=LargePipeline, **kwargs)
    remote.upload_retry_base_seconds = 0
    remote.upload_poll_seconds = 0.001
    return remote


def test_large_artifact_uses_hash_checked_chunks_and_waits_for_finalization(tmp_path):
    server = ChunkServer()
    with chunk_worker(server, temp_directory=tmp_path) as remote:
        remote.run_once()
    assert server.puts == [(0, UPLOAD_CHUNK_BYTES), (UPLOAD_CHUNK_BYTES, 321)]
    assert bytes(server.buffer) == b"v" * LargePipeline.size
    assert server.status_gets == 2
    assert server.finalizations == 1
    assert server.artifacts == ["video", "original_srt", "translated_srt"]
    assert server.calls[-1][0].endswith("/complete")
    assert not any(path.endswith("/artifacts/video") for path, _body in server.calls)
    assert server.deleted == 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("stale_recovery", [False, True])
def test_chunk_append_lost_response_recovers_offset_without_duplicate_bytes(stale_recovery):
    server = ChunkServer()
    server.lost_append = True
    server.stale_recovery = stale_recovery
    with chunk_worker(server) as remote:
        remote.run_once()
    assert len(server.buffer) == LargePipeline.size
    assert len(server.accepted) == 2
    offsets = [offset for offset, _length in server.puts]
    assert offsets == ([0, 0, UPLOAD_CHUNK_BYTES] if stale_recovery else [0, UPLOAD_CHUNK_BYTES])
    assert server.calls[-1][0].endswith("/complete")


def test_chunk_creation_and_completion_response_loss_reuse_session_idempotently():
    server = ChunkServer()
    server.lost_create = True
    server.lost_complete = True
    with chunk_worker(server) as remote:
        remote.run_once()
    assert server.creations == 2
    assert server.finalizations == 1
    assert len(server.accepted) == 2
    assert server.calls[-1][0].endswith("/complete")
    assert server.deleted == 0


def test_chunk_transient_upload_failure_retries_only_three_times_and_discards_session():
    server = ChunkServer()
    server.put_errors = [503] * 10
    with chunk_worker(server) as remote:
        remote.run_once()
    assert len(server.puts) == 4
    assert server.deleted == 1
    assert server.finalizations == 0
    assert server.calls[-1][0].endswith("/fail")
    assert not server.artifacts


def test_chunk_forbidden_error_is_not_retried_and_does_not_publish():
    server = ChunkServer()
    server.put_errors = [403]
    with chunk_worker(server) as remote:
        remote.run_once()
    assert len(server.puts) == 1
    assert server.deleted == 1
    assert server.status_gets == 0
    assert server.calls[-1][0].endswith("/fail")


@pytest.mark.parametrize("interrupt", ["cancel", "lease"])
def test_heartbeat_interrupts_chunk_retry_backoff_and_discards_partial_result(interrupt, tmp_path):
    server = ChunkServer()
    server.interrupt = interrupt
    with chunk_worker(server, temp_directory=tmp_path) as remote:
        remote.upload_retry_base_seconds = 0.2
        remote.run_once()
    assert len(server.puts) == 1
    assert server.deleted == 1
    assert not server.artifacts
    assert not any(path.endswith("/uploads/upload-video/complete") for path, _body in server.calls)
    failures = [json.loads(body) for path, body in server.calls if path.endswith("/fail")]
    if interrupt == "cancel":
        assert failures[0]["cancelled"] is True
    else:
        assert not failures
    assert list(tmp_path.iterdir()) == []


def test_chunk_finalization_failure_cannot_publish_partial_artifact():
    server = ChunkServer()
    server.fail_finalizing = True
    with chunk_worker(server) as remote:
        remote.run_once()
    assert server.finalizations == 1
    assert server.deleted == 1
    assert not server.artifacts
    assert server.calls[-1][0].endswith("/fail")
    assert b"provider-secret" not in server.calls[-1][1]


@pytest.mark.parametrize(
    "metadata",
    [
        {"offset": -1},
        {"offset": LargePipeline.size + 1},
        {"offset": True},
        {"size_bytes": LargePipeline.size + 1},
        {"size_bytes": float(LargePipeline.size)},
        {"job_id": "someone-else"},
        {"artifact_name": "translated_srt"},
        {"filename": "../../private.mp4"},
    ],
)
def test_chunk_invalid_session_metadata_is_rejected_and_session_discarded(metadata):
    server = ChunkServer()
    server.override = metadata
    with chunk_worker(server) as remote:
        remote.run_once()
    assert not server.puts
    assert server.deleted == 1
    assert not server.artifacts
    assert server.calls[-1][0].endswith("/fail")


def test_chunk_finalization_deadline_is_bounded_while_lease_is_active():
    server = ChunkServer()
    server.never_complete = True
    clock = {"now": 0.0}
    server.on_status = lambda: clock.__setitem__("now", clock["now"] + 0.25)
    with chunk_worker(server) as remote:
        remote._upload_clock = lambda: clock["now"]
        remote.upload_deadline_seconds = 1
        remote.run_once()
    assert server.status_gets == 4
    assert server.deleted == 1
    assert server.calls[-1][0].endswith("/fail")
    assert b"time limit" in server.calls[-1][1]
    assert not server.artifacts


def test_chunk_finalization_ready_state_waits_for_durable_server_retry():
    server = ChunkServer()
    polls = {"count": 0}

    def recover():
        polls["count"] += 1
        if polls["count"] == 1:
            server.session["status"] = "ready"
        elif polls["count"] == 2:
            server.session["status"] = "finalizing"

    server.on_status = recover
    with chunk_worker(server) as remote:
        remote.run_once()
    assert server.status_gets == 3
    assert server.finalizations == 1
    assert server.deleted == 0
    assert server.artifacts == ["video", "original_srt", "translated_srt"]
