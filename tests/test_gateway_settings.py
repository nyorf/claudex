import asyncio
import json
import os
import socket
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx2
import pytest

from claudex.config.launch import plan_launch
from claudex.config.models import load_model_map
from claudex.infrastructure import gateway_settings
from claudex.infrastructure.catalog import CatalogModel
from claudex.infrastructure.gateway_settings import (
    GatewayLaunch,
    GatewaySettingsError,
    GatewaySettingsStore,
    configure_claude,
    unconfigure_claude,
)
from claudex.main import _gateway_binding, serve_gateway


def _settings(port: int, token: str) -> dict[str, Any]:
    return {
        "model": "claudex-opus",
        "env": {
            "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
        },
        "modelSettings": {"claudex-opus": {"effortLevel": "high"}},
    }


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def test_gateway_bearer_is_private_stable_and_port_specific(tmp_path: Path) -> None:
    store = GatewaySettingsStore(tmp_path)
    token = store.load(8765, "registration-a")
    with pytest.raises(GatewaySettingsError, match="Start Claudex"):
        store.settings(8765)
    store.publish(8765, "registration-a", token, _settings(8765, token))
    assert store.settings(8765) == _settings(8765, token)
    reopened = GatewaySettingsStore(tmp_path)
    assert reopened.load(8765, "registration-a") == token
    assert reopened.settings(8765) == _settings(8765, token)
    assert reopened.load(8766, "registration-a") != token
    assert stat.S_IMODE((tmp_path / "gateways").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "gateways" / "8765.json").stat().st_mode) == 0o600


def test_binding_change_invalidates_old_credential_and_published_settings(tmp_path: Path) -> None:
    store = GatewaySettingsStore(tmp_path)
    first = store.load(8765, "registration-a")
    store.publish(8765, "registration-a", first, _settings(8765, first))
    second = store.load(8765, "registration-b")
    assert second != first
    with pytest.raises(GatewaySettingsError, match="Start Claudex"):
        store.settings(8765)
    with pytest.raises(GatewaySettingsError, match="no longer matches"):
        store.publish(8765, "registration-a", first, _settings(8765, first))
    with pytest.raises(GatewaySettingsError, match="settings do not match"):
        store.publish(8765, "registration-b", second, _settings(8765, first))
    store.publish(8765, "registration-b", second, _settings(8765, second))
    assert store.load(8765, "registration-a") not in (first, second)


def test_same_binding_loads_are_serialized(tmp_path: Path) -> None:
    def load() -> str:
        return GatewaySettingsStore(tmp_path).load(8765, "registration-a")

    with ThreadPoolExecutor(max_workers=2) as executor:
        tokens = [executor.submit(load) for _ in range(2)]
        assert tokens[0].result() == tokens[1].result()


def test_only_one_proxy_owns_a_state_directory(tmp_path: Path) -> None:
    store = GatewaySettingsStore(tmp_path)
    with (
        store.running_instance(),
        pytest.raises(GatewaySettingsError, match="already starting or running"),
        GatewaySettingsStore(tmp_path).running_instance(),
    ):
        pytest.fail("A second proxy claimed the same state directory.")
    with store.running_instance():
        assert (tmp_path / "gateways").is_dir()


def test_restart_preserves_the_saved_launch_and_cannot_rotate_the_bearer(tmp_path: Path) -> None:
    store = GatewaySettingsStore(tmp_path)
    token = store.load(8765, "binding-a")
    launch = GatewayLaunch("work", "claudex-opus", "high", tmp_path / "custom.yaml", True)
    settings = _settings(8765, token)
    store.publish(8765, "binding-a", token, settings, launch)
    with pytest.raises(GatewaySettingsError, match="The saved gateway changed"):
        store.load(8765, "binding-b", reuse_only=True)
    reopened = GatewaySettingsStore(tmp_path)
    assert reopened.load(8765, "binding-a", reuse_only=True) == token
    reopened.publish(8765, "binding-a", token, settings)
    assert reopened.snapshot(8765).launch == launch
    assert reopened.settings(8765) == settings


def test_gateway_effort_levels_round_trip_and_legacy_fallback(tmp_path: Path) -> None:
    store = GatewaySettingsStore(tmp_path)
    token = store.load(8765, "binding-a")
    settings = {**_settings(8765, token), "availableModels": ["claudex-opus", "claudex-sonnet"]}
    levels = {"claudex-opus": ("low", "max"), "claudex-sonnet": ("high",)}
    store.publish(8765, "binding-a", token, settings, effort_levels=levels)
    record = _read(tmp_path / "gateways" / "8765.json")
    assert record["effort_levels"] == {"claudex-opus": ["low", "max"], "claudex-sonnet": ["high"]}
    assert "effort_levels" not in record["settings"]
    assert store.snapshot(8765).effort_levels == levels
    store.publish(8765, "binding-a", token, settings)
    assert store.snapshot(8765).effort_levels is None


