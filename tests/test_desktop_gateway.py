from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from claudex.domain.readiness import readiness_proof
from claudex.infrastructure import desktop_gateway
from claudex.infrastructure.gateway_settings import GatewaySettingsError, GatewaySettingsStore

_PORT = 18887


def _published(path: Path) -> dict[str, Any]:
    store = GatewaySettingsStore(path)
    token = store.load(_PORT, "synthetic-binding")
    settings: dict[str, Any] = {
        "env": {
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{_PORT}",
            "ANTHROPIC_AUTH_TOKEN": token,
        },
        "availableModels": ["claudex-opus"],
    }
    store.publish(_PORT, "synthetic-binding", token, settings)
    return settings


@pytest.mark.parametrize("valid", [True, False])
def test_readiness_probe_sends_challenge_without_credentials(
    monkeypatch: pytest.MonkeyPatch, valid: bool
) -> None:
    token = "t" * 45
    challenge = "a" * 64
    expected = readiness_proof(token.encode(), challenge) if valid else "0" * 64
    observed: dict[str, Any] = {}

    class Response:
        status_code = 200

        def iter_bytes(self) -> Iterator[bytes]:
            yield json.dumps({"proof": expected}).encode()

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            observed.update(kwargs)

        def __enter__(self) -> Client:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        @contextmanager
        def stream(self, method: str, url: str, **kwargs: Any) -> Iterator[Response]:
            observed.update(method=method, url=url, **kwargs)
            yield Response()

    monkeypatch.setattr(desktop_gateway.secrets, "token_hex", lambda _: challenge)
    monkeypatch.setattr(desktop_gateway.httpx2, "Client", Client)
    assert desktop_gateway._probe(_PORT, token) is valid
    assert observed["headers"] == {"X-Claudex-Challenge": challenge}
    assert token not in repr(observed)
    assert observed["trust_env"] is False and observed["follow_redirects"] is False


def test_existing_gateway_is_reused_without_a_restart_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _published(tmp_path)
    monkeypatch.setattr(desktop_gateway, "_probe", lambda *_: True)
    monkeypatch.setattr(desktop_gateway.subprocess, "Popen", lambda *_a, **_kw: pytest.fail())
    desktop_gateway.ensure_gateway(tmp_path, _PORT, None)


def test_occupied_port_fails_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _published(tmp_path)
    monkeypatch.setattr(desktop_gateway, "_probe", lambda *_: False)
    monkeypatch.setattr(desktop_gateway, "_port_available", lambda *_: False)
    monkeypatch.setattr(desktop_gateway.subprocess, "Popen", lambda *_a, **_kw: pytest.fail())
    with pytest.raises(GatewaySettingsError, match="occupied"):
        desktop_gateway.ensure_gateway(tmp_path, _PORT, ("/synthetic",))


def test_stopped_legacy_gateway_requests_explicit_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _published(tmp_path)
    monkeypatch.setattr(desktop_gateway, "_probe", lambda *_: False)
    monkeypatch.setattr(desktop_gateway, "_port_available", lambda *_: True)
    with pytest.raises(GatewaySettingsError, match="Start the gateway"):
        desktop_gateway.ensure_gateway(tmp_path, _PORT, None)


def test_background_process_starts_detached_and_waits_for_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _published(tmp_path)
    probes = iter([False, False, True])
    calls: list[tuple[Any, ...]] = []

    class Process:
        def poll(self) -> None:
            return None

    def spawn(command: tuple[str, ...], **kwargs: Any) -> Process:
        calls.append((command, kwargs))
        return Process()

    monkeypatch.setattr(desktop_gateway, "_probe", lambda *_: next(probes))
    monkeypatch.setattr(desktop_gateway, "_port_available", lambda *_: True)
    monkeypatch.setattr(desktop_gateway.time, "sleep", lambda _: None)
    monkeypatch.setattr(desktop_gateway.subprocess, "Popen", spawn)
    desktop_gateway.ensure_gateway(tmp_path, _PORT, ("/synthetic", "resume"))
    assert len(calls) == 1
    command, options = calls[0]
    assert command == ("/synthetic", "resume")
    assert options["start_new_session"] is True
    assert (
        options["stdin"]
        == options["stdout"]
        == options["stderr"]
        == desktop_gateway.subprocess.DEVNULL
    )


