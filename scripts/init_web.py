"""Create a private server configuration; never echo passwords or worker tokens."""

from __future__ import annotations

import argparse
import json
import os
import secrets
from getpass import getpass
from pathlib import Path

from linguoai.web_settings import password_hash


def main():
    parser = argparse.ArgumentParser(description="LinguoAI Web-Zugang initialisieren")
    parser.add_argument("--config", type=Path, default=Path(".linguoai-web/server.json"))
    parser.add_argument("--state", type=Path, default=Path(".linguoai-web"))
    parser.add_argument("--public-url", default="http://127.0.0.1:8765")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--website", type=Path)
    parser.add_argument("--worker-env", type=Path)
    parser.add_argument("--model-directory", type=Path)
    parser.add_argument("--generate-login-file", type=Path)
    args = parser.parse_args()
    if args.config.exists():
        raise SystemExit("Konfiguration existiert bereits; vorhandene Schlüssel bleiben erhalten.")
    password = (
        secrets.token_urlsafe(24)
        if args.generate_login_file
        else getpass("Neues Web-Passwort (mindestens 12 Zeichen): ")
    )
    token = secrets.token_urlsafe(48)
    config = {
        "state_directory": str(args.state.resolve()),
        "users": {args.username: password_hash(password)},
        "session_secret": secrets.token_urlsafe(48),
        "worker_token": token,
        "public_url": args.public_url,
        "storage_backend": "local",
        "max_upload_bytes": 100 * 1024**2,
        "max_result_bytes": 1024**3,
        "max_active_per_owner": 2,
        "max_active_total": 20,
        "retention_seconds": 86400,
        "lease_seconds": 90,
    }
    if args.website:
        config["website_directory"] = str(args.website.resolve())
    args.config.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(args.config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(config, output, indent=2)
    if args.worker_env:
        args.worker_env.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(args.worker_env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(f"LINGUOAI_WORKER_TOKEN={token}\nLINGUOAI_WHISPER_MODEL=small\n")
            output.write(
                f"LINGUOAI_MODEL_CACHE={args.state.resolve() / 'cache'}\n"
                f"HF_HOME={args.state.resolve() / 'cache'}\n"
            )
            output.write(f"LINGUOAI_WORKER_TEMP={args.state.resolve() / 'work'}\n")
            if args.model_directory:
                output.write(
                    f"LINGUOAI_PARAKEET_MODEL_DIRECTORY={args.model_directory.resolve()}\n"
                )
    if args.generate_login_file:
        args.generate_login_file.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(args.generate_login_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(
                f"URL: {args.public_url.rstrip('/')}/web.html\n"
                f"Benutzer: {args.username}\nPasswort: {password}\n"
            )
    print(
        "Serverkonfiguration und gegebenenfalls private Startdateien erzeugt. "
        "Keine Zugangsdaten ausgegeben."
    )


if __name__ == "__main__":
    main()