def test_desktop_mappings_round_trip_and_reject_corrupt_records(tmp_path: Path) -> None:
    store = GatewaySettingsStore(tmp_path)
    token = store.load(8765, "binding-a")
    settings = {**_settings(8765, token), "availableModels": ["claudex-opus", "claudex-sonnet"]}
    models = {"claudex-opus": "claude-opus-5-5", "claudex-sonnet": "claude-sonnet-5-5"}
    families = {"opus": "claudex-opus", "sonnet": "claudex-sonnet"}
    store.publish(
        8765, "binding-a", token, settings, desktop_models=models, desktop_families=families
    )
    snapshot = store.snapshot(8765)
    assert snapshot.desktop_models == models and snapshot.desktop_families == families
    path = tmp_path / "gateways" / "8765.json"
    record = _read(path)
    record["desktop_models"]["claudex-sonnet"] = "claude-opus-5-5"
    path.write_text(json.dumps(record))
    with pytest.raises(GatewaySettingsError, match="Desktop model mappings are invalid"):
        store.snapshot(8765)


def test_gateway_route_mappings_round_trip_and_reject_corrupt_records(tmp_path: Path) -> None:
    store = GatewaySettingsStore(tmp_path)
    token = store.load(8765, "binding-a")
    settings = {**_settings(8765, token), "availableModels": ["claudex-opus", "claudex-sonnet"]}
    aliases = {"claude-sonnet-4-6": "claudex-opus"}
    families = {"sonnet": "claudex-sonnet"}
    store.publish(
        8765, "binding-a", token, settings, route_aliases=aliases, route_families=families
    )
    assert store.snapshot(8765).route_aliases == aliases
    assert store.snapshot(8765).route_families == families
    path = tmp_path / "gateways" / "8765.json"
    record = _read(path)
    record["route_families"]["sonnet"] = "unavailable"
    path.write_text(json.dumps(record))
    with pytest.raises(GatewaySettingsError, match="gateway model mappings are invalid"):
        store.snapshot(8765)


def test_expanded_cli_allowlist_preserves_desktop_and_effort_metadata(tmp_path: Path) -> None:
    store = GatewaySettingsStore(tmp_path)
    token = store.load(8765, "binding-a")
    settings = {
        **_settings(8765, token),
        "availableModels": ["claudex-opus", "claudex-sonnet", "claude-opus", "claude-sonnet"],
        "modelPicker": {"options": [{"model": "claudex-opus"}, {"model": "claudex-sonnet"}]},
    }
    levels = {"claudex-opus": ("high",), "claudex-sonnet": ("low", "high")}
    models = {"claudex-opus": "claude-opus-5-5", "claudex-sonnet": "claude-sonnet-5-5"}
    families = {"opus": "claudex-opus", "sonnet": "claudex-sonnet"}
    store.publish(
        8765,
        "binding-a",
        token,
        settings,
        effort_levels=levels,
        desktop_models=models,
        desktop_families=families,
    )
    snapshot = store.snapshot(8765)
    assert snapshot.effort_levels == levels
    assert snapshot.desktop_models == models
    assert snapshot.desktop_families == families


