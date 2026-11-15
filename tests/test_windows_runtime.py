import asyncio
import json
import os
import signal
import subprocess  # nosec B404
import sys
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest

from claudex.config.launch import Launch
from claudex.config.models import load_model_map
from claudex.infrastructure.credentials import (
    SERVICE,
    CredentialStore,
    InvalidStoredCredentialsError,
    _os_keyring,
    _WindowsCredentialBackend,
)
from claudex.infrastructure.gateway_settings import (
    GatewaySettingsStore,
    configure_claude,
    unconfigure_claude,
)
from claudex.main import _local_gateway, serve_gateway


class FakeVault:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.fail_service: str | None = None

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        if service == self.fail_service:
            raise OSError("synthetic failure")
        self.values[service, username] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.values[service, username]


class FakeProvider:
    async def access_token(self, *, refresh_if: str | None = None) -> str:
        return "synthetic-token"


def test_windows_vault_chunks_large_values_and_cleans_old_chunks() -> None:
    vault = FakeVault()
    backend = _WindowsCredentialBackend(vault)
    backend.set_password("service", "user", "\U0001f680" * 3500)
    assert backend.get_password("service", "user") == "\U0001f680" * 3500
    assert all(len(value.encode("utf-16-le")) <= 2560 for value in vault.values.values())
    manifest = (backend._manifest_service("service", "user"), "user")
    old = set(vault.values) - {manifest}
    backend.set_password("service", "user", "refreshed" * 1000)
    assert not old.intersection(vault.values)
    assert backend.get_password("service", "user") == "refreshed" * 1000
    backend.delete_password("service", "user")
    assert not vault.values


def test_windows_vault_rolls_back_failed_manifest_and_reports_missing_chunks() -> None:
    vault = FakeVault()
    backend = _WindowsCredentialBackend(vault)
    backend.set_password("service", "user", "original" * 1000)
    before = dict(vault.values)
    vault.fail_service = backend._manifest_service("service", "user")
    with pytest.raises(OSError):
        backend.set_password("service", "user", "replacement" * 1000)
    assert vault.values == before
    assert backend.get_password("service", "user") == "original" * 1000
    chunk = next(key for key in vault.values if key[0] != vault.fail_service)
    del vault.values[chunk]
    with pytest.raises(InvalidStoredCredentialsError, match="incomplete"):
        backend.get_password("service", "user")


def test_windows_vault_removes_credentials_with_a_missing_chunk() -> None:
    vault = FakeVault()
    backend = _WindowsCredentialBackend(vault)
    backend.set_password(SERVICE, "profile", "synthetic" * 1000)
    manifest = backend._manifest_service(SERVICE, "profile")
    chunk = next(key for key in vault.values if key[0] != manifest)
    del vault.values[chunk]
    CredentialStore(backend).delete("profile")
    assert not vault.values


def test_windows_vault_keeps_profiles_separate() -> None:
    vault = FakeVault()
    backend = _WindowsCredentialBackend(vault)
    backend.set_password(SERVICE, "first", "first-token")
    backend.set_password(SERVICE, "second", "second-token")
    backend.delete_password(SERVICE, "first")
    assert backend.get_password(SERVICE, "second") == "second-token"
    backend.delete_password(SERVICE, "second")
    assert not vault.values


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows Credential Manager")
def test_native_windows_credential_manager_round_trip() -> None:
    backend = _os_keyring()
    service = "org.claudex.synthetic-test." + uuid4().hex
    value = "synthetic" * 1000
    try:
        backend.set_password(service, "test", value)
        assert backend.get_password(service, "test") == value
        backend.set_password(service, "test", value + "refresh")
        assert backend.get_password(service, "test") == value + "refresh"
    finally:
        if backend.get_password(service, "test") is not None:
            backend.delete_password(service, "test")


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows filesystem behavior")
def test_native_windows_settings_persist_and_restore(tmp_path: Path) -> None:
    gateway = GatewaySettingsStore(tmp_path / "state")
    token = gateway.load(8765, "synthetic-profile")
    settings = {
        "env": {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:8765",
            "ANTHROPIC_AUTH_TOKEN": token,
        }
    }
    gateway.publish(8765, "synthetic-profile", token, settings)
    assert GatewaySettingsStore(tmp_path / "state").load(8765, "synthetic-profile") == token
    assert gateway.settings(8765) == settings
    config = tmp_path / "claude"
    assert configure_claude(config, settings)
    assert json.loads((config / "settings.json").read_text())["env"] == settings["env"]
    powershell = (
        "$a = Get-Acl -LiteralPath $env:CLAUDEX_TEST_SETTINGS; "
        "$ids = @($a.Access | ForEach-Object { "
        "$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value }); "
        "@{ protected = $a.AreAccessRulesProtected; ids = $ids; "
        "current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value } "
        "| ConvertTo-Json -Compress"
    )
    result = subprocess.run(  # noqa: S603  # nosec B603
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", powershell],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "CLAUDEX_TEST_SETTINGS": str(config / "settings.json")},
    )
    acl = json.loads(result.stdout)
    assert acl["protected"]
    assert set(acl["ids"]) == {"S-1-5-18", "S-1-5-32-544", acl["current"]}
    assert unconfigure_claude(config)


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows CLI behavior")
def test_native_windows_cli_imports_through_entry_point() -> None:
    result = subprocess.run(  # nosec B603
        [sys.executable, "-m", "claudex", "--help"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "NO_COLOR": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert "configure" in result.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows socket and signal behavior")
def test_native_windows_gateway_socket_and_graceful_stop(tmp_path: Path) -> None:
    model_map = load_model_map(tmp_path / "missing.yaml")
    route = model_map.routes[model_map.default]
    launch = Launch(model_map.default, model_map.routes, route.context_window, "synthetic")

    async def exercise() -> None:
        async with httpx2.AsyncClient(trust_env=False) as client:
            async with _local_gateway(client, FakeProvider(), launch, 0) as gateway:
                response = await client.get(f"{gateway.url}/v1/models")
                assert response.status_code == 401
                response = await client.get(
                    f"{gateway.url}/v1/models", headers={"x-api-key": gateway.token}
                )
                assert response.status_code == 200
            loop = asyncio.get_running_loop()
            loop.call_later(1, signal.raise_signal, signal.SIGTERM)
            assert await serve_gateway(client, FakeProvider(), launch, port=0) == 0

    asyncio.run(exercise())
