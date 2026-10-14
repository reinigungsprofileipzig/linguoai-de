"""Geschützte Upload-API; die eigentliche Verarbeitung läuft in separaten Workern."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import logging
import shutil
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import (
    LANGUAGES,
    PARAKEET_V3_LANGUAGES,
    TRANSLATION_SERVICES,
    VIDEO_EXTENSIONS,
    AppConfig,
)
from .web_chunk_api import install_upload_routes
from .web_jobs import JobConflict, JobNotFound, JobStore, LeaseLost
from .web_settings import ServerSettings, Sessions, verify_password
from .web_storage import DriveStorage, LocalStorage, StorageRouter
from .web_uploads import MAX_CHUNK_BYTES

LOG = logging.getLogger(__name__)
COOKIE = "linguoai_session"
ARTIFACT_NAMES = {
    "video": ".mp4",
    "original_srt": ".original.srt",
    "translated_srt": ".translated.srt",
}


class JobOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    target_language: str = Field(default="de", min_length=2, max_length=12)
    source_language: str | None = Field(default=None, max_length=12)
    asr_engine: str = "whisper"
    parakeet_profile: str = "multilingual-v3"
    translation_service: str = "google"
    tts_engine: str = "edge"
    tts_voice: str | None = Field(default=None, max_length=100)

    def checked(self) -> dict[str, Any]:
        data = self.model_dump()
        if data["target_language"] not in LANGUAGES:
            raise ValueError("Wähle eine unterstützte Zielsprache.")
        if data["source_language"] == "auto":
            data["source_language"] = None
        if data["source_language"] and data["source_language"] not in LANGUAGES:
            raise ValueError("Wähle eine unterstützte Quellsprache.")
        if self.asr_engine not in {"whisper", "parakeet"}:
            raise ValueError("Unbekannte Spracherkennung.")
        if self.parakeet_profile not in {"multilingual-v3", "german-primeline"}:
            raise ValueError("Unbekanntes Parakeet-Profil.")
        if self.asr_engine == "parakeet":
            AppConfig(parakeet_profile=self.parakeet_profile).validate_parakeet_language(
                data["source_language"]
            )
        if self.translation_service not in TRANSLATION_SERVICES or self.tts_engine not in {
            "edge",
            "gtts",
        }:
            raise ValueError("Diese Übersetzung oder Stimme ist im Webdienst nicht verfügbar.")
        if self.tts_voice and any(ord(c) < 32 for c in self.tts_voice):
            raise ValueError("Ungültige Stimme.")
        return data


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=512)


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: str = Field(min_length=1, max_length=100, pattern=r"^[\w.-]+$")
    capabilities: dict[str, Any] = Field(default_factory=dict)


class Lease(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: str = Field(min_length=1, max_length=100)
    lease_token: str = Field(min_length=16, max_length=200)


class Heartbeat(Lease):
    progress: float | None = Field(default=None, ge=0, le=100)
    stage: str | None = Field(default=None, max_length=100)


class Failure(Lease):
    error: str = Field(default="Verarbeitung fehlgeschlagen.", max_length=2000)
    failure_code: (
        Literal[
            "translation_unavailable",
            "voice_unavailable",
            "recognition_failed",
            "service_unavailable",
            "processing_failed",
        ]
        | None
    ) = None
    cancelled: bool = False


PUBLIC_FAILURE_MESSAGES = {
    "translation_unavailable": (
        "Der Übersetzungsdienst ist momentan nicht verfügbar oder hat sein Limit erreicht. "
        "Bitte später erneut versuchen oder einen anderen Übersetzungsdienst wählen."
    ),
    "voice_unavailable": (
        "Die Sprachausgabe ist momentan nicht verfügbar. "
        "Bitte später erneut versuchen oder eine andere Sprachausgabe wählen."
    ),
    "recognition_failed": (
        "Die Sprache konnte nicht erkannt werden. "
        "Bitte die Tonspur prüfen oder eine andere Spracherkennung wählen."
    ),
    "service_unavailable": (
        "Ein benötigter Dienst oder ein Modell ist momentan nicht verfügbar. "
        "Bitte später erneut versuchen."
    ),
    "processing_failed": "Verarbeitung fehlgeschlagen. Bitte Datei und Einstellungen prüfen.",
}


class BodyLimitMiddleware:
    """Begrenzt auch gestreamte multipart-Anfragen vor dem Speichern auf Platte."""

    def __init__(self, app: Any, settings: ServerSettings, sessions: Sessions) -> None:
        self.app, self.settings, self.sessions = app, settings, sessions

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = scope["path"]
        if "/api/worker/" in path and not hmac.compare_digest(
            request.headers.get("authorization", "").encode(),
            ("Bearer " + self.settings.worker_token).encode(),
        ):
            await JSONResponse({"detail": "Worker-Zugang verweigert."}, 401)(scope, receive, send)
            return
        # Authentifizierung vor dem Multipart-Parser verhindert anonyme Dateispools.
        if (
            (path.endswith("/api/jobs") and scope["method"] == "POST") or "/api/uploads" in path
        ) and not self.sessions.owner(request.cookies.get(COOKIE)):
            await JSONResponse({"detail": "Bitte zuerst anmelden."}, 401)(scope, receive, send)
            return
        if (
            "/api/" in path
            and "/api/worker/" not in path
            and scope["method"] in {"POST", "PUT", "DELETE"}
            and request.headers.get("X-LinguoAI-Request") != "1"
        ):
            await JSONResponse({"detail": "Ungültige Browseranfrage."}, 403)(scope, receive, send)
            return
        if (
            "/api/" in path
            and "/api/worker/" not in path
            and scope["method"] in {"POST", "PUT", "DELETE"}
        ):
            public = urlsplit(self.settings.public_url)
            allowed_origin = f"{public.scheme}://{public.netloc}"
            if request.headers.get("origin") and request.headers["origin"] != allowed_origin:
                await JSONResponse(
                    {"detail": "Die Anfrage stammt von einer anderen Website."}, 403
                )(scope, receive, send)
                return
        chunk = scope["method"] == "PUT" and "/uploads/" in path
        legacy_file = scope["method"] == "POST" and (
            path.endswith("/api/jobs") or "/artifacts/" in path
        )
        if chunk:
            limit = MAX_CHUNK_BYTES
        elif legacy_file:
            limit = (
                self.settings.max_result_bytes
                if "/artifacts/" in path
                else self.settings.max_upload_bytes
            ) + 65536
        else:
            limit = 65536
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > limit):
            await JSONResponse({"detail": "Die Datei oder Anfrage ist zu groß."}, 413)(
                scope, receive, send
            )
            return
        if legacy_file or chunk:
            reservation = int(length) if length and length.isdigit() else limit
            required = reservation * 3 + 512 * 1024**2
            roots = [self.settings.state_directory]
            if legacy_file:
                roots.append(Path(tempfile.gettempdir()))
            if any(shutil.disk_usage(root).free < required for root in roots):
                await JSONResponse(
                    {"detail": "Der Dateispeicher ist momentan voll. Bitte später versuchen."}, 507
                )(scope, receive, send)
                return
        consumed = 0

        async def limited_receive() -> dict:
            nonlocal consumed
            message = await receive()
            consumed += len(message.get("body", b""))
            if consumed > limit:
                raise HTTPException(413, "Die Datei oder Anfrage ist zu groß.")
            return message

        await self.app(scope, limited_receive, send)


def create_app(
    settings: ServerSettings | None = None, *, storage: Any = None, store: JobStore | None = None
) -> FastAPI:
    settings = (settings or ServerSettings.from_env()).validated()
    settings.state_directory.mkdir(parents=True, exist_ok=True)
    sessions = Sessions(settings)
    store = store or JobStore(
        settings.state_directory / "jobs.sqlite3",
        lease_seconds=settings.lease_seconds,
        retention_seconds=settings.retention_seconds,
        max_active_per_owner=settings.max_active_per_owner,
        max_active_total=settings.max_active_total,
    )
    storage = storage or (
        StorageRouter(
            LocalStorage(settings.state_directory / "files"),
            DriveStorage(
                settings.drive_folder_id,
                settings.drive_credentials_path,
                shared_drive=settings.drive_shared,
            ),
        )
        if settings.storage_backend == "drive"
        else LocalStorage(settings.state_directory / "files")
    )
    workers: dict[str, tuple[float, dict]] = {}
    workers_lock = threading.Lock()
    login_failures: dict[str, list[float]] = {}
    login_lock = threading.Lock()
    download_slots = threading.BoundedSemaphore(2)

    def cleanup() -> None:
        chunk_service.cleanup()
        for orphan in store.orphan_cleanup_records():
            try:
                storage.delete(orphan["storage_ref"])
                store.delete_orphan(orphan["storage_ref"], orphan["cleanup_token"])
            except Exception:
                LOG.warning("Nicht zugeordneter Upload wird später erneut aufgeräumt.")
        for row in store.expired_cleanup_records():
            references = [row.get("input_ref")]
            references += [a["storage_ref"] for a in row.get("artifacts", {}).values()]
            references += row.get("stale_refs", [])
            try:
                for ref in filter(None, references):
                    storage.delete(ref)
                store.expire(row["id"], row.get("cleanup_token"))
            except Exception:
                LOG.warning("Temporäre Dateien konnten noch nicht aufgeräumt werden: %s", row["id"])

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async def cleanup_loop():
            while True:
                try:
                    await run_in_threadpool(cleanup)
                except Exception:
                    LOG.warning("Aufräumen wird nach einem vorübergehenden Fehler erneut versucht.")
                await asyncio.sleep(60)

        async def recover_loop():
            while True:
                try:
                    await chunk_service.recover()
                except Exception:
                    LOG.warning(
                        "Upload-Abschluss wird nach einem vorübergehenden Fehler erneut versucht."
                    )
                await asyncio.sleep(2)

        task = asyncio.create_task(cleanup_loop())
        recovery = asyncio.create_task(recover_loop())
        try:
            yield
        finally:
            task.cancel()
            recovery.cancel()
            with suppress(asyncio.CancelledError):
                await task
            with suppress(asyncio.CancelledError):
                await recovery
            await chunk_service.stop()

    app = FastAPI(
        title="LinguoAI Web",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        root_path=settings.public_path,
    )
    app.state.store, app.state.storage, app.state.settings = store, storage, settings
    host = urlsplit(settings.public_url).hostname
    app.add_middleware(BodyLimitMiddleware, settings=settings, sessions=sessions)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=[host, "localhost", "127.0.0.1", "testserver"]
    )

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'"
        )
        if "/api/" in request.url.path:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(JobNotFound)
    async def not_found(_request, _exc):
        return JSONResponse({"detail": "Auftrag nicht gefunden."}, 404)

    @app.exception_handler(LeaseLost)
    async def stale_lease(_request, _exc):
        return JSONResponse({"detail": "Worker-Zuweisung ist abgelaufen."}, 409)

    @app.exception_handler(JobConflict)
    async def job_conflict(_request, exc):
        return JSONResponse({"detail": str(exc)}, 409)

    def owner(request: Request) -> str:
        value = sessions.owner(request.cookies.get(COOKIE))
        if not value:
            raise HTTPException(401, "Bitte zuerst anmelden.")
        return value

    def browser_change(request: Request) -> None:
        if request.headers.get("X-LinguoAI-Request") != "1":
            raise HTTPException(403, "Ungültige Browseranfrage.")
        origin = request.headers.get("origin")
        public = urlsplit(settings.public_url)
        allowed = f"{public.scheme}://{public.netloc}"
        if origin and origin != allowed:
            raise HTTPException(403, "Die Anfrage stammt von einer anderen Website.")

    def worker_auth(request: Request) -> None:
        actual = request.headers.get("authorization", "")
        if not hmac.compare_digest(actual.encode(), ("Bearer " + settings.worker_token).encode()):
            raise HTTPException(401, "Worker-Zugang verweigert.")

    def public_job(row: dict) -> dict:
        if row.get("expires_at") and row["expires_at"] <= time.time():
            row = {**row, "status": "expired"}
        result = {
            key: row.get(key)
            for key in (
                "id",
                "filename",
                "status",
                "progress",
                "stage",
                "error",
                "cancel_requested",
                "created_at",
                "updated_at",
                "finished_at",
                "expires_at",
                "options",
            )
        }
        result["available_at"] = row.get("finished_at") if row["status"] == "completed" else None
        result["artifacts"] = []
        if row["status"] == "completed":
            for name in row.get("artifacts", {}):
                result["artifacts"].append(
                    {
                        "name": name,
                        "label": {
                            "video": "Video",
                            "original_srt": "Original-Untertitel",
                            "translated_srt": "Übersetzte Untertitel",
                        }[name],
                        "download_url": (
                            f"{settings.public_path}/api/jobs/{row['id']}/artifacts/{name}"
                        ),
                    }
                )
        return result

    def register(worker_id: str, capabilities: dict | None = None) -> None:
        with workers_lock:
            old = workers.get(worker_id, (0, {}))[1]
            workers[worker_id] = (time.time(), capabilities or old)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.post("/api/auth/login", dependencies=[Depends(browser_change)])
    def login(data: Login, request: Request):
        address = request.client.host if request.client else "unknown"
        with login_lock:
            failures = [t for t in login_failures.get(address, []) if t > time.time() - 60]
            if len(failures) >= 8:
                raise HTTPException(429, "Bitte eine Minute warten und erneut anmelden.")
            login_failures[address] = failures
        encoded = settings.users.get(data.username)
        if not encoded or not verify_password(data.password, encoded):
            with login_lock:
                login_failures.setdefault(address, []).append(time.time())
            raise HTTPException(401, "Benutzername oder Passwort stimmt nicht.")
        response = JSONResponse({"authenticated": True, "username": data.username})
        response.set_cookie(
            COOKIE,
            sessions.issue(data.username),
            max_age=settings.session_seconds,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="strict",
            path=settings.public_path or "/",
        )
        return response

    @app.post("/api/auth/logout", dependencies=[Depends(browser_change)])
    def logout():
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(
            COOKIE,
            path=settings.public_path or "/",
            secure=settings.secure_cookies,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/api/capabilities")
    def capabilities(request: Request):
        authenticated = bool(sessions.owner(request.cookies.get(COOKIE)))
        with workers_lock:
            recent = [
                caps
                for stamp, caps in workers.values()
                if stamp > time.time() - settings.worker_online_seconds
            ]
        engines = [{"id": "whisper", "label": "Whisper · vielseitig"}]
        profiles = []
        for profile, label, codes in (
            ("multilingual-v3", "Parakeet v3 · 25 Sprachen", sorted(PARAKEET_V3_LANGUAGES)),
            ("german-primeline", "primeline · Deutsch", ["de"]),
        ):
            if any(
                "parakeet" in caps.get("asr_engines", [])
                and profile in caps.get("parakeet_profiles", [])
                for caps in recent
            ):
                profiles.append(
                    {"id": profile, "label": label, "languages": codes, "available": True}
                )
        if profiles:
            engines.insert(
                0,
                {"id": "parakeet", "label": "Parakeet · schnelle CPU-Option", "profiles": profiles},
            )
        translations = (
            sorted(
                {code for caps in recent for code in caps.get("translation_services", ["google"])}
            )
            if recent
            else ["google"]
        )
        voices = (
            sorted({code for caps in recent for code in caps.get("tts_engines", ["edge"])})
            if recent
            else ["edge"]
        )
        return {
            "authenticated": authenticated,
            "engines": engines,
            "default_engine": engines[0]["id"],
            "max_upload_bytes": min(settings.max_upload_bytes, 1024**3),
            "upload_chunk_bytes": MAX_CHUNK_BYTES,
            "upload_protocol": "chunks-v1",
            "storage": settings.storage_backend,
            "worker_online": bool(recent),
            "languages": LANGUAGES,
            "retention_hours": settings.retention_seconds / 3600,
            "translation_services": [
                {
                    "id": code,
                    "label": {
                        "google": "Google Translate · online",
                        "deepl": "DeepL · online",
                        "gemini": "Gemini · online",
                        "nllb": "NLLB · lokal",
                    }[code],
                }
                for code in translations
                if code in TRANSLATION_SERVICES
            ],
            "tts_engines": [
                {
                    "id": code,
                    "label": {"edge": "Microsoft Edge · online", "gtts": "Google TTS · online"}.get(
                        code, code
                    ),
                }
                for code in voices
                if code in {"edge", "gtts"}
            ],
        }

    @app.get("/api/jobs")
    def list_jobs(username: str = Depends(owner)):
        return {"jobs": [public_job(row) for row in store.list_jobs(username)]}

    def check_options(options):
        try:
            checked = (
                JobOptions.model_validate_json(options)
                if isinstance(options, str)
                else JobOptions.model_validate(options)
            ).checked()
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc
        with workers_lock:
            recent = [
                caps
                for stamp, caps in workers.values()
                if stamp > time.time() - settings.worker_online_seconds
            ]
        if recent and not any(
            checked["translation_service"] in caps.get("translation_services", ["google"])
            and checked["tts_engine"] in caps.get("tts_engines", ["edge"])
            and checked["asr_engine"] in caps.get("asr_engines", ["whisper"])
            and (
                checked["asr_engine"] != "parakeet"
                or checked["parakeet_profile"] in caps.get("parakeet_profiles", [])
            )
            for caps in recent
        ):
            raise HTTPException(422, "Kein verfügbarer Worker unterstützt diese Kombination.")
        return checked

    async def copy_upload(file: UploadFile, destination: Path, maximum: int) -> int:
        size = 0
        with destination.open("wb") as handle:
            while block := await file.read(1024 * 1024):
                size += len(block)
                if size > maximum:
                    raise HTTPException(413, "Die Datei überschreitet die Uploadgrenze.")
                await run_in_threadpool(handle.write, block)
        if size == 0:
            raise HTTPException(422, "Die Datei ist leer.")
        return size

    @app.post("/api/jobs", dependencies=[Depends(browser_change)], status_code=201)
    async def upload_job(
        file: Annotated[UploadFile, File()],
        options: Annotated[str, Form()],
        request: Request,
        username: str = Depends(owner),
    ):
        filename = (file.filename or "video.mp4").replace("\\", "/").rsplit("/", 1)[-1]
        if (
            not filename
            or len(filename) > 200
            or Path(filename).suffix.lower() not in VIDEO_EXTENSIONS
            or any(ord(c) < 32 for c in filename)
        ):
            raise HTTPException(422, "Bitte eine unterstützte Videodatei wählen.")
        checked = check_options(options)
        row = store.create(username, filename, checked)
        ref = None
        try:
            if (
                settings.storage_backend == "local"
                and shutil.disk_usage(settings.state_directory).free
                < settings.max_upload_bytes + 1024**3
            ):
                raise HTTPException(
                    507, "Der Dateispeicher ist momentan voll. Bitte später versuchen."
                )
            with tempfile.TemporaryDirectory(
                prefix="upload-", dir=settings.state_directory
            ) as directory:
                path = Path(directory) / ("source" + Path(filename).suffix.lower())
                size = await copy_upload(file, path, settings.max_upload_bytes)
                if await request.is_disconnected():
                    raise HTTPException(400, "Upload wurde unterbrochen.")
                ref = await run_in_threadpool(storage.upload, path, job_id=row["id"], name="input")
                store.register_orphan(ref)
                row = store.queue(row["id"], username, ref, size)
            return public_job(row)
        except BaseException:
            if ref:
                with suppress(Exception):
                    await run_in_threadpool(storage.delete, ref)
                    store.delete_orphan(ref)
            with suppress(Exception):
                store.fail_upload(row["id"], username, "Upload fehlgeschlagen oder unterbrochen.")
            raise
        finally:
            await file.close()

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, username: str = Depends(owner)):
        return public_job(store.get(job_id, username))

    @app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(browser_change)])
    def cancel_job(job_id: str, username: str = Depends(owner)):
        return public_job(store.cancel(job_id, username))

    async def stored_file(ref: str, filename: str, size_bytes: int):
        if isinstance(storage, LocalStorage) or (
            isinstance(storage, StorageRouter) and storage.is_local_ref(ref)
        ):
            try:
                return FileResponse(
                    storage.file_path(ref), filename=filename, media_type="application/octet-stream"
                )
            except FileNotFoundError as exc:
                raise HTTPException(404, "Die Datei ist nicht mehr verfügbar.") from exc
        if not await run_in_threadpool(download_slots.acquire, True, 30):
            raise HTTPException(429, "Alle Downloads sind momentan belegt. Bitte erneut versuchen.")
        temporary = tempfile.TemporaryDirectory(prefix="download-", dir=settings.state_directory)
        path = Path(temporary.name) / "result"

        def release():
            temporary.cleanup()
            download_slots.release()

        try:
            if shutil.disk_usage(settings.state_directory).free < size_bytes + 512 * 1024**2:
                raise HTTPException(507, "Temporärer Downloadspeicher ist belegt.")
            await run_in_threadpool(storage.download, ref, path)
            return FileResponse(
                path,
                filename=filename,
                media_type="application/octet-stream",
                background=BackgroundTask(release),
            )
        except Exception as exc:
            release()
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(
                503, "Datei konnte nicht geladen werden. Bitte erneut versuchen."
            ) from exc

    @app.get("/api/jobs/{job_id}/artifacts/{name}")
    async def download_result(job_id: str, name: str, username: str = Depends(owner)):
        row = store.get(job_id, username)
        if row.get("expires_at") and row["expires_at"] <= time.time():
            raise HTTPException(410, "Die Aufbewahrungsfrist ist abgelaufen.")
        if row["status"] != "completed" or name not in row.get("artifacts", {}):
            raise HTTPException(404, "Ergebnis noch nicht verfügbar.")
        stem = Path(row["filename"]).stem[:150]
        artifact = row["artifacts"][name]
        return await stored_file(
            artifact["storage_ref"], stem + ARTIFACT_NAMES[name], artifact["size_bytes"]
        )

    @app.post("/api/worker/claim", dependencies=[Depends(worker_auth)])
    def claim(data: Claim):
        register(data.worker_id, data.capabilities)
        row = store.claim(data.worker_id, data.capabilities)
        if row:
            # Alte Teil-Ergebnisse werden nie als Download angeboten.
            for ref in row.get("stale_refs", []):
                with suppress(Exception):
                    storage.delete(ref)
        return {"job": row}

    @app.post("/api/worker/jobs/{job_id}/heartbeat", dependencies=[Depends(worker_auth)])
    def heartbeat(job_id: str, data: Heartbeat):
        register(data.worker_id)
        return store.heartbeat(
            job_id, data.worker_id, data.lease_token, progress=data.progress, stage=data.stage
        )

    @app.get("/api/worker/jobs/{job_id}/input", dependencies=[Depends(worker_auth)])
    async def worker_input(
        job_id: str, x_worker_id: Annotated[str, Header()], x_job_lease: Annotated[str, Header()]
    ):
        row = store.verify_lease(job_id, x_worker_id, x_job_lease)
        return await stored_file(row["input_ref"], row["filename"], row["size_bytes"])

    @app.post("/api/worker/jobs/{job_id}/artifacts/{name}", dependencies=[Depends(worker_auth)])
    async def artifact_upload(
        job_id: str,
        name: str,
        file: Annotated[UploadFile, File()],
        worker_id: Annotated[str, Form()],
        lease_token: Annotated[str, Form()],
    ):
        if name not in ARTIFACT_NAMES:
            raise HTTPException(422, "Unbekanntes Ergebnis.")
        store.verify_lease(job_id, worker_id, lease_token)
        ref = None
        try:
            with tempfile.TemporaryDirectory(
                prefix="artifact-", dir=settings.state_directory
            ) as directory:
                path = Path(directory) / (name + ARTIFACT_NAMES[name])
                size = await copy_upload(file, path, settings.max_result_bytes)
                ref = await run_in_threadpool(storage.upload, path, job_id=job_id, name=name)
                store.register_orphan(ref)
                row = store.add_artifact(job_id, worker_id, lease_token, name, ref, size)
                return {"name": name, "size_bytes": size, "status": row["status"]}
        except BaseException:
            if ref:
                with suppress(Exception):
                    await run_in_threadpool(storage.delete, ref)
                    store.delete_orphan(ref)
            raise
        finally:
            await file.close()

    @app.post("/api/worker/jobs/{job_id}/complete", dependencies=[Depends(worker_auth)])
    def complete(job_id: str, data: Lease):
        return public_job(store.complete(job_id, data.worker_id, data.lease_token))

    @app.post("/api/worker/jobs/{job_id}/fail", dependencies=[Depends(worker_auth)])
    def fail(job_id: str, data: Failure):
        # Öffentliche Fehler enthalten weder lokale Pfade noch Anbieter-/Zugangsschlüssel.
        error = (
            "Verarbeitung abgebrochen."
            if data.cancelled
            else PUBLIC_FAILURE_MESSAGES.get(
                data.failure_code, PUBLIC_FAILURE_MESSAGES["processing_failed"]
            )
        )
        return public_job(
            store.fail(job_id, data.worker_id, data.lease_token, error, cancelled=data.cancelled)
        )

    chunk_service = install_upload_routes(
        app, settings, store, storage, owner, browser_change, worker_auth, public_job, check_options
    )

    website = settings.website_directory or Path(__file__).resolve().parents[1] / "website"
    if settings.website_directory is None and not (website / "web.html").is_file():
        website = Path(sys.prefix) / "share" / "linguoai-web"
    if (website / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=website / "assets"), name="assets")

    @app.get("/web.html", include_in_schema=False)
    def web_page():
        if not (website / "web.html").is_file():
            raise HTTPException(503, "Die Weboberfläche ist noch nicht installiert.")
        return FileResponse(website / "web.html", media_type="text/html")

    @app.get("/", include_in_schema=False)
    def home():
        return RedirectResponse(settings.public_path + "/web.html")

    return app


def entrypoint() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="LinguoAI Upload- und Download-Dienst")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    settings = ServerSettings.from_env()
    uvicorn.run(
        "linguoai.web_api:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        root_path=settings.public_path,
    )


if __name__ == "__main__":
    entrypoint()
