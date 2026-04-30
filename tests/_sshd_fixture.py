"""Real localhost sshd fixture for SSH-provider integration tests."""
from __future__ import annotations

import getpass
import os
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SSHD_BIN = shutil.which("sshd") or "/usr/sbin/sshd"
SSH_BIN = shutil.which("ssh") or "/usr/bin/ssh"
SSH_KEYGEN_BIN = shutil.which("ssh-keygen") or "/usr/bin/ssh-keygen"


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@contextmanager
def localhost_sshd() -> Iterator[dict[str, str | int]]:
    tmp = Path(tempfile.mkdtemp(prefix="evo-sshd-"))
    local_home = tmp / "local-home"
    local_home.mkdir()
    host_key = tmp / "ssh_host_ed25519_key"
    client_key = tmp / "ssh_client_ed25519_key"
    authorized_keys = tmp / "authorized_keys"
    sshd_config = tmp / "sshd_config"
    port = _free_port()
    user = getpass.getuser()

    subprocess.run(
        [SSH_KEYGEN_BIN, "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [SSH_KEYGEN_BIN, "-q", "-t", "ed25519", "-N", "", "-f", str(client_key)],
        check=True,
        capture_output=True,
    )
    authorized_keys.write_text(
        (client_key.with_suffix(".pub")).read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    sshd_config.write_text(
        "\n".join([
            f"Port {port}",
            "ListenAddress 127.0.0.1",
            f"HostKey {host_key}",
            f"PidFile {tmp / 'sshd.pid'}",
            f"AuthorizedKeysFile {authorized_keys}",
            "PubkeyAuthentication yes",
            "PasswordAuthentication no",
            "KbdInteractiveAuthentication no",
            "ChallengeResponseAuthentication no",
            "UsePAM no",
            "PermitRootLogin no",
            f"AllowUsers {user}",
            "StrictModes no",
            "LogLevel VERBOSE",
            "PrintMotd no",
            "Subsystem sftp internal-sftp",
            "",
        ]),
        encoding="utf-8",
    )

    proc = subprocess.Popen(
        [SSHD_BIN, "-D", "-e", "-f", str(sshd_config)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            check = socket.socket()
            try:
                check.settimeout(0.2)
                check.connect(("127.0.0.1", port))
                break
            except OSError:
                if proc.poll() is not None:
                    stderr = proc.stderr.read().decode("utf-8", errors="replace")
                    raise RuntimeError(f"sshd exited early: {stderr[:1000]}")
                time.sleep(0.1)
            finally:
                check.close()
        else:
            raise RuntimeError("sshd did not start listening within 10s")

        env = os.environ.copy()
        env["HOME"] = str(local_home)
        login = subprocess.run(
            [
                SSH_BIN,
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-p", str(port),
                "-i", str(client_key),
                f"{user}@127.0.0.1",
                "printf ok",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if login.returncode != 0 or login.stdout.strip() != "ok":
            raise RuntimeError(
                f"ssh self-login failed: {(login.stderr or login.stdout)[:1000]}"
            )

        yield {
            "host": f"{user}@127.0.0.1",
            "port": port,
            "key": str(client_key),
            "local_home": str(local_home),
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        shutil.rmtree(tmp, ignore_errors=True)
