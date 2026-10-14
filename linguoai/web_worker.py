"""Authenticated polling worker for the web queue; all model paths stay operator-side."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import math
import os
import re
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import VIDEO_EXTENSIONS, AppConfig
from .domain import ProgressUpdate
from .errors import (
    CancelledError,
    ConfigurationError,
    DependencyError,
    LinguoAIError,
    SpeechSynthesisError,
    TranscriptionError,
    TranslationError,
)
from .runtime import check_cancelled, redact_secrets

JOB_OPTIONS = frozenset(
    {
        "target_language",
        "source_language",
        "asr_engine",
        "parakeet_profile",
        "translation_service",
        "tts_engine",
        "tts_voice",
    }
)
ARTIFACT_NAMES = ("video", "original_srt", "translated_srt")
UPLOAD_CHUNK_BYTES = 8 * 1024**2
UPLOAD_MAX_RETRIES = 3


class LeaseLostError(LinguoAIError):
    """The server no longer confirms ownership, so this worker must not publish."""


def _failure_code(error: BaseException) -> str:
    if isinstance(error, TranslationError):
        return "translation_unavailable"
    if isinstance(error, SpeechSynthesisError):
        return "voice_unavailable"
    if isinstance(error, TranscriptionError):
        return "recognition_failed"
    if isinstance(error, DependencyError):
        return "service_unavailable"
    return "processing_failed"


def _model_directories(environment: Mapping[str, str]) -> dict[str, Path]:
    directories = {}
    for profile, variable in (
        ("multilingual-v3", "LINGUOAI_PARAKEET_MODEL_DIRECTORY"),
        ("german-primeline", "LINGUOAI_PARAKEET_GERMAN_MODEL_DIRECTORY"),
    ):
        value = environment.get(variable, "").strip()
        if value:
            directories[profile] = Path(value).expanduser()
    return directories


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def worker_capabilities(environment: Mapping[str, str] | None = None) -> dict[str, list[str]]:
    environment = os.environ if environment is None else environment
    profiles = []
    if _installed("sherpa_onnx") and _installed("numpy"):
        for profile, directory in _model_directories(environment).items():
            if (
                directory.is_dir()
                and (directory / "tokens.txt").is_file()
                and all(
                    (directory / f"{role}.int8.onnx").is_file()
                    or (directory / f"{role}.onnx").is_file()
                    for role in ("encoder", "decoder", "joiner")
                )
            ):
                profiles.append(profile)
    translations = ["google"]
    if environment.get("DEEPL_API_KEY") and _installed("deepl"):
        translations.append("deepl")
    if environment.get("GEMINI_API_KEY") and _installed("google.genai"):
        translations.append("gemini")
    if _installed("torch") and _installed("transformers") and _installed("sentencepiece"):
        translations.append("nllb")
    voices = ["edge", "gtts"]
    piper_model = environment.get("LINGUOAI_PIPER_MODEL", "")
    piper_config = environment.get("LINGUOAI_PIPER_CONFIG") or f"{piper_model}.json"
    if (
        piper_model
        and _installed("piper")
        and Path(piper_model).is_file()
        and Path(piper_config).is_file()
    ):
        voices.append("piper")
    return {
        "asr_engines": ["whisper", *(["parakeet"] if profiles else [])],
        "parakeet_profiles": profiles,
        "translation_services": translations,
        "tts_engines": voices,
    }


def build_job_config(
    options: Mapping[str, Any],
    *,
    environment: Mapping[str, str] | None = None,
    temp_directory: Path | None = None,
) -> AppConfig:
    environment = os.environ if environment is None else environment
    if not isinstance(options, Mapping) or set(options) - JOB_OPTIONS:
        raise ConfigurationError("The job contains unsupported worker options.")
    if any(
        value is not None and (not isinstance(value, str) or len(value) > 200)
        for value in options.values()
    ):
        raise ConfigurationError("Job options must be short strings.")
    required_strings = set(options) - {"source_language", "tts_voice"}
    if any(options[key] is None for key in required_strings):
        raise ConfigurationError("A required job option is missing.")
    settings = dict(options)
    profile = settings.get("parakeet_profile", "multilingual-v3")
    settings.update(
        whisper_model=environment.get("LINGUOAI_WHISPER_MODEL", "small"),
        device=environment.get("LINGUOAI_WORKER_DEVICE", "auto"),
        compute_type=environment.get("LINGUOAI_WORKER_COMPUTE_TYPE", "default"),
        parakeet_model_directory=_model_directories(environment).get(profile),
        parakeet_threads=4,
        transcription_workers=1,
        chunk_seconds=30.0,
        gemini_api_key=environment.get("GEMINI_API_KEY", ""),
        deepl_api_key=environment.get("DEEPL_API_KEY", ""),
        temp_directory=temp_directory,
    )
    if settings.get("tts_engine") == "piper":
        settings["piper_model"] = environment.get("LINGUOAI_PIPER_MODEL") or None
        settings["piper_config"] = environment.get("LINGUOAI_PIPER_CONFIG") or None
    return AppConfig(**settings).validated()


class _JobLease:
    def __init__(self, worker: RemoteWorker, job: dict[str, Any], client: Any) -> None:
        self.worker = worker
        self.job = job
        self.client = client
        self.cancel_event = threading.Event()
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.pipeline: Any | None = None
        self.lost = False
        self.progress = 0.0
        self.stage = "downloading"
        self.thread = threading.Thread(target=self._heartbeat_loop, name="linguoai-heartbeat")

    def update(self, progress: float, stage: str) -> None:
        with self.lock:
            self.progress = max(self.progress, min(99, max(0, progress)))
            self.stage = stage

    def pipeline_progress(self, update: ProgressUpdate) -> None:
        self.update(5 + update.percent * 0.85, update.stage.value)

    def attach_pipeline(self, pipeline: Any) -> None:
        with self.lock:
            self.pipeline = pipeline
            cancelled = self.cancel_event.is_set()
        if cancelled:
            pipeline.cancel()

    def _cancel(self, *, lost: bool = False) -> None:
        with self.lock:
            self.lost = self.lost or lost
            self.cancel_event.set()
            pipeline = self.pipeline
        if pipeline is not None:
            pipeline.cancel()

    def pulse(self) -> None:
        with self.lock:
            progress, stage = self.progress, self.stage
        try:
            response = self.client.post(
                f"/api/worker/jobs/{self.job['id']}/heartbeat",
                json={**self.worker._ownership(self.job), "progress": progress, "stage": stage},
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("cancel_requested"):
                self._cancel()
        except Exception as exc:
            self._cancel(lost=True)
            raise LeaseLostError("The server no longer confirms this worker lease.") from exc

    def start(self) -> None:
        self.pulse()
        check_cancelled(self.cancel_event)
        self.thread.start()

    def _heartbeat_loop(self) -> None:
        while not self.stopped.wait(self.worker.heartbeat_seconds):
            try:
                self.pulse()
            except LeaseLostError:
                return

    def stop(self) -> None:
        self.stopped.set()
        if self.thread.ident is not None:
            self.thread.join()

    def check(self) -> None:
        if self.lost:
            raise LeaseLostError("The worker lease was lost; results cannot be published.")
        check_cancelled(self.cancel_event)


class RemoteWorker:
    """One job at a time, plus a separate HTTP heartbeat during inference and transfer."""

    def __init__(
        self,
        server: str,
        token: str,
        worker_id: str,
        *,
        client_factory: Callable[..., Any] | None = None,
        pipeline_factory: Callable[..., Any] | None = None,
        heartbeat_seconds: float = 10,
        request_timeout_seconds: float = 60,
        temp_directory: Path | None = None,
        environment: Mapping[str, str] | None = None,
        upload_deadline_seconds: float = 1800,
        upload_poll_seconds: float = 1,
        upload_retry_base_seconds: float = 0.5,
    ) -> None:
        address = urlsplit(server)
        if (
            address.scheme not in {"http", "https"}
            or not address.hostname
            or address.username is not None
            or address.password is not None
            or address.query
            or address.fragment
            or (address.path and not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/?)*", address.path))
            or (
                address.scheme == "http"
                and address.hostname not in {"localhost", "127.0.0.1", "::1"}
            )
        ):
            raise ConfigurationError(
                "Use an HTTPS server URL with a safe path, or HTTP on localhost."
            )
        if not token or any(character.isspace() for character in token):
            raise ConfigurationError("Set LINGUOAI_WORKER_TOKEN to a nonempty worker token.")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", worker_id):
            raise ConfigurationError(
                "Worker ID must contain 1-80 letters, digits, dots or hyphens."
            )
        if (
            not math.isfinite(heartbeat_seconds)
            or heartbeat_seconds <= 0
            or not math.isfinite(request_timeout_seconds)
            or request_timeout_seconds <= 0
        ):
            raise ConfigurationError("Worker timeouts must be positive and finite.")
        if (
            not math.isfinite(upload_deadline_seconds)
            or upload_deadline_seconds <= 0
            or not math.isfinite(upload_poll_seconds)
            or upload_poll_seconds <= 0
            or not math.isfinite(upload_retry_base_seconds)
            or upload_retry_base_seconds < 0
        ):
            raise ConfigurationError("Upload deadlines and polling intervals must be valid.")
        if client_factory is None:
            try:
                import httpx
            except ImportError as exc:
                raise DependencyError("The remote worker requires the 'web' extra.") from exc
            client_factory = httpx.Client
        if pipeline_factory is None:
            from .pipeline import VideoDubbingPipeline

            pipeline_factory = VideoDubbingPipeline
        self.pipeline_factory = pipeline_factory
        self.environment = os.environ if environment is None else environment
        self.worker_id = worker_id
        self.heartbeat_seconds = heartbeat_seconds
        self.temp_directory = temp_directory
        self.upload_deadline_seconds = upload_deadline_seconds
        self.upload_poll_seconds = upload_poll_seconds
        self.upload_retry_base_seconds = upload_retry_base_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self._upload_clock = time.monotonic
        self._token = token
        self._client_settings = {
            "base_url": server.rstrip("/"),
            "headers": {"Authorization": f"Bearer {token}"},
            "timeout": request_timeout_seconds,
            "follow_redirects": False,
        }
        self._client_factory = client_factory
        self.client = client_factory(**self._client_settings)
        self.stop_event = threading.Event()

    def __enter__(self) -> RemoteWorker:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def close(self) -> None:
        self.stop_event.set()
        self.client.close()

    def _ownership(self, job: Mapping[str, Any]) -> dict[str, str]:
        return {"worker_id": self.worker_id, "lease_token": job["lease_token"]}

    def _redact(self, error: object, *extra_secrets: str) -> str:
        return redact_secrets(
            error,
            self._token,
            self.environment.get("GEMINI_API_KEY", ""),
            self.environment.get("DEEPL_API_KEY", ""),
            *extra_secrets,
        )[:1000]

    def _download(self, job: dict[str, Any], destination: Path, lease: _JobLease) -> None:
        with self.client.stream(
            "GET",
            f"/api/worker/jobs/{job['id']}/input",
            headers={"X-Worker-ID": self.worker_id, "X-Job-Lease": job["lease_token"]},
        ) as response:
            response.raise_for_status()
            with destination.open("xb") as output:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    lease.check()
                    output.write(chunk)
        if not destination.stat().st_size:
            raise LinguoAIError("The downloaded video is empty.")

    def _upload_headers(self, job: Mapping[str, Any]) -> dict[str, str]:
        return {"X-Worker-ID": self.worker_id, "X-Job-Lease": job["lease_token"]}

    def _upload_remaining(self, lease: _JobLease, deadline: float) -> float:
        lease.check()
        remaining = deadline - self._upload_clock()
        if remaining <= 0:
            raise LinguoAIError("Artifact upload exceeded its time limit.")
        return remaining

    def _upload_pause(self, seconds: float, lease: _JobLease, deadline: float) -> None:
        remaining = self._upload_remaining(lease, deadline)
        lease.cancel_event.wait(min(seconds, remaining, 5))
        self._upload_remaining(lease, deadline)

    @staticmethod
    def _transient_upload_error(error: Exception) -> bool:
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status is not None:
            return status == 429 or 500 <= status <= 599
        import httpx

        return isinstance(error, httpx.RequestError)

    def _upload_request_once(
        self, method: str, url: str, *, lease: _JobLease, deadline: float, **kwargs: Any
    ) -> Any:
        remaining = self._upload_remaining(lease, deadline)
        response = self.client.request(
            method, url, timeout=min(self.request_timeout_seconds, remaining), **kwargs
        )
        self._upload_remaining(lease, deadline)
        response.raise_for_status()
        return response

    def _upload_request(
        self, method: str, url: str, *, lease: _JobLease, deadline: float, **kwargs: Any
    ) -> Any:
        for retry in range(UPLOAD_MAX_RETRIES + 1):
            try:
                return self._upload_request_once(
                    method, url, lease=lease, deadline=deadline, **kwargs
                )
            except Exception as error:
                if not self._transient_upload_error(error) or retry == UPLOAD_MAX_RETRIES:
                    raise
                self._upload_pause(
                    min(5, self.upload_retry_base_seconds * 2**retry), lease, deadline
                )
        raise AssertionError("Upload retry loop did not return.")

    @staticmethod
    def _upload_id(row: Any) -> str:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", row["id"])
        ):
            raise LinguoAIError("The server returned an invalid upload session.")
        return row["id"]

    def _upload_row(
        self,
        row: Any,
        *,
        job: Mapping[str, Any],
        name: str,
        filename: str,
        size: int,
        upload_id: str,
    ) -> dict[str, Any]:
        if self._upload_id(row) != upload_id:
            raise LinguoAIError("The server changed the upload session identity.")
        offset, chunk_bytes = row.get("offset"), row.get("chunk_bytes")
        if (
            row.get("job_id") != job["id"]
            or isinstance(row.get("size_bytes"), bool)
            or not isinstance(row.get("size_bytes"), int)
            or row.get("size_bytes") != size
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= size
            or isinstance(chunk_bytes, bool)
            or not isinstance(chunk_bytes, int)
            or chunk_bytes <= 0
            or not isinstance(row.get("status"), str)
            or len(row["status"]) > 40
        ):
            raise LinguoAIError("The server returned invalid upload size or ownership metadata.")
        for key, expected in (
            ("kind", "artifact"),
            ("artifact_name", name),
            ("filename", filename),
        ):
            if key in row and row[key] != expected:
                raise LinguoAIError("The upload session belongs to a different artifact.")
        if row["status"] == "cancelled":
            raise CancelledError("The artifact upload was cancelled.")
        if row["status"] in {"failed", "expired", "aborted"}:
            # Server diagnostics may contain local paths or provider credentials.
            raise LinguoAIError("The server could not finalize this artifact upload.")
        if row["status"] in {"finalizing", "completed"} and offset != size:
            raise LinguoAIError("An incomplete artifact cannot be finalized.")
        return row

    def _upload_artifact(
        self, job: dict[str, Any], name: str, path: Path, lease: _JobLease
    ) -> None:
        if path.stat().st_size > UPLOAD_CHUNK_BYTES:
            self._upload_artifact_chunks(job, name, path, lease)
            return
        lease.check()
        with path.open("rb") as file:
            response = self.client.post(
                f"/api/worker/jobs/{job['id']}/artifacts/{name}",
                data=self._ownership(job),
                files={"file": (path.name, file, "application/octet-stream")},
            )
            response.raise_for_status()
        lease.check()

    def _upload_artifact_chunks(
        self, job: dict[str, Any], name: str, path: Path, lease: _JobLease
    ) -> None:
        size = path.stat().st_size
        deadline = self._upload_clock() + self.upload_deadline_seconds
        base = f"/api/worker/jobs/{job['id']}/uploads"
        headers = self._upload_headers(job)
        upload_id = None
        completed = False

        def checked(row: Any) -> dict[str, Any]:
            return self._upload_row(
                row, job=job, name=name, filename=path.name, size=size, upload_id=upload_id
            )

        try:
            created = self._upload_request(
                "POST",
                f"{base}/{name}",
                lease=lease,
                deadline=deadline,
                json={**self._ownership(job), "filename": path.name, "size_bytes": size},
            ).json()
            upload_id = self._upload_id(created)
            row = checked(created)
            url = f"{base}/{upload_id}"
            if row["status"] not in {"finalizing", "completed"}:
                with path.open("rb") as file:
                    while row["offset"] < size:
                        self._upload_remaining(lease, deadline)
                        offset = row["offset"]
                        file.seek(offset)
                        chunk = file.read(
                            min(UPLOAD_CHUNK_BYTES, row["chunk_bytes"], size - offset)
                        )
                        if not chunk:
                            raise LinguoAIError("The artifact ended before its declared size.")
                        chunk_headers = {
                            **headers,
                            "Content-Type": "application/octet-stream",
                            "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest(),
                        }
                        expected_offset = offset + len(chunk)
                        for retry in range(UPLOAD_MAX_RETRIES + 1):
                            try:
                                received = self._upload_request_once(
                                    "PUT",
                                    url,
                                    lease=lease,
                                    deadline=deadline,
                                    params={"offset": offset},
                                    headers=chunk_headers,
                                    content=chunk,
                                )
                                row = checked(received.json())
                                if row["offset"] != expected_offset:
                                    raise LinguoAIError(
                                        "The server acknowledged an unexpected offset."
                                    )
                                break
                            except Exception as error:
                                if (
                                    not self._transient_upload_error(error)
                                    or retry == UPLOAD_MAX_RETRIES
                                ):
                                    raise
                                self._upload_pause(
                                    min(5, self.upload_retry_base_seconds * 2**retry),
                                    lease,
                                    deadline,
                                )
                                # The append may have succeeded even though its response was lost.
                                recovered = self._upload_request(
                                    "GET", url, lease=lease, deadline=deadline, headers=headers
                                )
                                row = checked(recovered.json())
                                if row["offset"] == expected_offset:
                                    break
                                if row["offset"] != offset:
                                    raise LinguoAIError(
                                        "The recovered upload offset is inconsistent."
                                    ) from error
                        lease.update(90 + 2 * row["offset"] / size, "uploading")

            if row["status"] not in {"finalizing", "completed"}:
                finalizing = self._upload_request(
                    "POST",
                    f"{url}/complete",
                    lease=lease,
                    deadline=deadline,
                    json=self._ownership(job),
                )
                row = checked(finalizing.json())
            while row["status"] != "completed":
                if row["status"] not in {"finalizing", "ready"}:
                    raise LinguoAIError("The server did not start artifact finalization.")
                self._upload_pause(self.upload_poll_seconds, lease, deadline)
                status = self._upload_request(
                    "GET", url, lease=lease, deadline=deadline, headers=headers
                )
                row = checked(status.json())
            self._upload_remaining(lease, deadline)
            completed = True
        finally:
            if upload_id is not None and not completed:
                # Cancellation/lease loss must not block this bounded cleanup attempt.
                with suppress(Exception):
                    self.client.delete(
                        f"{base}/{upload_id}",
                        headers=headers,
                        timeout=min(self.request_timeout_seconds, 5),
                    )

    def _process(self, job: dict[str, Any], lease: _JobLease) -> None:
        suffix = Path(str(job.get("filename", ""))).suffix.lower()
        if suffix not in VIDEO_EXTENSIONS:
            raise ConfigurationError("The job input is not a supported video format.")
        with tempfile.TemporaryDirectory(
            prefix="linguoai-worker-", dir=self.temp_directory
        ) as work:
            directory = Path(work)
            config = build_job_config(
                job.get("options", {}), environment=self.environment, temp_directory=directory
            )
            source = directory / f"input{suffix}"
            self._download(job, source, lease)
            lease.check()
            pipeline = self.pipeline_factory(
                config, event_sink=lease.pipeline_progress, cancel_event=lease.cancel_event
            )
            lease.attach_pipeline(pipeline)
            result = pipeline.run_path(source, directory / "output.mp4")
            lease.check()
            if not result.succeeded or len(result.completed) != 1:
                raise LinguoAIError("The pipeline did not produce one complete result.")
            completed = result.completed[0]
            paths = (
                completed.output_path,
                completed.original_subtitles,
                completed.translated_subtitles,
            )
            for path in paths:
                path = Path(path)
                if (
                    path.is_symlink()
                    or path.resolve().parent != directory.resolve()
                    or not path.is_file()
                    or not path.stat().st_size
                ):
                    raise LinguoAIError(
                        "The pipeline output is incomplete or outside the job directory."
                    )
            lease.update(90, "uploading")
            for index, (name, path) in enumerate(zip(ARTIFACT_NAMES, paths, strict=True)):
                lease.check()
                self._upload_artifact(job, name, Path(path), lease)
                lease.update(93 + index * 2, "uploading")
            lease.stop()
            lease.pulse()
            lease.check()
            response = self.client.post(
                f"/api/worker/jobs/{job['id']}/complete", json=self._ownership(job)
            )
            response.raise_for_status()

    def run_once(self) -> bool:
        response = self.client.post(
            "/api/worker/claim",
            json={
                "worker_id": self.worker_id,
                "capabilities": worker_capabilities(self.environment),
            },
        )
        response.raise_for_status()
        job = response.json().get("job")
        if job is None:
            return False
        if (
            not isinstance(job, dict)
            or not isinstance(job.get("id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", job["id"])
            or not isinstance(job.get("lease_token"), str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", job["lease_token"])
        ):
            raise LinguoAIError("The server returned an invalid job claim.")
        with self._client_factory(**self._client_settings) as heartbeat_client:
            lease = _JobLease(self, job, heartbeat_client)
            try:
                lease.start()
                self._process(job, lease)
            except (Exception, KeyboardInterrupt) as exc:
                print(f"Worker job failed: {self._redact(exc, job['lease_token'])}", flush=True)
                lease.stop()
                if not lease.lost:
                    with suppress(Exception):
                        failure = self.client.post(
                            f"/api/worker/jobs/{job['id']}/fail",
                            json={
                                **self._ownership(job),
                                "error": self._redact(exc, job["lease_token"]),
                                "failure_code": _failure_code(exc),
                                "cancelled": isinstance(exc, (CancelledError, KeyboardInterrupt))
                                or lease.cancel_event.is_set(),
                            },
                        )
                        failure.raise_for_status()
                if isinstance(exc, KeyboardInterrupt):
                    raise
            finally:
                lease.stop()
        return True

    def run(self, poll_seconds: float = 3) -> None:
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise ConfigurationError("Poll interval must be positive and finite.")
        while not self.stop_event.is_set():
            try:
                claimed = self.run_once()
            except Exception as exc:
                print(f"Worker connection error: {self._redact(exc)}", flush=True)
                claimed = False
            if not claimed:
                self.stop_event.wait(poll_seconds)


def entrypoint() -> int:
    parser = argparse.ArgumentParser(description="Run LinguoAI's remote video worker.")
    parser.add_argument("--server", default=os.environ.get("LINGUOAI_SERVER_URL"))
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}-{os.getpid()}")
    parser.add_argument("--once", action="store_true", help="Claim at most one job, then exit.")
    parser.add_argument("--poll-seconds", type=float, default=3)
    args = parser.parse_args()
    if not args.server:
        parser.error("Set --server or LINGUOAI_SERVER_URL.")
    try:
        with RemoteWorker(
            args.server,
            os.environ.get("LINGUOAI_WORKER_TOKEN", ""),
            args.worker_id,
            temp_directory=Path(os.environ["LINGUOAI_WORKER_TEMP"])
            if os.environ.get("LINGUOAI_WORKER_TEMP")
            else None,
        ) as worker:
            if args.once:
                worker.run_once()
            else:
                worker.run(args.poll_seconds)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(redact_secrets(exc, os.environ.get("LINGUOAI_WORKER_TOKEN", "")))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(entrypoint())
