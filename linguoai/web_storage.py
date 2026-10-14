"""Private temporary storage for the web service; references are never local paths."""

from __future__ import annotations

import json
import math
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol

from .errors import ConfigurationError, DependencyError, FileOperationError


class Storage(Protocol):
    def upload(self, path: Path, *, job_id: str, name: str) -> str: ...

    def download(self, ref: str, destination: Path) -> None: ...

    def delete(self, ref: str) -> None: ...


def _check_upload(path: Path, job_id: str, name: str) -> Path:
    path = Path(path)
    if not path.is_file():
        raise FileOperationError("The upload source is not a file.")
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", job_id):
        raise ConfigurationError("Invalid storage job ID.")
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 200
        or name in {".", ".."}
        or any(character in name for character in "/\\\x00")
        or any(ord(character) < 32 for character in name)
    ):
        raise ConfigurationError("Invalid storage filename.")
    return path


def _temporary_destination(destination: Path) -> tuple[int, str]:
    destination = Path(destination)
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise FileOperationError("The download destination is not a regular file.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return tempfile.mkstemp(prefix=".linguoai-download-", dir=destination.parent)


class LocalStorage:
    """Atomic local storage with opaque UUID references and no directory traversal."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise ConfigurationError("The local storage root must be a directory.")

    def _root(self) -> Path:
        if self.root.is_symlink() or self.root.resolve() != self.root or not self.root.is_dir():
            raise FileOperationError("The storage root has changed.")
        return self.root

    def _path(self, ref: str) -> Path:
        if not isinstance(ref, str) or not re.fullmatch(r"[0-9a-f]{32}", ref):
            raise FileOperationError("Invalid local storage reference.")
        root = self._root()
        path = root / ref
        if path.is_symlink() or path.resolve().parent != root:
            raise FileOperationError("Storage reference escapes the private storage root.")
        if path.exists() and not path.is_file():
            raise FileOperationError("Storage reference is not a regular file.")
        return path

    def upload(self, path: Path, *, job_id: str, name: str) -> str:
        source = _check_upload(path, job_id, name)
        root = self._root()
        ref = uuid.uuid4().hex
        descriptor, temporary = tempfile.mkstemp(prefix=".upload-", dir=root)
        try:
            with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._path(ref))
            return ref
        finally:
            with suppress(FileNotFoundError):
                Path(temporary).unlink()

    def download(self, ref: str, destination: Path) -> None:
        source = self._path(ref)
        descriptor, temporary = _temporary_destination(destination)
        try:
            with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output, length=1024 * 1024)
            os.replace(temporary, destination)
        finally:
            with suppress(FileNotFoundError):
                Path(temporary).unlink()

    def file_path(self, ref: str) -> Path:
        """Return a checked private file for server-side streaming without a second copy."""
        path = self._path(ref)
        if not path.is_file():
            raise FileNotFoundError("The stored file is unavailable.")
        return path

    def delete(self, ref: str) -> None:
        with suppress(FileNotFoundError):
            self._path(ref).unlink()


def _media_upload(path: Path) -> Any:
    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise DependencyError("Google Drive storage requires the 'drive' extra.") from exc
    return MediaFileUpload(
        str(path),
        mimetype=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        chunksize=8 * 1024 * 1024,
        resumable=True,
    )


def _media_download(output: Any, request: Any) -> Any:
    try:
        from googleapiclient.http import MediaIoBaseDownload
    except ImportError as exc:
        raise DependencyError("Google Drive storage requires the 'drive' extra.") from exc
    return MediaIoBaseDownload(output, request, chunksize=8 * 1024 * 1024)


class DriveStorage:
    """One private Drive folder; no public URLs, client credentials, or arbitrary file IDs.

    Credentials are operator-controlled authorized-user JSON or ADC. Service accounts
    require a real Shared Drive, checked against the folder metadata before first use.
    The Google client is serialized because httplib2 transports are not thread safe.
    """

    def __init__(
        self,
        folder_id: str,
        credentials_path: str | Path | None = None,
        shared_drive: bool = False,
        service: Any | None = None,
        *,
        timeout_seconds: float = 60,
        retries: int = 3,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", folder_id) or folder_id == "root":
            raise ConfigurationError("A private Google Drive folder ID is required.")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ConfigurationError("Drive timeout must be positive and finite.")
        if not isinstance(retries, int) or not 0 <= retries <= 5:
            raise ConfigurationError("Drive retries must be between 0 and 5.")
        self.folder_id = folder_id
        self.shared_drive = shared_drive
        self.retries = retries
        self._lock = threading.RLock()
        self._folder_checked = False
        self.service = service or self._build_service(credentials_path, timeout_seconds)

    def _build_service(self, credentials_path: str | Path | None, timeout: float) -> Any:
        if credentials_path is not None:
            try:
                credential_type = json.loads(Path(credentials_path).read_text("utf-8")).get("type")
            except (OSError, ValueError, AttributeError) as exc:
                raise ConfigurationError("Cannot read the operator's Drive credentials.") from exc
            if credential_type not in {"authorized_user", "service_account"}:
                raise ConfigurationError("Use authorized-user or service-account Drive JSON.")
            if credential_type == "service_account" and not self.shared_drive:
                raise ConfigurationError("Service-account storage requires a Shared Drive.")
        try:
            import google.auth
            import google_auth_httplib2
            import httplib2
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise DependencyError("Google Drive storage requires the 'drive' extra.") from exc
        scopes = ["https://www.googleapis.com/auth/drive.file"]
        try:
            if credentials_path is None:
                credentials, _project = google.auth.default(scopes=scopes)
            else:
                credentials, _project = google.auth.load_credentials_from_file(
                    str(credentials_path), scopes=scopes
                )
            if isinstance(credentials, service_account.Credentials) and not self.shared_drive:
                raise ConfigurationError("Service-account storage requires a Shared Drive.")
            transport = google_auth_httplib2.AuthorizedHttp(
                credentials, http=httplib2.Http(timeout=timeout)
            )
            return build("drive", "v3", http=transport, cache_discovery=False)
        except ConfigurationError:
            raise
        except Exception as exc:
            # Credential-loading errors may include JSON/key details. Do not expose them.
            raise ConfigurationError("Cannot initialize authorized Google Drive access.") from exc

    def _check_folder(self) -> None:
        if self._folder_checked:
            return
        metadata = (
            self.service.files()
            .get(
                fileId=self.folder_id,
                fields="id,mimeType,driveId,trashed,permissions(type)",
                supportsAllDrives=True,
            )
            .execute(num_retries=self.retries)
        )
        if metadata.get("mimeType") != "application/vnd.google-apps.folder" or metadata.get(
            "trashed"
        ):
            raise ConfigurationError("The configured Drive folder is unavailable.")
        if self.shared_drive and not metadata.get("driveId"):
            raise ConfigurationError("The configured folder is not in a Shared Drive.")
        if any(item.get("type") == "anyone" for item in metadata.get("permissions", [])):
            raise ConfigurationError("The temporary Drive folder must not be publicly shared.")
        self._folder_checked = True

    def _file_id(self, ref: str) -> str:
        if not isinstance(ref, str) or not re.fullmatch(r"drive:[A-Za-z0-9_-]{1,200}", ref):
            raise FileOperationError("Invalid Google Drive storage reference.")
        file_id = ref.removeprefix("drive:")
        if file_id == self.folder_id or file_id == "root":
            raise FileOperationError("The storage folder cannot be used as a file reference.")
        self._check_folder()
        metadata = (
            self.service.files()
            .get(
                fileId=file_id,
                fields="id,parents,appProperties,mimeType,trashed",
                supportsAllDrives=True,
            )
            .execute(num_retries=self.retries)
        )
        if (
            metadata.get("parents") != [self.folder_id]
            or metadata.get("appProperties", {}).get("linguoai") != "1"
            or metadata.get("mimeType") == "application/vnd.google-apps.folder"
        ):
            raise FileOperationError(
                "Drive reference is outside this application's private folder."
            )
        if metadata.get("trashed"):
            raise FileNotFoundError("Drive file has already been deleted.")
        return file_id

    def upload(self, path: Path, *, job_id: str, name: str) -> str:
        source = _check_upload(path, job_id, name)
        with self._lock:
            self._check_folder()
            media = _media_upload(source)
            try:
                request = self.service.files().create(
                    body={
                        "name": name,
                        "parents": [self.folder_id],
                        "appProperties": {
                            "linguoai": "1",
                            "job_id": job_id,
                            # Drive custom properties have a 124-byte key+value limit.
                            "name": name.encode("utf-8")[:120].decode("utf-8", errors="ignore"),
                        },
                    },
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                )
                response = None
                while response is None:
                    _status, response = request.next_chunk(num_retries=self.retries)
                file_id = response.get("id", "")
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", file_id):
                    raise FileOperationError("Drive returned no valid file reference.")
                return f"drive:{file_id}"
            finally:
                # MediaFileUpload owns a file descriptor even when the request fails.
                stream = getattr(media, "stream", None)
                if stream is not None:
                    stream().close()

    def download(self, ref: str, destination: Path) -> None:
        with self._lock:
            file_id = self._file_id(ref)
            descriptor, temporary = _temporary_destination(destination)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    request = self.service.files().get_media(fileId=file_id, supportsAllDrives=True)
                    download = _media_download(output, request)
                    done = False
                    while not done:
                        _status, done = download.next_chunk(num_retries=self.retries)
                os.replace(temporary, destination)
            finally:
                with suppress(FileNotFoundError):
                    Path(temporary).unlink()

    def delete(self, ref: str) -> None:
        with self._lock:
            try:
                file_id = self._file_id(ref)
                self.service.files().delete(fileId=file_id, supportsAllDrives=True).execute(
                    num_retries=self.retries
                )
            except FileNotFoundError:
                return
            except Exception as exc:
                if getattr(getattr(exc, "resp", None), "status", None) != 404:
                    raise

    def close(self) -> None:
        with self._lock:
            close = getattr(self.service, "close", None)
            if close is not None:
                close()


class StorageRouter:
    """Route persisted references while using Drive for new uploads when configured.

    Existing local references remain readable after activating Drive. Provider failures
    are returned to the caller; new uploads never silently switch to another backend.
    """

    def __init__(
        self, local_storage: LocalStorage, drive_storage: DriveStorage | None = None
    ) -> None:
        self.local_storage = local_storage
        self.drive_storage = drive_storage

    @staticmethod
    def is_local_ref(ref: str) -> bool:
        return isinstance(ref, str) and re.fullmatch(r"(?:local:)?[0-9a-f]{32}", ref) is not None

    def _target(self, ref: str) -> tuple[Storage, str]:
        if self.is_local_ref(ref):
            return self.local_storage, ref.removeprefix("local:")
        if isinstance(ref, str) and re.fullmatch(r"drive:[A-Za-z0-9_-]{1,200}", ref):
            if self.drive_storage is None:
                raise ConfigurationError("Google Drive storage is not configured.")
            return self.drive_storage, ref
        raise FileOperationError("Invalid storage reference.")

    def upload(self, path: Path, *, job_id: str, name: str) -> str:
        storage = self.drive_storage if self.drive_storage is not None else self.local_storage
        return storage.upload(path, job_id=job_id, name=name)

    def download(self, ref: str, destination: Path) -> None:
        storage, provider_ref = self._target(ref)
        storage.download(provider_ref, destination)

    def delete(self, ref: str) -> None:
        storage, provider_ref = self._target(ref)
        storage.delete(provider_ref)

    def file_path(self, ref: str) -> Path:
        """Stream only checked local files; Drive references require a download."""
        if not self.is_local_ref(ref):
            raise FileOperationError("Direct streaming requires a local storage reference.")
        return self.local_storage.file_path(ref.removeprefix("local:"))

    def close(self) -> None:
        if self.drive_storage is not None:
            self.drive_storage.close()
