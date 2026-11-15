import errno
import hmac
import json
import os
import secrets
import socket
import subprocess  # nosec B404
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import httpx2
from filelock import FileLock, Timeout

from claudex.domain.readiness import readiness_proof
from claudex.infrastructure.gateway_settings import (
    GatewayNotPublishedError,
    GatewaySettingsError,
    GatewaySettingsStore,
    GatewaySnapshot,
    _file_info,
    _locked,
)

_START_TIMEOUT = 45.0
_MAX_PROOF_BYTES = 1024


def _probe(port: int, token: str) -> bool:
    challenge = secrets.token_hex(32)
    proof = readiness_proof(token.encode("ascii"), challenge)
    try:
        with (
            httpx2.Client(timeout=1.0, trust_env=False, follow_redirects=False) as client,
            client.stream(
                "GET",
                f"http://127.0.0.1:{port}/health/ready",
                headers={"X-Claudex-Challenge": challenge},
            ) as response,
        ):
            if response.status_code != 200:
                return False
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > _MAX_PROOF_BYTES:
                    return False
        decoded = json.loads(content)
        provided = decoded.get("proof") if isinstance(decoded, dict) else None
        return (
            isinstance(provided, str)
            and provided.isascii()
            and hmac.compare_digest(provided, proof)
        )
    except httpx2.HTTPError, ValueError, UnicodeError:
        return False


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.settimeout(1.0)
        result = listener.connect_ex(("127.0.0.1", port))
    if result in (errno.ECONNREFUSED, getattr(errno, "WSAECONNREFUSED", 10061)):
        return True
    if result == 0:
        return False
    raise GatewaySettingsError("Could not check the local gateway port. Try again.")


@contextmanager
def _startup_lock(state_dir: Path, port: int) -> Iterator[None]:
    directory = state_dir / "gateways"
    with _locked(directory, "desktop-init.lock", private=True):
        lock_path = directory / f"desktop-start-{port}.lock"
        _file_info(lock_path, private=True)
    try:
        with FileLock(lock_path, timeout=_START_TIMEOUT + 5, mode=0o600):
            yield
    except Timeout:
        raise GatewaySettingsError("The Claudex gateway is starting. Try again.") from None


def _snapshot(state_dir: Path, port: int) -> GatewaySnapshot | None:
    try:
        return GatewaySettingsStore(state_dir).snapshot(port)
    except GatewayNotPublishedError:
        return None


def _token(snapshot: GatewaySnapshot) -> str:
    return snapshot.settings["env"]["ANTHROPIC_AUTH_TOKEN"]


def gateway_status(state_dir: Path, port: int) -> str:
    snapshot = _snapshot(state_dir, port)
    if snapshot is not None and _probe(port, _token(snapshot)):
        return "running"
    return "stopped" if _port_available(port) else "unverified"


def ensure_gateway(
    state_dir: Path,
    port: int,
    command: tuple[str, ...] | None,
    *,
    expected_token: str | None = None,
) -> GatewaySnapshot:
    with _startup_lock(state_dir, port):
        current = _snapshot(state_dir, port)
        if expected_token is not None and (
            current is None or not hmac.compare_digest(_token(current), expected_token)
        ):
            raise GatewaySettingsError("Gateway settings changed. Try again.")
        if current is not None and _probe(port, _token(current)):
            return current
        if not _port_available(port):
            raise GatewaySettingsError(
                "The local port is occupied by a different or older gateway. "
                "Stop it before starting Claudex."
            )
        if command is None:
            raise GatewaySettingsError("Start the gateway with claudex serve first.")
        return _start_gateway(state_dir, port, command, expected_token)


def _start_gateway(
    state_dir: Path, port: int, command: tuple[str, ...], expected_token: str | None
) -> GatewaySnapshot:
    if not command or not Path(command[0]).is_absolute():
        raise GatewaySettingsError("Claudex needs an absolute executable to start the gateway.")
    child_env = dict(os.environ, CLAUDEX_STATE_DIR=str(state_dir))
    if getattr(sys, "frozen", False):
        child_env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    windows = sys.platform == "win32"
    flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if windows
        else 0
    )
    try:
        process = subprocess.Popen(  # noqa: S603  # nosec B603
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=child_env,
            start_new_session=not windows,
            creationflags=flags,
        )
    except OSError:
        raise GatewaySettingsError("Could not start the Claudex gateway.") from None
    try:
        return _wait_started(process, state_dir, port, expected_token)
    except BaseException:
        if process.poll() is None:
            process.terminate()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=3)
            if process.poll() is None:
                process.kill()
                process.wait()
        raise


def _wait_started(
    process: subprocess.Popen[bytes], state_dir: Path, port: int, expected_token: str | None
) -> GatewaySnapshot:
    deadline = time.monotonic() + _START_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise GatewaySettingsError(
                "The Claudex gateway stopped during startup. "
                f"Run claudex serve --foreground --port {port} to see the cause."
            )
        try:
            current = _snapshot(state_dir, port)
        except GatewaySettingsError:
            current = None
        if current is not None and _probe(port, _token(current)):
            if expected_token is not None and not hmac.compare_digest(
                _token(current), expected_token
            ):
                raise GatewaySettingsError("Gateway settings changed. Try again.")
            return current
        time.sleep(0.2)
    raise GatewaySettingsError("The Claudex gateway did not become ready.")


def stop_gateway(state_dir: Path, port: int) -> bool:
    with _startup_lock(state_dir, port):
        snapshot = _snapshot(state_dir, port)
        if snapshot is None or not _probe(port, _token(snapshot)):
            if _port_available(port):
                return False
            raise GatewaySettingsError("The local port belongs to an unverified gateway.")
        token = _token(snapshot).encode("ascii")
        try:
            with httpx2.Client(timeout=3.0, trust_env=False, follow_redirects=False) as client:
                url = f"http://127.0.0.1:{port}"
                with client.stream("GET", url + "/control/challenge") as response:
                    response.raise_for_status()
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > _MAX_PROOF_BYTES:
                            raise GatewaySettingsError(
                                "Could not verify the gateway shutdown request."
                            )
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise GatewaySettingsError("Could not verify the gateway shutdown request.")
                challenge = payload.get("challenge")
                proof = payload.get("proof")
                if (
                    not isinstance(challenge, str)
                    or len(challenge) != 64
                    or any(c not in "0123456789abcdef" for c in challenge)
                    or not isinstance(proof, str)
                    or not proof.isascii()
                    or not hmac.compare_digest(proof, readiness_proof(token, challenge))
                ):
                    raise GatewaySettingsError("Could not verify the gateway shutdown request.")
                signed = hmac.digest(
                    token, b"claudex-stop-v1:" + challenge.encode("ascii"), "sha256"
                ).hex()
                result = client.post(
                    url + "/control/stop",
                    headers={"X-Claudex-Challenge": challenge, "X-Claudex-Proof": signed},
                )
                result.raise_for_status()
        except httpx2.HTTPError, ValueError:
            raise GatewaySettingsError("Could not stop the Claudex gateway.") from None
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if not _probe(port, _token(snapshot)) and _port_available(port):
                remaining = max(0, deadline - time.monotonic())
                with GatewaySettingsStore(state_dir).running_instance(timeout=remaining):
                    return True
            time.sleep(0.1)
        raise GatewaySettingsError("The Claudex gateway did not stop in time.")
