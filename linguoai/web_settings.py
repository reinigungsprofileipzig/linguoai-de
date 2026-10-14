"""Serverseitige Konfiguration und signierte Browser-Sitzungen."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def password_hash(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Das Web-Passwort muss mindestens zwölf Zeichen enthalten.")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 390_000)
    return f"pbkdf2_sha256$390000${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, count, salt, expected = encoded.split("$")
        iterations = int(count)
        if algorithm != "pbkdf2_sha256" or not 100_000 <= iterations <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), iterations
        ).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class ServerSettings:
    state_directory: Path
    users: dict[str, str] = field(repr=False)
    session_secret: str = field(repr=False)
    worker_token: str = field(repr=False)
    public_url: str = "http://127.0.0.1:8765"
    storage_backend: str = "local"
    drive_folder_id: str = ""
    drive_credentials_path: Path | None = None
    drive_shared: bool = False
    max_upload_bytes: int = 512 * 1024**2
    max_result_bytes: int = 2 * 1024**3
    max_active_per_owner: int = 3
    max_active_total: int = 100
    retention_seconds: int = 86400
    lease_seconds: int = 90
    session_seconds: int = 43200
    worker_online_seconds: int = 60
    website_directory: Path | None = None

    def validated(self) -> ServerSettings:
        if not self.users or any(not key or not value for key, value in self.users.items()):
            raise ValueError("Mindestens ein Web-Benutzer mit Passwort-Hash ist erforderlich.")
        if len(self.session_secret) < 32 or len(self.worker_token) < 32:
            raise ValueError("Sitzungs- und Worker-Schlüssel müssen mindestens 32 Zeichen haben.")
        url = urlsplit(self.public_url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.query or url.fragment:
            raise ValueError("LINGUOAI_PUBLIC_URL muss eine HTTP(S)-Adresse ohne Query sein.")
        if url.username or url.password or ".." in url.path.split("/"):
            raise ValueError("Ungültige öffentliche Serveradresse.")
        if url.scheme != "https" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Ein öffentlicher Dienst benötigt HTTPS.")
        if self.storage_backend not in {"local", "drive"}:
            raise ValueError("Speicher muss local oder drive sein.")
        if self.storage_backend == "drive" and not self.drive_folder_id:
            raise ValueError("Für Drive fehlt die private Ordner-ID.")
        if not 1024 <= self.max_upload_bytes <= 10 * 1024**3:
            raise ValueError("Uploadgrenze muss zwischen 1 KB und 10 GB liegen.")
        if not 1024 <= self.max_result_bytes <= 10 * 1024**3:
            raise ValueError("Ergebnisgrenze muss zwischen 1 KB und 10 GB liegen.")
        if self.session_seconds < 60 or self.worker_online_seconds < 10:
            raise ValueError("Sitzungs- oder Worker-Zeitfenster ist zu kurz.")
        if self.lease_seconds < 30 or self.retention_seconds < 60:
            raise ValueError("Lease oder Aufbewahrungsfrist sind zu kurz.")
        return self

    @property
    def public_path(self) -> str:
        return urlsplit(self.public_url).path.rstrip("/")

    @property
    def secure_cookies(self) -> bool:
        return urlsplit(self.public_url).scheme == "https"

    @classmethod
    def from_env(cls) -> ServerSettings:
        config_path = Path(os.environ.get("LINGUOAI_WEB_CONFIG", ".linguoai-web/server.json"))
        data = json.loads(config_path.read_text("utf-8")) if config_path.is_file() else {}
        state = Path(
            os.environ.get("LINGUOAI_WEB_STATE", data.get("state_directory", ".linguoai-web"))
        )
        users = data.get("users", {})
        if os.environ.get("LINGUOAI_WEB_PASSWORD_HASH"):
            users = {
                os.environ.get("LINGUOAI_WEB_USERNAME", "admin"): os.environ[
                    "LINGUOAI_WEB_PASSWORD_HASH"
                ]
            }
        credentials = os.environ.get(
            "LINGUOAI_DRIVE_CREDENTIALS", data.get("drive_credentials_path")
        )
        return cls(
            state_directory=state.expanduser().resolve(),
            users=users,
            session_secret=os.environ.get(
                "LINGUOAI_SESSION_SECRET", data.get("session_secret", "")
            ),
            worker_token=os.environ.get("LINGUOAI_WORKER_TOKEN", data.get("worker_token", "")),
            public_url=os.environ.get(
                "LINGUOAI_PUBLIC_URL", data.get("public_url", "http://127.0.0.1:8765")
            ),
            storage_backend=os.environ.get(
                "LINGUOAI_STORAGE", data.get("storage_backend", "local")
            ),
            drive_folder_id=os.environ.get(
                "LINGUOAI_DRIVE_FOLDER_ID", data.get("drive_folder_id", "")
            ),
            drive_credentials_path=Path(credentials).expanduser().resolve()
            if credentials
            else None,
            drive_shared=os.environ.get(
                "LINGUOAI_DRIVE_SHARED", str(data.get("drive_shared", False))
            ).lower()
            == "true",
            max_upload_bytes=int(
                os.environ.get(
                    "LINGUOAI_MAX_UPLOAD_BYTES", data.get("max_upload_bytes", 512 * 1024**2)
                )
            ),
            max_result_bytes=int(data.get("max_result_bytes", 2 * 1024**3)),
            max_active_per_owner=int(data.get("max_active_per_owner", 3)),
            max_active_total=int(data.get("max_active_total", 100)),
            retention_seconds=int(data.get("retention_seconds", 86400)),
            lease_seconds=int(data.get("lease_seconds", 90)),
            website_directory=Path(data["website_directory"])
            if data.get("website_directory")
            else None,
        ).validated()


class Sessions:
    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings

    def issue(self, owner: str) -> str:
        payload = json.dumps(
            {
                "owner": owner,
                "exp": int(time.time()) + self.settings.session_seconds,
                "nonce": secrets.token_hex(16),
            },
            separators=(",", ":"),
        ).encode()
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        signature = hmac.new(
            self.settings.session_secret.encode(), encoded.encode(), hashlib.sha256
        ).hexdigest()
        return f"{encoded}.{signature}"

    def owner(self, cookie: str | None) -> str | None:
        if not cookie or len(cookie) > 2048:
            return None
        try:
            encoded, signature = cookie.rsplit(".", 1)
            expected = hmac.new(
                self.settings.session_secret.encode(), encoded.encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return None
            data = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            if data["exp"] <= time.time() or data["owner"] not in self.settings.users:
                return None
            return data["owner"]
        except (ValueError, KeyError, TypeError):
            return None