def test_gateway_early_exit_has_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _published(tmp_path)

    class FailedProcess:
        def poll(self) -> int:
            return 1

    monkeypatch.setattr(desktop_gateway, "_probe", lambda *_: False)
    monkeypatch.setattr(desktop_gateway, "_port_available", lambda *_: True)
    monkeypatch.setattr(desktop_gateway.subprocess, "Popen", lambda *_a, **_kw: FailedProcess())
    with pytest.raises(GatewaySettingsError, match="Run claudex serve"):
        desktop_gateway.ensure_gateway(tmp_path, _PORT, ("/synthetic",))


def test_gateway_timeout_terminates_only_the_started_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _published(tmp_path)

    class Process:
        stopped = False

        def poll(self) -> int | None:
            return 1 if self.stopped else None

        def terminate(self) -> None:
            self.stopped = True

        def wait(self, *, timeout: int | None = None) -> int:
            return 1

    process = Process()
    monkeypatch.setattr(desktop_gateway, "_probe", lambda *_: False)
    monkeypatch.setattr(desktop_gateway, "_port_available", lambda *_: True)
    monkeypatch.setattr(desktop_gateway.subprocess, "Popen", lambda *_a, **_kw: process)
    monkeypatch.setattr(desktop_gateway, "_START_TIMEOUT", 0.0)
    with pytest.raises(GatewaySettingsError, match="did not become ready"):
        desktop_gateway.ensure_gateway(tmp_path, _PORT, ("/synthetic",))
    assert process.stopped


def test_saved_gateway_change_aborts_before_start(tmp_path: Path) -> None:
    published = _published(tmp_path)
    replacement_token = GatewaySettingsStore(tmp_path).load(_PORT, "other-synthetic-binding")
    replacement = {
        **published,
        "env": {**published["env"], "ANTHROPIC_AUTH_TOKEN": replacement_token},
    }
    GatewaySettingsStore(tmp_path).publish(
        _PORT, "other-synthetic-binding", replacement_token, replacement
    )
    with pytest.raises(GatewaySettingsError, match="settings changed"):
        desktop_gateway.ensure_gateway(
            tmp_path,
            _PORT,
            ("/synthetic",),
            expected_token=published["env"]["ANTHROPIC_AUTH_TOKEN"],
        )


def test_managed_gateway_reports_state_and_reuses_a_matching_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = _published(tmp_path)
    token = published["env"]["ANTHROPIC_AUTH_TOKEN"]
    monkeypatch.setattr(desktop_gateway, "_probe", lambda *_: True)
    monkeypatch.setattr(desktop_gateway.subprocess, "Popen", lambda *_a, **_kw: pytest.fail())
    assert desktop_gateway.gateway_status(tmp_path, _PORT) == "running"
    snapshot = desktop_gateway.ensure_gateway(tmp_path, _PORT, ("/synthetic",))
    assert snapshot.settings == published
    with pytest.raises(GatewaySettingsError, match="changed"):
        desktop_gateway.ensure_gateway(tmp_path, _PORT, ("/synthetic",), expected_token=token + "x")


@pytest.mark.parametrize("partial", [False, True])
def test_managed_gateway_handles_missing_settings_and_occupied_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partial: bool
) -> None:
    if partial:
        GatewaySettingsStore(tmp_path).load(_PORT, "interrupted-startup")
    monkeypatch.setattr(desktop_gateway, "_port_available", lambda *_: True)
    assert desktop_gateway.gateway_status(tmp_path, _PORT) == "stopped"
    with pytest.raises(GatewaySettingsError, match="Start the gateway"):
        desktop_gateway.ensure_gateway(tmp_path, _PORT, None)
    monkeypatch.setattr(desktop_gateway, "_port_available", lambda *_: False)
    assert desktop_gateway.gateway_status(tmp_path, _PORT) == "unverified"
    with pytest.raises(GatewaySettingsError, match="occupied"):
        desktop_gateway.ensure_gateway(tmp_path, _PORT, ("/synthetic",))
    with pytest.raises(GatewaySettingsError, match="unverified"):
        desktop_gateway.stop_gateway(tmp_path, _PORT)
