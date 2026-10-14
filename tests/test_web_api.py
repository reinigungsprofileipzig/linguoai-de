import json
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from linguoai.web_api import create_app
from linguoai.web_settings import ServerSettings, Sessions, password_hash, verify_password


class WebApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.encoded = password_hash("test-browser-password")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = ServerSettings(
            Path(self.temporary.name),
            {"alice": self.encoded, "bob": self.encoded},
            "s" * 48,
            "w" * 48,
            max_upload_bytes=4096,
        )
        disk = namedtuple("usage", "total used free")(20 * 1024**3, 0, 20 * 1024**3)
        self.disk_patch = patch("linguoai.web_api.shutil.disk_usage", return_value=disk)
        self.disk_patch.start()
        self.addCleanup(self.disk_patch.stop)
        self.app = create_app(self.settings)
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8765")
        self.addCleanup(self.client.close)
        self.change_headers = {"X-LinguoAI-Request": "1"}
        self.worker_headers = {"Authorization": "Bearer " + self.settings.worker_token}

    def login(self, username="alice"):
        response = self.client.post(
            "/api/auth/login",
            json={"username": username, "password": "test-browser-password"},
            headers=self.change_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def upload(self, **overrides):
        options = {"target_language": "de", **overrides}
        return self.client.post(
            "/api/jobs",
            data={"options": json.dumps(options)},
            files={"file": ("sample.mp4", b"video data", "video/mp4")},
            headers=self.change_headers,
        )

    def test_login_cookie_is_http_only_and_password_hash_not_plaintext(self):
        response = self.login()
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertNotIn("test-browser-password", cookie)
        self.assertTrue(verify_password("test-browser-password", self.encoded))
        self.assertFalse(verify_password("wrong-password", self.encoded))
        sessions = Sessions(self.settings)
        self.assertIsNone(sessions.owner(sessions.issue("alice") + "tampered"))

    def test_browser_auth_and_csrf_are_required_before_upload(self):
        self.assertEqual(self.upload().status_code, 401)
        self.login()
        self.assertEqual(self.client.post("/api/jobs", content=b"not multipart").status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/auth/logout",
                headers={**self.change_headers, "Origin": "https://evil.example"},
            ).status_code,
            403,
        )

    def test_owner_isolation_and_worker_claim_are_separate(self):
        self.login()
        response = self.upload()
        self.assertEqual(response.status_code, 201, response.text)
        job_id = response.json()["id"]
        public = self.client.get(f"/api/jobs/{job_id}").json()
        self.assertNotIn("input_ref", public)
        self.assertNotIn("lease_token", public)
        self.assertEqual(
            self.client.post("/api/worker/claim", json={"worker_id": "test"}).status_code, 401
        )
        self.login("bob")
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}").status_code, 404)
        self.assertEqual(self.client.get("/api/jobs").json()["jobs"], [])
        claim = self.client.post(
            "/api/worker/claim",
            headers=self.worker_headers,
            json={"worker_id": "test", "capabilities": {"asr_engines": ["whisper"]}},
        )
        self.assertEqual(claim.status_code, 200, claim.text)
        leased = claim.json()["job"]
        self.assertEqual(leased["id"], job_id)
        wrong = self.client.get(
            f"/api/worker/jobs/{job_id}/input",
            headers={**self.worker_headers, "X-Worker-ID": "test", "X-Job-Lease": "stale-token"},
        )
        self.assertEqual(wrong.status_code, 409, wrong.text)

    def test_path_and_secret_options_rejected(self):
        self.login()
        for options in (
            {"parakeet_model_directory": "/etc"},
            {"gemini_api_key": "secret"},
            {"source_language": "ja", "asr_engine": "parakeet"},
            {"tts_engine": "chatterbox"},
        ):
            with self.subTest(options=options):
                self.assertEqual(self.upload(**options).status_code, 422)
        self.assertEqual(self.client.get("/api/jobs").json()["jobs"], [])

    def test_upload_size_and_active_queue_limit(self):
        self.login()
        response = self.client.post(
            "/api/jobs",
            data={"options": '{"target_language":"de"}'},
            files={"file": ("sample.mp4", b"x" * 5000)},
            headers=self.change_headers,
        )
        self.assertEqual(response.status_code, 413, response.text)
        for _ in range(3):
            self.assertEqual(self.upload().status_code, 201)
        self.assertEqual(self.upload().status_code, 409)

    def test_parakeet_only_offered_after_worker_reports_real_profile(self):
        self.assertEqual(self.client.get("/api/capabilities").json()["default_engine"], "whisper")
        response = self.client.post(
            "/api/worker/claim",
            headers=self.worker_headers,
            json={
                "worker_id": "cpu",
                "capabilities": {
                    "asr_engines": ["parakeet"],
                    "parakeet_profiles": ["multilingual-v3"],
                },
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        caps = self.client.get("/api/capabilities").json()
        self.assertTrue(caps["worker_online"])
        self.assertEqual(caps["default_engine"], "parakeet")
        self.assertEqual(len(caps["engines"][0]["profiles"][0]["languages"]), 25)

    def test_cancel_blocks_completion_and_public_downloads(self):
        self.login()
        job_id = self.upload().json()["id"]
        claim = self.client.post(
            "/api/worker/claim", headers=self.worker_headers, json={"worker_id": "test"}
        ).json()["job"]
        cancelled = self.client.post(f"/api/jobs/{job_id}/cancel", headers=self.change_headers)
        self.assertTrue(cancelled.json()["cancel_requested"])
        beat = self.client.post(
            f"/api/worker/jobs/{job_id}/heartbeat",
            headers=self.worker_headers,
            json={
                "worker_id": "test",
                "lease_token": claim["lease_token"],
                "progress": 3,
                "stage": "downloading",
            },
        )
        self.assertTrue(beat.json()["cancel_requested"])
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}/artifacts/video").status_code, 404)
        finish = self.client.post(
            f"/api/worker/jobs/{job_id}/complete",
            headers=self.worker_headers,
            json={"worker_id": "test", "lease_token": claim["lease_token"]},
        )
        self.assertEqual(finish.status_code, 200, finish.text)
        self.assertEqual(finish.json()["status"], "cancelled")
        self.assertEqual(finish.json()["artifacts"], [])

    def test_non_ascii_worker_header_is_denied_without_server_error(self):
        response = self.client.post(
            "/api/worker/claim",
            headers=[(b"authorization", b"Bearer \xff")],
            json={"worker_id": "worker"},
        )
        self.assertEqual(response.status_code, 401)

    def test_unavailable_translation_rejected_for_online_worker(self):
        self.login()
        self.client.post(
            "/api/worker/claim",
            headers=self.worker_headers,
            json={
                "worker_id": "cpu",
                "capabilities": {
                    "asr_engines": ["whisper"],
                    "translation_services": ["google"],
                    "tts_engines": ["edge"],
                },
            },
        )
        self.assertEqual(self.upload(translation_service="nllb").status_code, 422)
        caps = self.client.get("/api/capabilities").json()
        self.assertEqual([item["id"] for item in caps["translation_services"]], ["google"])

    def test_low_disk_rejects_before_spooling_or_creating_job(self):
        self.login()
        disk = namedtuple("usage", "total used free")(1024**3, 1024**3, 0)
        with patch("linguoai.web_api.shutil.disk_usage", return_value=disk):
            response = self.upload()
        self.assertEqual(response.status_code, 507, response.text)
        self.assertEqual(self.client.get("/api/jobs").json()["jobs"], [])
        self.assertEqual(list((self.settings.state_directory / "files").rglob("*.tmp")), [])

    def test_ready_time_only_published_after_all_results_are_complete(self):
        self.login()
        queued = self.upload().json()
        self.assertIsNone(queued["available_at"])
        claim = self.client.post(
            "/api/worker/claim", headers=self.worker_headers, json={"worker_id": "test"}
        ).json()["job"]
        ownership = {"worker_id": "test", "lease_token": claim["lease_token"]}
        for name in ("video", "original_srt", "translated_srt"):
            uploaded = self.client.post(
                f"/api/worker/jobs/{queued['id']}/artifacts/{name}",
                headers=self.worker_headers,
                data=ownership,
                files={"file": ("output.mp4" if name == "video" else "output.srt", b"result")},
            )
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
        completed = self.client.post(
            f"/api/worker/jobs/{queued['id']}/complete",
            headers=self.worker_headers,
            json=ownership,
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        public = completed.json()
        self.assertEqual(public["status"], "completed")
        self.assertEqual(public["available_at"], public["finished_at"])
        self.assertGreater(public["available_at"], public["created_at"])
        self.assertGreater(public["expires_at"], public["available_at"])
        self.assertEqual(len(public["artifacts"]), 3)
        self.assertNotIn("lease_token", public)
        self.assertNotIn("input_ref", public)

    def test_typed_provider_failures_publish_safe_helpful_messages(self):
        self.login()
        cases = (
            ("translation_unavailable", "Übersetzungsdienst"),
            ("voice_unavailable", "Sprachausgabe"),
            ("recognition_failed", "Tonspur"),
            ("service_unavailable", "Modell"),
            ("processing_failed", "Datei"),
            (None, "Datei"),
        )
        for code, useful_detail in cases:
            with self.subTest(failure_code=code):
                uploaded = self.upload()
                self.assertEqual(uploaded.status_code, 201, uploaded.text)
                job_id = uploaded.json()["id"]
                claimed = self.client.post(
                    "/api/worker/claim", headers=self.worker_headers, json={"worker_id": "test"}
                ).json()["job"]
                raw_error = (
                    "GEMINI_API_KEY=private-provider-key C:/private/credentials.json "
                    + self.settings.worker_token
                    + " "
                    + claimed["lease_token"]
                )
                response = self.client.post(
                    f"/api/worker/jobs/{job_id}/fail",
                    headers=self.worker_headers,
                    json={
                        "worker_id": "test",
                        "lease_token": claimed["lease_token"],
                        "failure_code": code,
                        "error": raw_error,
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "failed")
                self.assertIn(useful_detail, response.json()["error"])
                public = self.client.get(f"/api/jobs/{job_id}")
                self.assertEqual(public.json()["error"], response.json()["error"])
                listing = self.client.get("/api/jobs")
                for secret in (
                    "private-provider-key",
                    "C:/private",
                    self.settings.worker_token,
                    claimed["lease_token"],
                    "GEMINI_API_KEY",
                ):
                    self.assertNotIn(secret, response.text + public.text + listing.text)

    def test_cancelled_failure_keeps_cancel_message_with_provider_code(self):
        self.login()
        job_id = self.upload().json()["id"]
        claimed = self.client.post(
            "/api/worker/claim", headers=self.worker_headers, json={"worker_id": "test"}
        ).json()["job"]
        response = self.client.post(
            f"/api/worker/jobs/{job_id}/fail",
            headers=self.worker_headers,
            json={
                "worker_id": "test",
                "lease_token": claimed["lease_token"],
                "cancelled": True,
                "failure_code": "translation_unavailable",
                "error": "private-provider-key",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertEqual(response.json()["error"], "Verarbeitung abgebrochen.")

    def test_unknown_failure_code_cannot_finish_job(self):
        self.login()
        job_id = self.upload().json()["id"]
        claimed = self.client.post(
            "/api/worker/claim", headers=self.worker_headers, json={"worker_id": "test"}
        ).json()["job"]
        rejected = self.client.post(
            f"/api/worker/jobs/{job_id}/fail",
            headers=self.worker_headers,
            json={
                "worker_id": "test",
                "lease_token": claimed["lease_token"],
                "failure_code": "arbitrary_provider_message",
                "error": "secret",
            },
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)
        public = self.client.get(f"/api/jobs/{job_id}").json()
        self.assertEqual(public["status"], "running")
        self.assertIsNone(public["error"])

    def test_deepl_and_gemini_only_offered_for_configured_worker(self):
        self.login()
        initial = self.client.get("/api/capabilities").json()
        self.assertEqual([item["id"] for item in initial["translation_services"]], ["google"])
        self.client.post(
            "/api/worker/claim",
            headers=self.worker_headers,
            json={
                "worker_id": "configured",
                "capabilities": {
                    "asr_engines": ["whisper"],
                    "translation_services": ["google"],
                    "tts_engines": ["edge"],
                },
            },
        )
        for provider in ("deepl", "gemini"):
            self.assertEqual(self.upload(translation_service=provider).status_code, 422)
        registered = self.client.post(
            "/api/worker/claim",
            headers=self.worker_headers,
            json={
                "worker_id": "configured",
                "capabilities": {
                    "asr_engines": ["whisper"],
                    "translation_services": ["google", "deepl", "gemini", "nllb"],
                    "tts_engines": ["edge"],
                },
            },
        )
        self.assertEqual(registered.status_code, 200, registered.text)
        caps = self.client.get("/api/capabilities").json()
        labels = {item["id"]: item["label"] for item in caps["translation_services"]}
        self.assertEqual(set(labels), {"google", "deepl", "gemini", "nllb"})
        self.assertEqual(labels["deepl"], "DeepL · online")
        self.assertEqual(labels["gemini"], "Gemini · online")
        for provider in ("deepl", "gemini"):
            uploaded = self.upload(translation_service=provider)
            self.assertEqual(uploaded.status_code, 201, uploaded.text)
            self.assertEqual(uploaded.json()["options"]["translation_service"], provider)
            self.assertNotIn("api_key", uploaded.text)
        self.assertEqual(self.upload(deepl_api_key="private-provider-key").status_code, 422)
        self.assertEqual(self.upload(gemini_api_key="private-provider-key").status_code, 422)

    def test_drive_activation_preserves_existing_local_downloads_and_routes_new_uploads(self):
        from dataclasses import replace
        from unittest.mock import Mock

        source = self.settings.state_directory / "legacy.mp4"
        source.write_bytes(b"legacy private video")
        jobs, local = self.app.state.store, self.app.state.storage
        row = jobs.create("alice", source.name, {"target_language": "de"})
        ref = local.upload(source, job_id=row["id"], name="input")
        jobs.queue(row["id"], "alice", ref, source.stat().st_size)
        claim = jobs.claim("legacy-worker")
        for name in ("video", "original_srt", "translated_srt"):
            artifact = local.upload(source, job_id=row["id"], name=name)
            jobs.add_artifact(
                row["id"],
                "legacy-worker",
                claim["lease_token"],
                name,
                artifact,
                source.stat().st_size,
            )
        jobs.complete(row["id"], "legacy-worker", claim["lease_token"])
        drive = Mock(spec=("upload", "download", "delete"))
        drive.upload.return_value = "drive:new-file"
        settings = replace(self.settings, storage_backend="drive", drive_folder_id="private-folder")
        with patch("linguoai.web_api.DriveStorage", return_value=drive):
            app = create_app(settings, store=jobs)
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "alice", "password": "test-browser-password"},
                headers=self.change_headers,
            )
            self.assertEqual(login.status_code, 200)
            with patch(
                "linguoai.web_api.tempfile.TemporaryDirectory",
                side_effect=AssertionError("Local files should stream without a second copy"),
            ):
                for name in ("video", "original_srt", "translated_srt"):
                    downloaded = client.get(f"/api/jobs/{row['id']}/artifacts/{name}")
                    self.assertEqual(downloaded.status_code, 200)
                    self.assertEqual(downloaded.content, source.read_bytes())
                    self.assertEqual(downloaded.headers["cache-control"], "no-store")
            uploaded = client.post(
                "/api/jobs",
                data={"options": json.dumps({"target_language": "de"})},
                files={"file": ("new.mp4", b"new private video", "video/mp4")},
                headers=self.change_headers,
            )
            self.assertEqual(uploaded.status_code, 201, uploaded.text)
            drive.upload.assert_called_once()
            drive.download.assert_not_called()
            self.assertEqual(
                jobs.get(uploaded.json()["id"], "alice")["input_ref"], "drive:new-file"
            )
