"""Authorize one operator-owned, private Google Drive folder for LinguoAI.

Run on the operator's computer with a Google Desktop OAuth client JSON. The browser
consent uses a local loopback callback, offline access and only drive.file. No
credentials or authorization response are printed; this script never changes the
live server configuration. On Windows, protect the output directory with account
ACLs; POSIX file modes do not replace Windows ACLs.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import tempfile
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
FOLDER_NAME = "linguoai uploads"
FOLDER_MIME = "application/vnd.google-apps.folder"
FOLDER_MARKER = {"linguoai_storage": "private_operator_v1"}
FOLDER_FIELDS = "id,mimeType,driveId,trashed,permissions(type,role)"
SERVER_CREDENTIALS_PATH = "/etc/linguoai-web/drive-credentials.json"


class SetupError(Exception):
    """A safe operator-facing message, containing no provider response or secret."""


def _json_file(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise SetupError("Die private JSON-Datei ist nicht verfügbar.")
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise SetupError("Die private JSON-Datei benötigt Dateirechte 0600.")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SetupError("Die private JSON-Datei kann nicht gelesen werden.") from exc
    if not isinstance(value, dict):
        raise SetupError("Die private JSON-Datei enthält kein gültiges Objekt.")
    return value


def _check_authorized_user(value: dict[str, Any]) -> None:
    if value.get("type") != "authorized_user" or any(
        not isinstance(value.get(key), str) or not value[key].strip()
        for key in ("client_id", "client_secret", "refresh_token")
    ):
        raise SetupError("Persönliches Drive benötigt autorisierte OAuth-Refresh-Zugangsdaten.")
    scopes = value.get("scopes", [])
    if isinstance(scopes, str):
        scopes = scopes.split()
    if not isinstance(scopes, list) or any(scope != DRIVE_SCOPE for scope in scopes):
        raise SetupError("Die Zugangsdaten dürfen nur den Drive-Dateizugriff verwenden.")
    token_uri = value.get("token_uri", "https://oauth2.googleapis.com/token")
    if token_uri not in {
        "https://oauth2.googleapis.com/token",
        "https://accounts.google.com/o/oauth2/token",
    }:
        raise SetupError("Die Zugangsdaten müssen den Google-OAuth-Dienst verwenden.")


def _private_json_write(path: Path, value: dict[str, Any]) -> None:
    """Publish a complete credential file atomically, using 0600 on POSIX."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise SetupError("Die Ausgabedatei ist kein regulärer privater Dateipfad.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".linguoai-drive-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            if os.name == "posix":
                os.fchmod(output.fileno(), 0o600)
            json.dump(value, output, indent=2, ensure_ascii=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError as exc:
        raise SetupError(
            "Die private Ausgabedatei konnte nicht sicher gespeichert werden."
        ) from exc
    finally:
        with suppress(FileNotFoundError):
            Path(temporary).unlink()


@contextmanager
def _silent_oauth_logging():
    # The SDK loopback handler logs the callback URL, including its authorization
    # code, at INFO. OAuth DEBUG logs can also contain tokens. Restore each logger
    # after this one operator flow; never dump provider exceptions to the console.
    prefixes = ("google_auth_oauthlib", "requests_oauthlib", "oauthlib")
    names = {"google_auth_oauthlib.flow", "requests_oauthlib.oauth2_session"}
    names.update(name for name in logging.Logger.manager.loggerDict if name.startswith(prefixes))
    loggers = [(logging.getLogger(name), logging.getLogger(name).disabled) for name in names]
    try:
        for logger, _previous in loggers:
            logger.disabled = True
        yield
    finally:
        for logger, previous in loggers:
            logger.disabled = previous


def authorize(client_file: Path, credentials_file: Path, *, timeout: int = 300) -> None:
    client = _json_file(client_file).get("installed")
    if (
        not isinstance(client, dict)
        or not client.get("client_id")
        or not client.get("client_secret")
    ):
        raise SetupError("Ein Google-OAuth-Client vom Typ Desktop-App wird benötigt.")
    if client.get("auth_uri") not in {
        "https://accounts.google.com/o/oauth2/auth",
        "https://accounts.google.com/o/oauth2/v2/auth",
    } or client.get("token_uri") not in {
        "https://oauth2.googleapis.com/token",
        "https://accounts.google.com/o/oauth2/token",
    }:
        raise SetupError("Die Client-Datei muss den Google-OAuth-Dienst verwenden.")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise SetupError("Die Drive-Abhängigkeiten fehlen: pip install '.[drive]'.") from exc
    try:
        flow = InstalledAppFlow.from_client_config(
            {"installed": client}, scopes=[DRIVE_SCOPE], autogenerate_code_verifier=True
        )
        with _silent_oauth_logging():
            credentials = flow.run_local_server(
                host="127.0.0.1",
                bind_addr="127.0.0.1",
                port=0,
                open_browser=True,
                timeout_seconds=timeout,
                access_type="offline",
                prompt="consent",
                authorization_prompt_message="Bitte bestätige die Google-Freigabe im Browser.",
                success_message="Google-Freigabe erhalten. Dieses Fenster kann geschlossen werden.",
            )
        if not credentials.refresh_token or not credentials.has_scopes([DRIVE_SCOPE]):
            raise SetupError("Google hat keinen dauerhaften Drive-Dateizugriff freigegeben.")
        value = {
            "type": "authorized_user",
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "refresh_token": credentials.refresh_token,
            "token_uri": credentials.token_uri,
            "scopes": [DRIVE_SCOPE],
        }
        _check_authorized_user(value)
        _private_json_write(credentials_file, value)
    except SetupError:
        raise
    except Exception as exc:
        raise SetupError(
            "Die Google-Freigabe wurde nicht abgeschlossen. Bitte erneut versuchen."
        ) from exc


def _service(credentials_file: Path) -> Any:
    _check_authorized_user(_json_file(credentials_file))
    try:
        import google.auth
        import google_auth_httplib2
        import httplib2
        from googleapiclient.discovery import build

        credentials, _project = google.auth.load_credentials_from_file(
            str(credentials_file), scopes=[DRIVE_SCOPE]
        )
        transport = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http(timeout=60))
        return build("drive", "v3", http=transport, cache_discovery=False)
    except ImportError as exc:
        raise SetupError("Die Drive-Abhängigkeiten fehlen: pip install '.[drive]'.") from exc
    except Exception as exc:
        raise SetupError("Der autorisierte Google-Drive-Zugriff ist nicht verfügbar.") from exc


