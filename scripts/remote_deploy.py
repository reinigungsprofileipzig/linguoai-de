"""Run a bounded deployment operation over SSH; credentials arrive only on stdin.

This helper never saves or prints an SSH password. The input JSON provides password,
commands (literal operator commands), and optional upload pairs [local, remote].
Known changed SSH keys are rejected; first-use keys are reported and held in memory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from getpass import getpass
from pathlib import Path

import paramiko


class FirstUseKey(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected=None):
        self.expected = expected

    def missing_host_key(self, client, hostname, key):
        import base64

        fingerprint = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        if self.expected and fingerprint != self.expected:
            raise paramiko.SSHException("The SSH host fingerprint changed.")
        print(f"SSH first-use host key: {hostname} SHA256:{fingerprint}", flush=True)
        client.get_host_keys().add(hostname, key.get_name(), key)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--fingerprint")
    args = parser.parse_args()
    print("Ready for SSH operation JSON on stdin (credentials are not echoed).", flush=True)
    data = json.loads(getpass("SSH operation JSON (hidden): "))
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(FirstUseKey(args.fingerprint))
    try:
        client.connect(
            args.host,
            port=args.port,
            username=args.user,
            password=data.pop("password", None),
            timeout=20,
            banner_timeout=20,
            auth_timeout=20,
            look_for_keys=True,
        )
        if data.get("uploads") or data.get("downloads"):
            with client.open_sftp() as sftp:
                for local, remote in data.get("uploads", []):
                    sftp.put(str(Path(local)), remote)
                    print(f"Uploaded: {Path(local).name}", flush=True)
                for remote, local in data.get("downloads", []):
                    path = Path(local)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    sftp.get(remote, str(path))
                    print(f"Downloaded private operator file: {path.name}", flush=True)
        for command in data.get("commands", []):
            _stdin, stdout, stderr = client.exec_command(command, timeout=300)
            if data.get("command_stdin") is not None:
                _stdin.write(data["command_stdin"])
            _stdin.channel.shutdown_write()
            out, error = (
                stdout.read().decode(errors="replace"),
                stderr.read().decode(errors="replace"),
            )
            status = stdout.channel.recv_exit_status()
            print(
                json.dumps(
                    {"exit_code": status, "stdout": out[-20000:], "stderr": error[-4000:]},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if status:
                raise SystemExit(status)
    finally:
        client.close()


if __name__ == "__main__":
    main()
