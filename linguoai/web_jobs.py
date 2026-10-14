"""Transactional persistent jobs with secret, expiring worker leases."""

from __future__ import annotations

import json
import math
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

TERMINAL_STATES = ("completed", "failed", "cancelled")
ARTIFACT_NAMES = frozenset({"video", "original_srt", "translated_srt"})


class JobNotFound(LookupError):
    """A job does not exist or is not owned by the caller."""


class JobConflict(ValueError):
    """The requested operation conflicts with the current job state."""


class LeaseLost(JobConflict):
    """The worker lease expired or was replaced."""


class JobLimitReached(JobConflict):
    """An active job limit was reached."""


def _string(value, name, limit=512, *, allow_empty=False):
    if not isinstance(value, str) or len(value) > limit or (not value and not allow_empty):
        raise ValueError(f"Invalid {name}")
    return value


def _size(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
        raise ValueError("size_bytes must fit a nonnegative 64-bit integer")
    return value


class JobStore:
    """SQLite queue shared by API processes, with atomic claims and cleanup."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        lease_seconds=60,
        max_attempts=3,
        retention_seconds=86400,
        max_active_per_owner=3,
        max_active_total=100,
    ):
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if not math.isfinite(retention_seconds) or retention_seconds < 0:
            raise ValueError("retention_seconds must be nonnegative")
        for limit in (max_active_per_owner, max_active_total):
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise ValueError("Active job limits must be positive integers")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.retention_seconds = retention_seconds
        self.max_active_per_owner = max_active_per_owner
        self.max_active_total = max_active_total
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS web_jobs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, filename TEXT NOT NULL,
                    options TEXT NOT NULL, status TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    queued_at REAL, started_at REAL, finished_at REAL, expires_at REAL,
                    input_ref TEXT, size_bytes INTEGER NOT NULL DEFAULT 0,
                    progress REAL NOT NULL DEFAULT 0, stage TEXT NOT NULL DEFAULT '',
                    worker_id TEXT, lease_token TEXT, lease_until REAL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0, error TEXT,
                    artifacts TEXT NOT NULL DEFAULT '{}', stale_refs TEXT NOT NULL DEFAULT '[]',
                    cleanup_token TEXT, cleanup_until REAL
                );
                CREATE INDEX IF NOT EXISTS web_jobs_queue ON web_jobs(status, queued_at);
                CREATE INDEX IF NOT EXISTS web_jobs_expiry ON web_jobs(status, expires_at);
                CREATE INDEX IF NOT EXISTS web_jobs_owner ON web_jobs(owner, created_at);
                CREATE TABLE IF NOT EXISTS web_orphans (
                    storage_ref TEXT PRIMARY KEY,
                    created_at REAL NOT NULL, cleanup_after REAL NOT NULL,
                    cleanup_token TEXT, cleanup_until REAL
                );
                CREATE INDEX IF NOT EXISTS web_orphans_cleanup ON web_orphans(cleanup_after);
            """)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _dict(row) -> dict[str, Any]:
        result = dict(row)
        for field in ("options", "artifacts", "stale_refs"):
            result[field] = json.loads(result[field])
        result["cancel_requested"] = bool(result["cancel_requested"])
        return result

    @staticmethod
    def _row(connection, job_id, owner=None):
        row = connection.execute("SELECT * FROM web_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or (owner is not None and row["owner"] != owner):
            raise JobNotFound("Job not found")
        return row

    def _leased(self, connection, job_id, worker_id, lease_token, now):
        row = self._row(connection, job_id)
        if (
            row["status"] != "running"
            or row["worker_id"] != worker_id
            or not isinstance(lease_token, str)
            or not row["lease_token"]
            or not secrets.compare_digest(row["lease_token"].encode(), lease_token.encode())
            or row["lease_until"] <= now
        ):
            raise LeaseLost("Worker lease expired or was replaced")
        return row

    def _finish(self, connection, job_id, status, now, error=None):
        connection.execute(
            """UPDATE web_jobs SET status=?, updated_at=?, finished_at=?,
            expires_at=?, lease_until=NULL, lease_token=NULL, error=? WHERE id=?""",
            (status, now, now, now + self.retention_seconds, error, job_id),
        )

    @staticmethod
    def _count(connection, owner=None):
        query = "SELECT COUNT(*) FROM web_jobs WHERE status IN ('uploading','queued','running')"
        values = ()
        if owner is not None:
            query += " AND owner=?"
            values = (owner,)
        return connection.execute(query, values).fetchone()[0]

    def active_count(self, owner=None):
        with self._connection() as connection:
            return self._count(connection, owner)

    def list_jobs(self, owner, *, limit=20):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM web_jobs WHERE owner=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (owner, limit),
            ).fetchall()
            return [self._dict(row) for row in rows]

    def create(self, owner, filename, options):
        _string(owner, "owner", 256)
        _string(filename, "filename", 512)
        if not isinstance(options, dict):
            raise ValueError("options must be a dictionary")
        encoded = json.dumps(options, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 16384:
            raise ValueError("options are too large")
        job_id = secrets.token_urlsafe(24)
        with self._transaction() as connection:
            now = time.time()
            if (
                self._count(connection) >= self.max_active_total
                or self._count(connection, owner) >= self.max_active_per_owner
            ):
                raise JobLimitReached("Active job limit reached")
            connection.execute(
                """INSERT INTO web_jobs(id,owner,filename,options,status,
                created_at,updated_at,expires_at) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    owner,
                    filename,
                    encoded,
                    "uploading",
                    now,
                    now,
                    now + self.retention_seconds,
                ),
            )
            return self._dict(self._row(connection, job_id))

    def queue(self, job_id, owner, input_ref, size_bytes):
        _string(input_ref, "input_ref", 2048)
        _size(size_bytes)
        with self._transaction() as connection:
            now = time.time()
            row = self._row(connection, job_id, owner)
            if row["status"] != "uploading" or row["cleanup_token"]:
                raise JobConflict("Only an uploading job can be queued")
            self._attach_ref(connection, input_ref)
            connection.execute(
                """UPDATE web_jobs SET status='queued', input_ref=?, size_bytes=?,
                queued_at=?, updated_at=?, expires_at=NULL, stage='queued' WHERE id=?""",
                (input_ref, size_bytes, now, now, job_id),
            )
            return self._dict(self._row(connection, job_id))

    def get(self, job_id, owner=None):
        with self._connection() as connection:
            return self._dict(self._row(connection, job_id, owner))

    def fail_upload(self, job_id, owner, error, cancelled=False):
        _string(error, "error", 2048, allow_empty=True)
        with self._transaction() as connection:
            now = time.time()
            row = self._row(connection, job_id, owner)
            if row["status"] == "cancelled":
                return self._dict(row)
            if row["status"] != "uploading":
                raise JobConflict("Only an uploading job can fail upload")
            self._finish(connection, job_id, "cancelled" if cancelled else "failed", now, error)
            return self._dict(self._row(connection, job_id))

    def cancel(self, job_id, owner):
        with self._transaction() as connection:
            now = time.time()
            row = self._row(connection, job_id, owner)
            if row["status"] in ("queued", "uploading"):
                connection.execute("UPDATE web_jobs SET cancel_requested=1 WHERE id=?", (job_id,))
                self._finish(connection, job_id, "cancelled", now)
            elif row["status"] == "running":
                connection.execute(
                    "UPDATE web_jobs SET cancel_requested=1,updated_at=? WHERE id=?", (now, job_id)
                )
            return self._dict(self._row(connection, job_id))

    @staticmethod
    def _capable(options, capabilities):
        capabilities = capabilities or {}
        for option, capability, default in (
            ("translation_service", "translation_services", "google"),
            ("tts_engine", "tts_engines", "edge"),
        ):
            supported = capabilities.get(capability, [default])
            if not isinstance(supported, list) or options.get(option, default) not in supported:
                return False
        engine = options.get("asr_engine", "whisper")
        engines = capabilities.get("asr_engines", ["whisper"])
        if not isinstance(engines, list) or engine not in engines:
            return False
        if engine == "parakeet":
            profiles = capabilities.get("parakeet_profiles", [])
            return (
                isinstance(profiles, list)
                and options.get("parakeet_profile", "multilingual-v3") in profiles
            )
        return engine == "whisper"

    def claim(self, worker_id, capabilities=None):
        _string(worker_id, "worker_id", 256)
        if capabilities is not None and not isinstance(capabilities, dict):
            raise ValueError("capabilities must be a dictionary")
        with self._transaction() as connection:
            now = time.time()
            rows = connection.execute(
                """SELECT * FROM web_jobs WHERE status='queued'
                OR (status='running' AND lease_until<=?) ORDER BY queued_at,created_at,id""",
                (now,),
            ).fetchall()
            for row in rows:
                if row["cancel_requested"]:
                    self._finish(connection, row["id"], "cancelled", now)
                    continue
                if row["attempts"] >= self.max_attempts:
                    self._finish(
                        connection,
                        row["id"],
                        "failed",
                        now,
                        "Worker lease expired; retry limit reached",
                    )
                    continue
                if not self._capable(json.loads(row["options"]), capabilities):
                    continue
                stale = json.loads(row["stale_refs"])
                stale.extend(a["storage_ref"] for a in json.loads(row["artifacts"]).values())
                connection.execute(
                    """UPDATE web_jobs SET status='running',worker_id=?,
                    lease_token=?,lease_until=?,started_at=?,updated_at=?,attempts=attempts+1,
                    progress=0,stage='starting',error=NULL,artifacts='{}',stale_refs=?
                    WHERE id=?""",
                    (
                        worker_id,
                        secrets.token_urlsafe(32),
                        now + self.lease_seconds,
                        now,
                        now,
                        json.dumps(list(dict.fromkeys(stale))),
                        row["id"],
                    ),
                )
                return self._dict(self._row(connection, row["id"]))
            return None

    def verify_lease(self, job_id, worker_id, lease_token):
        with self._connection() as connection:
            return self._dict(self._leased(connection, job_id, worker_id, lease_token, time.time()))

    def heartbeat(self, job_id, worker_id, lease_token, progress=None, stage=None):
        if progress is not None and (
            isinstance(progress, bool)
            or not isinstance(progress, (float, int))
            or not math.isfinite(progress)
        ):
            raise ValueError("progress must be finite")
        if stage is not None:
            _string(stage, "stage", 128, allow_empty=True)
        with self._transaction() as connection:
            now = time.time()
            row = self._leased(connection, job_id, worker_id, lease_token, now)
            connection.execute(
                """UPDATE web_jobs SET lease_until=?,updated_at=?,progress=?,
                stage=? WHERE id=?""",
                (
                    now + self.lease_seconds,
                    now,
                    row["progress"] if progress is None else min(100, max(0, progress)),
                    row["stage"] if stage is None else stage,
                    job_id,
                ),
            )
            return self._dict(self._row(connection, job_id))

    def add_artifact(self, job_id, worker_id, lease_token, name, storage_ref, size_bytes):
        if name not in ARTIFACT_NAMES:
            raise ValueError("Unknown artifact name")
        _string(storage_ref, "storage_ref", 2048)
        _size(size_bytes)
        with self._transaction() as connection:
            now = time.time()
            row = self._leased(connection, job_id, worker_id, lease_token, now)
            if row["cancel_requested"]:
                raise JobConflict("Job cancellation requested")
            self._attach_ref(connection, storage_ref)
            artifacts = json.loads(row["artifacts"])
            stale = json.loads(row["stale_refs"])
            previous = artifacts.get(name)
            if previous and previous["storage_ref"] != storage_ref:
                stale.append(previous["storage_ref"])
            artifacts[name] = {"storage_ref": storage_ref, "size_bytes": size_bytes}
            connection.execute(
                "UPDATE web_jobs SET artifacts=?,stale_refs=?,updated_at=? WHERE id=?",
                (json.dumps(artifacts), json.dumps(list(dict.fromkeys(stale))), now, job_id),
            )
            return self._dict(self._row(connection, job_id))

    def complete(self, job_id, worker_id, lease_token):
        with self._transaction() as connection:
            now = time.time()
            row = self._leased(connection, job_id, worker_id, lease_token, now)
            if row["cancel_requested"]:
                self._finish(connection, job_id, "cancelled", now)
            else:
                if set(json.loads(row["artifacts"])) != ARTIFACT_NAMES:
                    raise JobConflict("All three result artifacts are required")
                self._finish(connection, job_id, "completed", now)
                connection.execute(
                    "UPDATE web_jobs SET progress=100,stage='completed' WHERE id=?", (job_id,)
                )
            return self._dict(self._row(connection, job_id))

    def fail(self, job_id, worker_id, lease_token, error, cancelled=False):
        _string(error, "error", 2048, allow_empty=True)
        with self._transaction() as connection:
            now = time.time()
            row = self._leased(connection, job_id, worker_id, lease_token, now)
            status = "cancelled" if cancelled or row["cancel_requested"] else "failed"
            self._finish(connection, job_id, status, now, error)
            return self._dict(self._row(connection, job_id))

    def expired_cleanup_records(self):
        """Lease retained references for five minutes so failed cleanup can retry."""
        with self._transaction() as connection:
            now = time.time()
            rows = connection.execute(
                """SELECT * FROM web_jobs
                WHERE status IN ('completed','failed','cancelled','uploading') AND expires_at<=?
                AND (cleanup_until IS NULL OR cleanup_until<=?)""",
                (now, now),
            ).fetchall()
            result = []
            for row in rows:
                connection.execute(
                    "UPDATE web_jobs SET cleanup_token=?,cleanup_until=? WHERE id=?",
                    (secrets.token_urlsafe(24), now + 300, row["id"]),
                )
                result.append(self._dict(self._row(connection, row["id"])))
            return result

    def expire(self, job_id, cleanup_token=None):
        """Forget deleted references, optionally fencing with the cleanup lease."""
        with self._transaction() as connection:
            now = time.time()
            row = self._row(connection, job_id)
            if row["status"] == "expired":
                return self._dict(row)
            if (
                row["status"] not in (*TERMINAL_STATES, "uploading")
                or row["expires_at"] is None
                or row["expires_at"] > now
            ):
                raise JobConflict("Job retention has not expired")
            if cleanup_token is not None and (
                not isinstance(cleanup_token, str)
                or not row["cleanup_token"]
                or not secrets.compare_digest(row["cleanup_token"].encode(), cleanup_token.encode())
                or row["cleanup_until"] <= now
            ):
                raise JobConflict("Cleanup lease expired or was replaced")
            connection.execute(
                """UPDATE web_jobs SET status='expired',input_ref=NULL,
                artifacts='{}',stale_refs='[]',updated_at=?,cleanup_token=NULL,cleanup_until=NULL
                WHERE id=?""",
                (now, job_id),
            )
            return self._dict(self._row(connection, job_id))

    @staticmethod
    def _attach_ref(connection, storage_ref):
        orphan = connection.execute(
            "SELECT cleanup_token FROM web_orphans WHERE storage_ref=?", (storage_ref,)
        ).fetchone()
        if orphan and orphan["cleanup_token"]:
            raise JobConflict("The uploaded file has already been claimed for cleanup")
        connection.execute("DELETE FROM web_orphans WHERE storage_ref=?", (storage_ref,))

    def register_orphan(self, storage_ref, delay_seconds=600):
        """Durably record a new upload until queue/result attachment commits."""
        _string(storage_ref, "storage_ref", 2048)
        if not math.isfinite(delay_seconds) or delay_seconds < 0:
            raise ValueError("Orphan cleanup delay must be finite and nonnegative")
        with self._transaction() as connection:
            now = time.time()
            if connection.execute(
                "SELECT 1 FROM web_jobs WHERE input_ref=? LIMIT 1", (storage_ref,)
            ).fetchone():
                return
            for row in connection.execute("SELECT artifacts,stale_refs FROM web_jobs"):
                if storage_ref in json.loads(row["stale_refs"]) or any(
                    artifact["storage_ref"] == storage_ref
                    for artifact in json.loads(row["artifacts"]).values()
                ):
                    return
            connection.execute(
                """INSERT OR IGNORE INTO web_orphans(storage_ref,created_at,cleanup_after)
                    VALUES(?,?,?)""",
                (storage_ref, now, now + delay_seconds),
            )

    def orphan_cleanup_records(self):
        """Claim unattached uploads for five minutes; retain them until deletion succeeds."""
        with self._transaction() as connection:
            now = time.time()
            rows = connection.execute(
                """SELECT storage_ref FROM web_orphans WHERE cleanup_after<=?
                    AND (cleanup_until IS NULL OR cleanup_until<=?)""",
                (now, now),
            ).fetchall()
            records = []
            for row in rows:
                token = secrets.token_urlsafe(24)
                connection.execute(
                    "UPDATE web_orphans SET cleanup_token=?,cleanup_until=? WHERE storage_ref=?",
                    (token, now + 300, row["storage_ref"]),
                )
                records.append({"storage_ref": row["storage_ref"], "cleanup_token": token})
            return records

    def delete_orphan(self, storage_ref, token=None):
        """Forget an orphan after storage deletion, optionally fenced by its cleanup lease."""
        with self._transaction() as connection:
            now = time.time()
            row = connection.execute(
                "SELECT * FROM web_orphans WHERE storage_ref=?", (storage_ref,)
            ).fetchone()
            if not row:
                return
            if token is not None and (
                not isinstance(token, str)
                or not row["cleanup_token"]
                or not secrets.compare_digest(row["cleanup_token"].encode(), token.encode())
                or row["cleanup_until"] <= now
            ):
                raise JobConflict("Orphan cleanup lease expired or was replaced")
            connection.execute("DELETE FROM web_orphans WHERE storage_ref=?", (storage_ref,))
