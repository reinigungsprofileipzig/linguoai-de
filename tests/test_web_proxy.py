"""Real Uvicorn coverage for a proxy that strips the public URL prefix."""

import json
import socket
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
import uvicorn

from linguoai.domain import BatchResult, JobResult, ProgressUpdate, Stage
from linguoai.web_api import create_app, entrypoint
from linguoai.web_settings import ServerSettings, password_hash
from linguoai.web_worker import RemoteWorker


class ProxyPipeline:
    def __init__(self, config, *, event_sink, cancel_event):
        self.config = config
        self.event_sink = event_sink
        self.cancel_event = cancel_event

    def cancel(self):
        self.cancel_event.set()

    def run_path(self, source, target):
        assert source.read_bytes() == b"proxy-input-video"
        self.event_sink(ProgressUpdate(50, Stage.TRANSLATING, "Translate"))
        original = target.parent / "original.srt"
        translated = target.parent / "translated.srt"
        target.write_bytes(b"proxy-result-video")
        original.write_bytes(b"proxy-original-subtitles")
        translated.write_bytes(b"proxy-translated-subtitles")
        return BatchResult(completed=(JobResult(source, target, original, translated, "en", 1),))


def test_uvicorn_prefix_strip_proxy_serves_assets_auth_jobs_and_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "linguoai.web_api.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=2**40, used=0, free=2**40),
    )
    prefix = "/linguoai"
    public_origin = "https://example.com"
    worker_token = "w" * 48
    website = Path(__file__).resolve().parents[1] / "website"
    settings = ServerSettings(
        state_directory=tmp_path / "state",
        users={"alice": password_hash("proxy-browser-password")},
        session_secret="s" * 48,
        worker_token=worker_token,
        public_url=public_origin + prefix,
        max_upload_bytes=4096,
        website_directory=website,
    )
    app = create_app(settings)
    with (
        mock.patch.object(ServerSettings, "from_env", return_value=settings),
        mock.patch("sys.argv", ["linguoai-web", "--host", "127.0.0.1", "--port", "8765"]),
        mock.patch("uvicorn.run") as launch,
    ):
        entrypoint()
    launch.assert_called_once()
    launch_options = launch.call_args.kwargs
    assert launch_options["root_path"] == prefix
    scopes = []
    scope_lock = threading.Lock()

    async def capture_scope(scope, receive, send):
        if scope["type"] == "http":
            with scope_lock:
                scopes.append(
                    {
                        "path": scope["path"],
                        "root_path": scope["root_path"],
                        "host": dict(scope["headers"]).get(b"host"),
                    }
                )
        await app(scope, receive, send)

    ready = threading.Event()

    class TestServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started:
                ready.set()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    backend_origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = TestServer(
        uvicorn.Config(
            capture_scope,
            root_path=launch_options["root_path"],
            log_level="error",
            access_log=False,
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1",
        )
    )
    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    server_thread.start()
    try:
        assert ready.wait(10), "Uvicorn did not start with the public root_path"
        with httpx.Client(base_url=backend_origin, timeout=10, follow_redirects=False) as backend:
            public_paths = []

            def proxy(request):
                # Nginx strips the prefix from the URL before forwarding to Uvicorn.
                assert request.url.host == "example.com"
                assert request.url.path.startswith(prefix + "/")
                public_paths.append(request.url.path)
                forwarded_path = request.url.path.removeprefix(prefix)
                if request.url.query:
                    forwarded_path += "?" + request.url.query.decode("ascii")
                headers = dict(request.headers)
                headers["host"] = "example.com"
                headers["x-forwarded-proto"] = "https"
                headers["x-forwarded-for"] = "203.0.113.10"
                response = backend.request(
                    request.method, forwarded_path, headers=headers, content=request.read()
                )
                return httpx.Response(
                    response.status_code, headers=response.headers, content=response.content
                )

            transport = httpx.MockTransport(proxy)
            public_url = public_origin + prefix + "/"
            with httpx.Client(base_url=public_url, transport=transport) as browser:
                home = browser.get("/")
                assert home.status_code == 307
                assert home.headers["location"] == prefix + "/web.html"
                assert prefix + prefix not in home.headers["location"]
                page = browser.get("/web.html")
                assert page.status_code == 200
                assert "text/html" in page.headers["content-type"]
                for name in ("simple.css", "web-app.css", "web-app.js", "logo-mark.svg"):
                    response = browser.get("/assets/" + name)
                    assert response.status_code == 200, (name, response.text)
                    assert response.content == (website / "assets" / name).read_bytes()
                assert browser.get("/healthz").json() == {"status": "ok"}
                capabilities = browser.get("/api/capabilities")
                assert capabilities.status_code == 200
                assert capabilities.json()["authenticated"] is False
                assert "de" in capabilities.json()["languages"]
                assert browser.get("/api/jobs").status_code == 401

                browser.headers.update({"X-LinguoAI-Request": "1", "Origin": public_origin})
                login = browser.post(
                    "/api/auth/login",
                    json={"username": "alice", "password": "proxy-browser-password"},
                )
                assert login.status_code == 200, login.text
                cookie = login.headers["set-cookie"].lower()
                assert "path=/linguoai" in cookie
                assert "secure" in cookie and "httponly" in cookie
                assert browser.get("/api/capabilities").json()["authenticated"] is True
                assert browser.get("/api/jobs").json() == {"jobs": []}
                upload = browser.post(
                    "/api/jobs",
                    data={"options": json.dumps({"target_language": "de"})},
                    files={"file": ("proxy.mp4", b"proxy-input-video", "video/mp4")},
                )
                assert upload.status_code == 201, upload.text
                job_id = upload.json()["id"]

                def worker_client(**kwargs):
                    return httpx.Client(transport=transport, **kwargs)

                with RemoteWorker(
                    public_url,
                    worker_token,
                    "proxy-worker",
                    client_factory=worker_client,
                    pipeline_factory=ProxyPipeline,
                    environment={},
                    request_timeout_seconds=10,
                ) as worker:
                    assert worker.run_once() is True

                result = browser.get(f"/api/jobs/{job_id}")
                assert result.status_code == 200, result.text
                assert result.json()["status"] == "completed", result.text
                expected = {
                    "video": b"proxy-result-video",
                    "original_srt": b"proxy-original-subtitles",
                    "translated_srt": b"proxy-translated-subtitles",
                }
                for artifact in result.json()["artifacts"]:
                    url = artifact["download_url"]
                    assert url == f"{prefix}/api/jobs/{job_id}/artifacts/{artifact['name']}"
                    assert prefix + prefix not in url
                    downloaded = browser.get(public_origin + url)
                    assert downloaded.status_code == 200, downloaded.text
                    assert downloaded.content == expected[artifact["name"]]
                assert len(result.json()["artifacts"]) == len(expected)
                logout = browser.post("/api/auth/logout")
                assert logout.status_code == 200
                assert browser.get("/api/jobs").status_code == 401

            assert all(scope["root_path"] == prefix for scope in scopes)
            assert all(scope["path"].startswith(prefix + "/") for scope in scopes)
            assert all(scope["host"] == b"example.com" for scope in scopes)
            assert not any(prefix + prefix in scope["path"] for scope in scopes)
            assert prefix + "/assets/web-app.js" in public_paths
            assert any(scope["path"] == prefix + "/assets/web-app.js" for scope in scopes)
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
        listener.close()
    assert not server_thread.is_alive()