async def test_saved_gateway_migrates_previous_binding_without_changing_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_map = load_model_map(tmp_path / "missing.yaml")
    catalog = {
        route.model: CatalogModel(route.model, 872_000) for route in model_map.routes.values()
    }
    launch = plan_launch(model_map, None, catalog)
    legacy = _gateway_binding("synthetic-registration", launch, "high", legacy=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    store = GatewaySettingsStore(tmp_path)
    token = store.load(port, legacy)
    store.publish(
        port,
        legacy,
        token,
        launch.persistent_settings(f"http://127.0.0.1:{port}", token, "high", environ={}),
    )

    class Provider:
        async def access_token(self, *, refresh_if: str | None = None) -> str:
            return "synthetic"

    async def verify_running(*_args: object) -> int:
        async with httpx2.AsyncClient(trust_env=False) as client:
            response = await client.get(
                f"http://127.0.0.1:{port}/v1/models", headers={"x-api-key": token}
            )
        assert response.status_code == 200
        assert store.snapshot(port).desktop_models == launch.desktop_models
        assert store.snapshot(port).desktop_families == launch.desktop_families
        return 0

    monkeypatch.setattr("claudex.main._wait_for_stop", verify_running)
    publishing = threading.Event()
    proceed = threading.Event()
    publish = GatewaySettingsStore.publish

    def wait_before_publish(self: GatewaySettingsStore, *args: Any, **kwargs: Any) -> None:
        publishing.set()
        assert proceed.wait(10)
        publish(self, *args, **kwargs)

    monkeypatch.setattr(GatewaySettingsStore, "publish", wait_before_publish)
    async with httpx2.AsyncClient() as client:
        task = asyncio.create_task(
            serve_gateway(
                client,
                Provider(),
                launch,
                "high",
                port=port,
                state_dir=tmp_path,
                binding="synthetic-registration",
                expected_binding=legacy,
            )
        )
        try:
            assert await asyncio.to_thread(publishing.wait, 5)
            async with httpx2.AsyncClient(trust_env=False, timeout=0.2) as probe:
                with pytest.raises(httpx2.TimeoutException):
                    await probe.get(
                        f"http://127.0.0.1:{port}/health/ready",
                        headers={"x-claudex-challenge": "a" * 64},
                    )
        finally:
            proceed.set()
        assert await task == 0
    snapshot = store.snapshot(port)
    assert snapshot.binding == _gateway_binding("synthetic-registration", launch, "high")
    assert snapshot.settings["env"]["ANTHROPIC_AUTH_TOKEN"] == token
    assert snapshot.desktop_models == launch.desktop_models


@pytest.mark.parametrize(
    "levels",
    [
        None,
        {},
        {"claudex-opus": []},
        {"claudex-opus": ["invalid"]},
        {"claudex-opus": ["high", "high"]},
        {"claudex-opus": "high"},
        {"claudex-opus": ["high"], "extra": ["high"]},
    ],
)
def test_gateway_rejects_corrupted_effort_levels(tmp_path: Path, levels: object) -> None:
    store = GatewaySettingsStore(tmp_path)
    token = store.load(8765, "binding-a")
    settings = {**_settings(8765, token), "availableModels": ["claudex-opus"]}
    store.publish(8765, "binding-a", token, settings)
    path = tmp_path / "gateways" / "8765.json"
    record = _read(path)
    record["effort_levels"] = levels
    path.write_text(json.dumps(record))
    with pytest.raises(GatewaySettingsError, match="effort levels are invalid.*serve"):
        store.snapshot(8765)


def test_configure_merges_and_restore_preserves_unrelated_edits(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    original = {
        "model": "sonnet",
        "env": {
            "ANTHROPIC_BASE_URL": "https://example.invalid",
            "OTHER_SETTING": "keep",
            "NO_PROXY": "claude.example.invalid, shared.example.invalid",
            "no_proxy": "other.example.invalid",
        },
        "modelSettings": {"claudex-opus": {"autoCompactWindow": 200_000}},
        "permissions": {"deny": ["Bash"]},
    }
    path.write_text(json.dumps(original))
    path.chmod(0o644)
    settings = _settings(8765, "synthetic-gateway-token")
    settings["env"]["NO_PROXY"] = "shared.example.invalid,127.0.0.1"
    assert configure_claude(tmp_path, settings)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / ".claudex-settings.json").stat().st_mode) == 0o600
    merged = _read(path)
    assert merged["env"]["OTHER_SETTING"] == "keep"
    assert (
        merged["env"]["NO_PROXY"]
        == merged["env"]["no_proxy"]
        == "claude.example.invalid,shared.example.invalid,other.example.invalid,127.0.0.1"
    )
    assert merged["modelSettings"]["claudex-opus"] == {
        "effortLevel": "high",
        "autoCompactWindow": 200_000,
    }
    merged["theme"] = "dark"
    merged["env"]["OTHER_SETTING"] = "edited"
    path.write_text(json.dumps(merged))
    assert not configure_claude(tmp_path, settings)
    assert unconfigure_claude(tmp_path)
    original["theme"] = "dark"
    original["env"]["OTHER_SETTING"] = "edited"
    assert _read(path) == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not unconfigure_claude(tmp_path)


def test_configure_preserves_proxy_wildcard_as_a_complete_value(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"env":{"no_proxy":"*"}}')
    settings = _settings(8765, "synthetic-gateway-token")
    settings["env"]["NO_PROXY"] = "127.0.0.1"
    assert configure_claude(tmp_path, settings)
    assert _read(path)["env"]["NO_PROXY"] == _read(path)["env"]["no_proxy"] == "*"
    assert unconfigure_claude(tmp_path)
    assert _read(path) == {"env": {"no_proxy": "*"}}


def test_configure_changes_ports_and_restores_the_original_settings(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"model":"sonnet"}')
    assert configure_claude(tmp_path, _settings(8765, "synthetic-token-one"))
    assert configure_claude(tmp_path, _settings(8766, "synthetic-token-two"))
    assert _read(path) == _settings(8766, "synthetic-token-two")
    assert unconfigure_claude(tmp_path)
    assert _read(path) == {"model": "sonnet"}


