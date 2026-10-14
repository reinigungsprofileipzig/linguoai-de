"""Bounded resumable browser and worker transfers, including durable finalization."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import threading
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .config import VIDEO_EXTENSIONS
from .web_jobs import JobConflict, JobNotFound, LeaseLost
from .web_uploads import (
    MAX_FILE_BYTES,
    ChunkUploadStore,
    UploadConflict,
    UploadLimitReached,
    UploadNotFound,
)

LOG = logging.getLogger(__name__)
RESULT_NAMES = {"video", "original_srt", "translated_srt"}
PUBLIC_UPLOAD_FIELDS = (
    "id",
    "job_id",
    "kind",
    "filename",
    "artifact_name",
    "size_bytes",
    "offset",
    "chunk_bytes",
    "status",
    "error",
    "created_at",
    "updated_at",
    "expires_at",
)


class BrowserUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=200)
    size_bytes: int = Field(strict=True, ge=1, le=10 * 1024**3)
    options: dict[str, Any] = Field(default_factory=dict)


class WorkerOwnership(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: str = Field(min_length=1, max_length=100, pattern=r"^[\w.-]+$")
    lease_token: str = Field(min_length=16, max_length=200)


class WorkerUpload(WorkerOwnership):
    filename: str = Field(min_length=1, max_length=200)
    size_bytes: int = Field(strict=True, ge=1, le=10 * 1024**3)


def checked_filename(filename: str, *, video: bool = True) -> str:
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if (
        not filename
        or len(filename) > 200
        or filename in {".", ".."}
        or any(ord(character) < 32 for character in filename)
        or (video and Path(filename).suffix.lower() not in VIDEO_EXTENSIONS)
    ):
        raise HTTPException(422, "Bitte eine unterstützte Videodatei wählen.")
    return filename


def lease_fence(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ChunkUploadService:
    def __init__(self, settings, jobs, storage, public_job):
        self.settings, self.jobs, self.storage = settings, jobs, storage
        self.public_job = public_job
        self.uploads = ChunkUploadStore(
            settings.state_directory,
            max_pending_per_principal=max(3, settings.max_active_per_owner),
            max_pending_total=max(3, settings.max_active_total * 3),
            max_size_bytes=min(
                MAX_FILE_BYTES, max(settings.max_upload_bytes, settings.max_result_bytes)
            ),
        )
        self.creation_lock = threading.Lock()
        self.tasks: dict[str, asyncio.Task] = {}
        self.finalize_slots = asyncio.Semaphore(2)

    def public(self, row, principal=None):
        result = {key: row.get(key) for key in PUBLIC_UPLOAD_FIELDS}
        if result["status"] == "ready":
            result["status"] = "finalizing"
        if result["status"] == "aborted" and result["error"] in {"", "Upload aborted"}:
            result["error"] = "Upload abgebrochen."
        actor = principal or row.get("principal")
        if actor and row["kind"] == "input" and row["status"] in {"completed", "aborted"}:
            with suppress(JobNotFound):
                result["job"] = self.public_job(self.jobs.get(row["job_id"], self.username(actor)))
        return result

    @staticmethod
    def username(principal):
        if not principal.startswith("user:"):
            raise UploadConflict("Invalid input principal")
        return principal[5:]

    def capacity(self, size: int, *, reserve=True):
        required = 512 * 1024**2 + size
        if reserve:
            required += self.uploads.reserved_remaining_bytes()
        if shutil.disk_usage(self.settings.state_directory).free < required:
            raise HTTPException(507, "Der Dateispeicher ist momentan voll. Bitte später versuchen.")

    def _job(self, row):
        if row["kind"] == "input":
            return self.jobs.get(row["job_id"], self.username(row["principal"]))
        job = self.jobs.get(row["job_id"])
        worker = row["principal"].removeprefix("worker:")
        if (
            job.get("worker_id") != worker
            or not job.get("lease_token")
            or lease_fence(job["lease_token"]) != row["fence_tag"]
        ):
            raise LeaseLost("The worker lease changed")
        return self.jobs.verify_lease(row["job_id"], worker, job["lease_token"])

    def attached(self, row, ref):
        if not ref:
            return False
        try:
            job = self.jobs.get(row["job_id"])
        except JobNotFound:
            return False
        if row["kind"] == "input":
            return (
                job.get("owner") == self.username(row["principal"]) and job.get("input_ref") == ref
            )
        return job.get("artifacts", {}).get(row.get("artifact_name"), {}).get("storage_ref") == ref

    def discard_ref(self, row, ref):
        if ref and not self.attached(row, ref):
            self.storage.delete(ref)
            self.jobs.delete_orphan(ref)

    def cleanup(self):
        for row in self.uploads.pending_records():
            try:
                job = self._job(row)
                if job["status"] in {"cancelled", "failed", "expired"} or job["cancel_requested"]:
                    self.uploads.abort(row["id"], row["principal"], row["fence_tag"])
            except (JobNotFound, JobConflict, LeaseLost):
                self.uploads.abort(row["id"], row["principal"], row["fence_tag"])
        for row in self.uploads.expired_cleanup_records():
            try:
                self.discard_ref(row, row.get("storage_ref"))
                if row["kind"] == "input":
                    with suppress(JobNotFound, JobConflict):
                        self.jobs.fail_upload(
                            row["job_id"],
                            self.username(row["principal"]),
                            "Upload wurde abgebrochen oder ist abgelaufen.",
                        )
                self.uploads.cleanup_ack(row["id"], row["cleanup_token"])
            except Exception:
                LOG.warning("Ein Teilupload wird später erneut aufgeräumt: %s", row["id"])

    async def renew(self, row):
        while True:
            await asyncio.sleep(30)
            await run_in_threadpool(
                self.uploads.renew_completion,
                row["id"],
                row["principal"],
                row["finalization_token"],
                row["fence_tag"],
            )

    def _store_and_attach(self, row):
        ref = row.get("storage_ref")
        if self.attached(row, ref):
            return self.uploads.finish(
                row["id"], row["principal"], row["finalization_token"], row["fence_tag"]
            )
        job = self._job(row)
        if job["cancel_requested"] or (row["kind"] == "input" and job["status"] != "uploading"):
            raise JobConflict("The job no longer accepts this upload")
        if not ref:
            self.capacity(row["size_bytes"])
            path = self.uploads.private_file_path(row["id"], row["principal"], row["fence_tag"])
            ref = self.storage.upload(
                path,
                job_id=row["job_id"],
                name="input" if row["kind"] == "input" else row["artifact_name"],
            )
            try:
                self.uploads.set_storage_ref(
                    row["id"], row["principal"], row["finalization_token"], ref, row["fence_tag"]
                )
                row["storage_ref"] = ref
                self.jobs.register_orphan(ref, delay_seconds=3600)
            except BaseException:
                with suppress(Exception):
                    self.discard_ref(row, ref)
                raise
        job = self._job(row)
        if row["kind"] == "input":
            if job.get("input_ref") != ref:
                self.jobs.queue(
                    row["job_id"], self.username(row["principal"]), ref, row["size_bytes"]
                )
        else:
            self.jobs.add_artifact(
                row["job_id"],
                row["principal"].removeprefix("worker:"),
                job["lease_token"],
                row["artifact_name"],
                ref,
                row["size_bytes"],
            )
        return self.uploads.finish(
            row["id"], row["principal"], row["finalization_token"], row["fence_tag"]
        )

    async def finalize(self, row):
        renewal = asyncio.create_task(self.renew(row))
        try:
            async with self.finalize_slots:
                if row.get("finalize_attempts", 0) > 3:
                    raise UploadConflict("Too many storage attempts")
                await run_in_threadpool(self._store_and_attach, row)
        except asyncio.CancelledError:
            with suppress(Exception):
                await run_in_threadpool(
                    self.uploads.release_completion,
                    row["id"],
                    row["principal"],
                    row["finalization_token"],
                    "Die Speicherung wird fortgesetzt.",
                    row["fence_tag"],
                )
            raise
        except Exception as error:
            permanent = isinstance(
                error, (JobConflict, JobNotFound, LeaseLost, UploadConflict, UploadNotFound)
            )
            with suppress(Exception):
                await run_in_threadpool(
                    self.uploads.release_completion,
                    row["id"],
                    row["principal"],
                    row["finalization_token"],
                    "Die Datei konnte momentan nicht gespeichert werden. Bitte erneut versuchen.",
                    row["fence_tag"],
                    not permanent,
                )
            if permanent:
                with suppress(Exception):
                    await run_in_threadpool(self.discard_ref, row, row.get("storage_ref"))
                if row["kind"] == "input":
                    with suppress(JobConflict, JobNotFound):
                        await run_in_threadpool(
                            self.jobs.fail_upload,
                            row["job_id"],
                            self.username(row["principal"]),
                            "Upload konnte nicht abgeschlossen werden. Bitte erneut versuchen.",
                        )
            LOG.warning(
                "Speicherung eines Teiluploads fehlgeschlagen (%s): %s",
                type(error).__name__,
                row["id"],
            )
        finally:
            if renewal:
                renewal.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await renewal

    def schedule(self, row):
        if row["id"] in self.tasks:
            return
        task = asyncio.create_task(self.finalize(row))
        self.tasks[row["id"]] = task
        task.add_done_callback(lambda finished: self.tasks.pop(row["id"], None))

    async def request_completion(self, upload_id, principal, fence_tag=""):
        row = await run_in_threadpool(self.uploads.get, upload_id, principal, fence_tag)
        if row["status"] == "completed":
            return JSONResponse(self.public(row, principal), 200)
        if upload_id in self.tasks:
            return JSONResponse(self.public(row, principal), 202)
        try:
            claimed = await run_in_threadpool(
                self.uploads.claim_completion, upload_id, principal, fence_tag
            )
        except UploadConflict:
            row = await run_in_threadpool(self.uploads.get, upload_id, principal, fence_tag)
            if row["status"] == "finalizing":
                return JSONResponse(self.public(row, principal), 202)
            raise
        if claimed["status"] == "completed":
            return JSONResponse(self.public(claimed, principal), 200)
        self.schedule(claimed)
        return JSONResponse(self.public(claimed, principal), 202)

    async def recover(self):
        for row in await run_in_threadpool(self.uploads.recoverable_completions):
            if row["id"] in self.tasks:
                continue
            with suppress(UploadNotFound, UploadConflict):
                claimed = await run_in_threadpool(
                    self.uploads.claim_completion, row["id"], row["principal"], row["fence_tag"]
                )
                if claimed["status"] != "completed":
                    self.schedule(claimed)

    async def stop(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def install_upload_routes(
    app, settings, jobs, storage, owner, browser_change, worker_auth, public_job, check_options
):
    service = ChunkUploadService(settings, jobs, storage, public_job)
    uploads = service.uploads
    app.state.chunk_uploads = service

    @app.exception_handler(UploadNotFound)
    async def not_found(_request, _error):
        return JSONResponse({"detail": "Upload nicht gefunden."}, 404)

    @app.exception_handler(UploadConflict)
    async def conflict(_request, _error):
        return JSONResponse(
            {"detail": "Uploadzustand hat sich geändert. Bitte den Status erneut laden."}, 409
        )

    @app.exception_handler(UploadLimitReached)
    async def capacity(_request, _error):
        return JSONResponse(
            {"detail": "Zu viele laufende Uploads. Bitte einen Auftrag abschließen."}, 409
        )

    def browser_session(upload_id, username):
        principal = "user:" + username
        row = uploads.get(upload_id, principal)
        jobs.get(row["job_id"], username)
        return row, principal

    def worker_session(job_id, upload_id, worker_id, lease_token):
        row = jobs.verify_lease(job_id, worker_id, lease_token)
        if row["cancel_requested"]:
            raise JobConflict("Job cancellation requested")
        principal, fence = "worker:" + worker_id, lease_fence(lease_token)
        session = uploads.get(upload_id, principal, fence)
        if session["job_id"] != job_id or session["kind"] != "artifact":
            raise UploadNotFound("Upload not found")
        return session, principal, fence

    async def append(row, principal, request, offset, sha256, fence=""):
        service.capacity(min(row["chunk_bytes"], row["size_bytes"] - row["offset"]), reserve=False)
        data = await request.body()
        try:
            result = await run_in_threadpool(
                uploads.append, row["id"], principal, offset, data, sha256, fence
            )
        except ValueError as error:
            if isinstance(error, UploadConflict):
                raise
            raise HTTPException(422, "Prüfsumme oder Dateiblock ist ungültig.") from error
        except OSError as error:
            raise HTTPException(507, "Temporärer Uploadspeicher ist belegt.") from error
        return service.public(result, principal)

    @app.post("/api/uploads", dependencies=[Depends(browser_change)], status_code=201)
    def browser_create(
        data: BrowserUpload,
        x_upload_request_id: Annotated[str, Header(min_length=1, max_length=80)],
        username: str = Depends(owner),
    ):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", x_upload_request_id):
            raise HTTPException(422, "Ungültige Upload-Anfrage.")
        if data.size_bytes > min(settings.max_upload_bytes, MAX_FILE_BYTES):
            raise HTTPException(413, "Die Datei überschreitet die Uploadgrenze.")
        filename, options = checked_filename(data.filename), check_options(data.options)
        principal = "user:" + username
        with service.creation_lock:
            previous = uploads.find_by_request_id(principal, x_upload_request_id)
            if previous:
                previous = uploads.get(previous["id"], principal)
                previous_job = jobs.get(previous["job_id"], username)
                if (
                    previous["size_bytes"] != data.size_bytes
                    or previous["filename"] != filename
                    or previous_job["options"] != options
                ):
                    raise UploadConflict("Upload request changed")
                return service.public(previous, principal)
            service.capacity(data.size_bytes * 2)
            job = jobs.create(username, filename, options)
            try:
                row = uploads.create(
                    principal,
                    job["id"],
                    "input",
                    filename,
                    data.size_bytes,
                    request_id=x_upload_request_id,
                )
            except BaseException:
                with suppress(JobConflict, JobNotFound):
                    jobs.fail_upload(job["id"], username, "Upload konnte nicht gestartet werden.")
                raise
        return service.public(row, principal)

    @app.get("/api/uploads/{upload_id}")
    def browser_get(upload_id: str, username: str = Depends(owner)):
        row, principal = browser_session(upload_id, username)
        return service.public(row, principal)

    @app.put("/api/uploads/{upload_id}", dependencies=[Depends(browser_change)])
    async def browser_append(
        upload_id: str,
        request: Request,
        offset: Annotated[int, Query(ge=0)],
        x_chunk_sha256: Annotated[str, Header(min_length=64, max_length=64)],
        username: str = Depends(owner),
    ):
        row, principal = browser_session(upload_id, username)
        job = jobs.get(row["job_id"], username)
        if job["status"] != "uploading" or job["cancel_requested"]:
            raise JobConflict("The job no longer accepts an upload")
        return await append(row, principal, request, offset, x_chunk_sha256)

    @app.post("/api/uploads/{upload_id}/complete", dependencies=[Depends(browser_change)])
    async def browser_complete(upload_id: str, username: str = Depends(owner)):
        row, principal = browser_session(upload_id, username)
        job = jobs.get(row["job_id"], username)
        if job["status"] == "cancelled" or job["cancel_requested"]:
            raise JobConflict("Job cancellation requested")
        return await service.request_completion(upload_id, principal)

    @app.delete("/api/uploads/{upload_id}", dependencies=[Depends(browser_change)])
    def browser_abort(upload_id: str, username: str = Depends(owner)):
        principal = "user:" + username
        # Abort can also reclaim an expired session; get() deliberately rejects expiry.
        row = uploads.abort(upload_id, principal)
        jobs.cancel(row["job_id"], username)
        result = service.public(row, principal)
        result["job"] = public_job(jobs.get(row["job_id"], username))
        return result

    @app.post(
        "/api/worker/jobs/{job_id}/uploads/{name}",
        dependencies=[Depends(worker_auth)],
        status_code=201,
    )
    def worker_create(job_id: str, name: str, data: WorkerUpload):
        if name not in RESULT_NAMES:
            raise HTTPException(422, "Unbekanntes Ergebnis.")
        if data.size_bytes > min(settings.max_result_bytes, MAX_FILE_BYTES):
            raise HTTPException(413, "Die Ergebnisdatei überschreitet die Uploadgrenze.")
        job = jobs.verify_lease(job_id, data.worker_id, data.lease_token)
        if job["cancel_requested"]:
            raise JobConflict("Job cancellation requested")
        filename = checked_filename(data.filename, video=False)
        principal, fence = "worker:" + data.worker_id, lease_fence(data.lease_token)
        key = hashlib.sha256(
            f"{job_id}:{name}:{fence}:{filename}:{data.size_bytes}".encode()
        ).hexdigest()
        with service.creation_lock:
            previous = uploads.find_by_request_id(principal, key, fence)
            if previous:
                return service.public(uploads.get(previous["id"], principal, fence))
            service.capacity(data.size_bytes * 2)
            row = uploads.create(
                principal,
                job_id,
                "artifact",
                filename,
                data.size_bytes,
                artifact_name=name,
                fence_tag=fence,
                request_id=key,
            )
        return service.public(row, principal)

    @app.get("/api/worker/jobs/{job_id}/uploads/{upload_id}", dependencies=[Depends(worker_auth)])
    def worker_get(
        job_id: str,
        upload_id: str,
        x_worker_id: Annotated[str, Header()],
        x_job_lease: Annotated[str, Header()],
    ):
        row, principal, _fence = worker_session(job_id, upload_id, x_worker_id, x_job_lease)
        return service.public(row, principal)

    @app.put("/api/worker/jobs/{job_id}/uploads/{upload_id}", dependencies=[Depends(worker_auth)])
    async def worker_append(
        job_id: str,
        upload_id: str,
        request: Request,
        offset: Annotated[int, Query(ge=0)],
        x_chunk_sha256: Annotated[str, Header(min_length=64, max_length=64)],
        x_worker_id: Annotated[str, Header()],
        x_job_lease: Annotated[str, Header()],
    ):
        row, principal, fence = worker_session(job_id, upload_id, x_worker_id, x_job_lease)
        return await append(row, principal, request, offset, x_chunk_sha256, fence)

    @app.post(
        "/api/worker/jobs/{job_id}/uploads/{upload_id}/complete",
        dependencies=[Depends(worker_auth)],
    )
    async def worker_complete(job_id: str, upload_id: str, data: WorkerOwnership):
        _row, principal, fence = worker_session(job_id, upload_id, data.worker_id, data.lease_token)
        return await service.request_completion(upload_id, principal, fence)

    @app.delete(
        "/api/worker/jobs/{job_id}/uploads/{upload_id}", dependencies=[Depends(worker_auth)]
    )
    def worker_abort(
        job_id: str,
        upload_id: str,
        x_worker_id: Annotated[str, Header()],
        x_job_lease: Annotated[str, Header()],
    ):
        jobs.verify_lease(job_id, x_worker_id, x_job_lease)
        principal, fence = "worker:" + x_worker_id, lease_fence(x_job_lease)
        row = uploads.get(upload_id, principal, fence)
        if row["job_id"] != job_id or row["kind"] != "artifact":
            raise UploadNotFound("Upload not found")
        row = uploads.abort(upload_id, principal, fence)
        return service.public(row, principal)

    return service