def _folder_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value)
        or value == "root"
    ):
        raise SetupError("Google Drive hat keine gültige private Ordner-ID bereitgestellt.")
    return value


def _check_private_folder(service: Any, folder_id: str) -> str:
    folder_id = _folder_id(folder_id)
    metadata = (
        service.files()
        .get(fileId=folder_id, fields=FOLDER_FIELDS, supportsAllDrives=True)
        .execute(num_retries=3)
    )
    if (
        metadata.get("mimeType") != FOLDER_MIME
        or metadata.get("trashed")
        or metadata.get("driveId")
        or any(permission.get("role") != "owner" for permission in metadata.get("permissions", []))
    ):
        raise SetupError("Der Betreiberordner muss privat und im persönlichen Google Drive liegen.")
    return folder_id


def ensure_private_folder(service: Any, folder_id: str | None = None) -> str:
    """Reuse this OAuth client's app-created folder without changing sharing settings."""
    try:
        if folder_id is not None:
            return _check_private_folder(service, folder_id)
        candidates = []
        page_token = None
        while True:
            response = (
                service.files()
                .list(
                    q=(
                        "mimeType = 'application/vnd.google-apps.folder' and trashed = false "
                        "and 'me' in owners and appProperties has "
                        "{ key='linguoai_storage' and value='private_operator_v1' }"
                    ),
                    spaces="drive",
                    fields="nextPageToken,files(id)",
                    pageSize=100,
                    pageToken=page_token,
                )
                .execute(num_retries=3)
            )
            candidates.extend(_folder_id(item.get("id")) for item in response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        if candidates:
            return _check_private_folder(service, sorted(candidates)[0])
        created = (
            service.files()
            .create(
                body={"name": FOLDER_NAME, "mimeType": FOLDER_MIME, "appProperties": FOLDER_MARKER},
                fields="id",
            )
            .execute(num_retries=3)
        )
        return _check_private_folder(service, created.get("id"))
    except SetupError:
        raise
    except Exception as exc:
        raise SetupError(
            "Der private Google-Drive-Ordner konnte nicht eingerichtet werden."
        ) from exc


def smoke_test(service: Any, folder_id: str) -> None:
    """Check upload, download and deletion using fixed synthetic bytes, never user data."""
    from linguoai.web_storage import DriveStorage

    storage = DriveStorage(folder_id, service=service)
    ref = None
    try:
        with tempfile.TemporaryDirectory(prefix="linguoai-drive-check-") as temporary:
            source = Path(temporary) / "LINGUOAI-STORAGE-CHECK.txt"
            destination = Path(temporary) / "download.txt"
            payload = b"LinguoAI private operator storage check\n"
            source.write_bytes(payload)
            ref = storage.upload(source, job_id=f"setup_{uuid.uuid4().hex}", name=source.name)
            storage.download(ref, destination)
            if destination.read_bytes() != payload:
                raise SetupError("Der Drive-Download hat den Speichertest nicht bestanden.")
    except SetupError:
        raise
    except Exception as exc:
        raise SetupError(
            "Upload oder Download hat den Google-Drive-Speichertest nicht bestanden."
        ) from exc
    finally:
        if ref is not None:
            try:
                storage.delete(ref)
            except Exception as exc:
                raise SetupError(
                    "Der Speichertest konnte seine Testdatei nicht löschen. "
                    "Bitte LINGUOAI-STORAGE-CHECK.txt im Betreiberordner entfernen."
                ) from exc


def connect_drive(
    *,
    credentials_file: Path,
    config_fragment: Path,
    client_file: Path | None = None,
    skip_oauth: bool = False,
    folder_id: str | None = None,
    server_credentials_path: str = SERVER_CREDENTIALS_PATH,
    timeout: int = 300,
) -> dict[str, Any]:
    """Return a nonsecret storage fragment only after authorization and a real smoke test."""
    credentials_file, config_fragment = Path(credentials_file), Path(config_fragment)
    if credentials_file.resolve() == config_fragment.resolve() or (
        client_file is not None
        and Path(client_file).resolve() in {credentials_file.resolve(), config_fragment.resolve()}
    ):
        raise SetupError(
            "Client-, Zugangs- und Konfigurationsdatei benötigen getrennte Dateipfade."
        )
    if not server_credentials_path or not server_credentials_path.startswith("/"):
        raise SetupError("Der Server-Zugangsdatenpfad muss ein absoluter Linux-Dateipfad sein.")
    if not 30 <= timeout <= 1800:
        raise SetupError("Die Freigabe-Wartezeit muss zwischen 30 und 1800 Sekunden liegen.")
    if skip_oauth:
        _check_authorized_user(_json_file(credentials_file))
    elif client_file is None:
        raise SetupError("Die private Google-Desktop-Client-Datei fehlt (--client-file).")
    else:
        authorize(Path(client_file), credentials_file, timeout=timeout)
    service = _service(credentials_file)
    try:
        private_folder = ensure_private_folder(service, folder_id)
        smoke_test(service, private_folder)
        fragment = {
            "storage_backend": "drive",
            "drive_folder_id": private_folder,
            "drive_credentials_path": server_credentials_path,
            "drive_shared": False,
        }
        _private_json_write(config_fragment, fragment)
        return fragment
    finally:
        with suppress(Exception):
            service.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-file", type=Path, help="Private Google Desktop OAuth client JSON")
    parser.add_argument("--credentials-file", type=Path, required=True, help="Private refresh JSON")
    parser.add_argument(
        "--config-fragment", type=Path, help="Nonsecret, separate storage settings JSON"
    )
    parser.add_argument("--server-credentials-path", default=SERVER_CREDENTIALS_PATH)
    parser.add_argument("--folder-id", help="Previously app-authorized private operator folder ID")
    parser.add_argument(
        "--skip-oauth", action="store_true", help="Reuse existing authorized-user JSON"
    )
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)
    fragment = args.config_fragment or args.credentials_file.with_name("drive-settings.json")
    try:
        connect_drive(
            credentials_file=args.credentials_file,
            config_fragment=fragment,
            client_file=args.client_file,
            skip_oauth=args.skip_oauth,
            folder_id=args.folder_id,
            server_credentials_path=args.server_credentials_path,
            timeout=args.timeout,
        )
    except SetupError as exc:
        print(f"Drive-Einrichtung nicht abgeschlossen: {exc}")
        return 1
    except Exception:
        print("Drive-Einrichtung nicht abgeschlossen. Private Dateien und Berechtigungen prüfen.")
        return 1
    print("Drive-Freigabe, privater Ordner sowie Upload, Download und Löschung geprüft.")
    print(f"Konfigurationsfragment: {fragment}")
    print("Der Server verwendet weiterhin seine bestehende Konfiguration bis zur Aktivierung.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