def test_matching_settings_become_private(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    settings = _settings(8765, "synthetic-gateway-token")
    path.write_text(json.dumps(settings))
    path.chmod(0o644)
    assert configure_claude(tmp_path, settings)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not configure_claude(tmp_path, settings)


def test_unconfigure_restores_model_preferences_changed_by_claude(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    original = {
        "model": "claudex-opus",
        "modelSettings": {"claudex-opus": {"effortLevel": "high", "autoCompactWindow": 100_000}},
        "autoCompactEnabled": False,
    }
    path.write_text(json.dumps(original))
    settings = _settings(8765, "synthetic-gateway-token")
    settings["modelSettings"]["claudex-opus"]["autoCompactWindow"] = "auto"
    settings["autoCompactEnabled"] = True
    assert configure_claude(tmp_path, settings)
    modified = _read(path)
    modified["model"] = "claudex-sonnet"
    modified["modelSettings"]["claudex-opus"] = {
        "effortLevel": "low",
        "autoCompactWindow": 200_000,
        "anotherProperty": True,
    }
    modified["autoCompactEnabled"] = False
    path.write_text(json.dumps(modified))
    assert unconfigure_claude(tmp_path)
    original["modelSettings"]["claudex-opus"]["anotherProperty"] = True
    assert _read(path) == original


def test_unconfigure_removes_only_settings_it_created(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    assert configure_claude(tmp_path, _settings(8765, "synthetic-token-one"))
    assert unconfigure_claude(tmp_path)
    assert not path.exists()
    assert configure_claude(tmp_path, _settings(8765, "synthetic-token-two"))
    modified = _read(path)
    modified["env"]["USER_SETTING"] = "user-value"
    path.write_text(json.dumps(modified))
    assert unconfigure_claude(tmp_path)
    assert _read(path) == {"env": {"USER_SETTING": "user-value"}}


def test_managed_field_conflict_preserves_settings_and_scrubs_values(tmp_path: Path) -> None:
    settings = _settings(8765, "synthetic-gateway-token")
    assert configure_claude(tmp_path, settings)
    path = tmp_path / "settings.json"
    conflict = _read(path)
    conflict["env"]["ANTHROPIC_AUTH_TOKEN"] = "synthetic-private-value"
    path.write_text(json.dumps(conflict))
    with pytest.raises(GatewaySettingsError, match="env.ANTHROPIC_AUTH_TOKEN") as result:
        unconfigure_claude(tmp_path)
    assert "synthetic" not in str(result.value)
    with pytest.raises(GatewaySettingsError, match="env.ANTHROPIC_AUTH_TOKEN"):
        configure_claude(tmp_path, settings)
    assert _read(path) == conflict


@pytest.mark.parametrize("malformed", ["{", "[]", '{"env":[]}', '{"a":1,"a":2}'])
def test_configure_refuses_malformed_settings(tmp_path: Path, malformed: str) -> None:
    path = tmp_path / "settings.json"
    path.write_text(malformed)
    with pytest.raises(GatewaySettingsError):
        configure_claude(tmp_path, _settings(8765, "synthetic-gateway-token"))
    assert path.read_text() == malformed


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo", "hardlink"])
def test_configure_refuses_unsafe_settings_target(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "settings.json"
    source = tmp_path / "original.json"
    source.write_text("{}")
    match kind:
        case "symlink":
            path.symlink_to(source)
        case "directory":
            path.mkdir()
        case "fifo":
            os.mkfifo(path)
        case "hardlink":
            path.hardlink_to(source)
    with pytest.raises(GatewaySettingsError):
        configure_claude(tmp_path, _settings(8765, "synthetic-gateway-token"))
    assert source.read_text() == "{}"


@pytest.mark.parametrize("failed_write", ["settings.json", ".claudex-settings.json"])
def test_configure_retries_after_interrupted_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_write: str,
) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"model":"sonnet"}')
    write = gateway_settings._write

    def interrupt(destination: Path, data: dict[str, Any]) -> None:
        if destination.name == failed_write and data.get("prior") is None:
            raise OSError
        write(destination, data)

    settings = _settings(8765, "synthetic-gateway-token")
    with monkeypatch.context() as context:
        context.setattr(gateway_settings, "_write", interrupt)
        with pytest.raises(GatewaySettingsError, match="access the settings files"):
            configure_claude(tmp_path, settings)
    assert configure_claude(tmp_path, settings) is (failed_write == "settings.json")
    assert _read(path) == settings
    assert unconfigure_claude(tmp_path)
    assert _read(path) == {"model": "sonnet"}
