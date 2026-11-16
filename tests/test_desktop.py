import json
import shlex
import stat
import subprocess  # nosec B404
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from claudex.infrastructure import desktop
from claudex.infrastructure.gateway_settings import GatewaySettingsError

_BEARER = "a" * 43
_COMMAND = ("/path with 'quotes'/claudex", "-I", "-m", "claudex")
_INSTALLED_COMMAND = (sys.executable, "-I", "-m", "claudex")
_OTHER_ID = "deec2ab0-57b6-4a8a-8cd4-78db84dbfebf"


@pytest.fixture(autouse=True)
def isolated_desktop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        desktop,
        "_app_metadata",
        lambda: {"CFBundleIdentifier": desktop._APP_ID, "CFBundleVersion": "5.11492.5"},
    )
    monkeypatch.setattr(desktop, "_desktop_running", lambda: False)
    managed = tmp_path / "managed"
    monkeypatch.setattr(
        desktop, "_managed_preferences", lambda: (managed / "device", managed / "user")
    )


def _support(home: Path) -> Path:
    return home / "Library" / "Application Support"


def _gateway_settings(bearer: str = _BEARER) -> dict[str, Any]:
    return {
        "env": {
            "ANTHROPIC_AUTH_TOKEN": bearer,
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:8765",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "872000",
        },
        "model": "claudex-opus",
        "availableModels": ["claudex-opus"],
        "modelSettings": {"claudex-opus": {"effortLevel": "high", "autoCompactWindow": 872_000}},
    }


def _configure(
    tmp_path: Path,
    *,
    bearer: str = _BEARER,
    export: bool = False,
    command: tuple[str, ...] = _COMMAND,
) -> bool | Path:
    settings = _gateway_settings(bearer)
    if export:
        settings["env"]["CLAUDE_CODE_EFFORT_LEVEL"] = "max"
    return desktop.configure_desktop(
        tmp_path,
        tmp_path / "state with spaces",
        8765,
        settings,
        command,
        export=export,
    )


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


@pytest.mark.parametrize("empty_library", [False, True])
def test_fresh_desktop_profile_is_activated_and_idempotent(
    tmp_path: Path, empty_library: bool
) -> None:
    root = _support(tmp_path) / "Claude-3p"
    meta_path = root / "configLibrary" / "_meta.json"
    mode_path = root / "claude_desktop_config.json"
    if empty_library:
        meta_path.parent.mkdir(parents=True)
        meta_path.write_text('{"appliedId":"","entries":[]}')
    assert _configure(tmp_path) is True
    meta = _json(meta_path)
    active = meta["appliedId"]
    assert meta["entries"] == [{"id": active, "name": "Claudex"}]
    assert _json(mode_path) == {"deploymentMode": "3p"}
    profile_path = meta_path.with_name(f"{active}.json")
    profile = _json(profile_path)
    helper = Path(profile["inferenceCredentialHelper"])
    assert profile == {
        "inferenceProvider": "gateway",
        "inferenceCredentialKind": "helper-script",
        "inferenceGatewayAuthScheme": "bearer",
        "inferenceCredentialHelper": str(helper),
        "inferenceGatewayBaseUrl": "http://127.0.0.1:8765",
        "modelDiscoveryEnabled": True,
        "defaultModelEffort": "high",
    }
    script = helper.read_text()
    assert _BEARER not in script + profile_path.read_text()
    assert script.splitlines()[0] == "#!/bin/sh"
    assert shlex.split(script.splitlines()[1]) == [
        "exec",
        *_COMMAND,
        "_desktop-credential",
        "--state-dir",
        str(tmp_path / "state with spaces"),
        "--port",
        "8765",
        "--fingerprint",
        sha256(_BEARER.encode()).hexdigest(),
    ]
    assert stat.S_IMODE(helper.stat().st_mode) == 0o700
    assert stat.S_IMODE(helper.parent.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600 for path in (meta_path, mode_path, profile_path)
    )
    assert _configure(tmp_path) is False
    assert _configure(tmp_path, bearer="b" * 43) is True
    assert _json(profile_path)["inferenceCredentialHelper"] != str(helper)
    assert helper.read_text() == script


def test_desktop_preserves_an_existing_profile_and_unrelated_fields(tmp_path: Path) -> None:
    root = _support(tmp_path) / "Claude-3p"
    library = root / "configLibrary"
    library.mkdir(parents=True)
    other = library / f"{_OTHER_ID}.json"
    other.write_text('{"existingProfile":true}\n')
    meta = {
        "entries": [{"id": _OTHER_ID, "name": "Claudex", "keep": True}],
        "appliedId": _OTHER_ID,
        "unrelated": {"retain": [1]},
    }
    meta_path = library / "_meta.json"
    meta_path.write_text(json.dumps(meta))
    mode_path = root / "claude_desktop_config.json"
    mode_path.write_text('{"deploymentMode":"1p","preferences":{"keep":true}}')

    assert _configure(tmp_path) is True
    assert other.read_text() == '{"existingProfile":true}\n'
    assert _json(mode_path) == {"deploymentMode": "3p", "preferences": {"keep": True}}
    applied = _json(meta_path)
    assert applied["unrelated"] == meta["unrelated"]
    assert applied["entries"] == [
        *meta["entries"],
        {
            "id": applied["appliedId"],
            "name": "Claudex (managed)",
        },
    ]
    assert applied["appliedId"] != _OTHER_ID
    assert _configure(tmp_path) is False


def test_desktop_preserves_claudex_profile_custom_preferences(tmp_path: Path) -> None:
    assert _configure(tmp_path) is True
    library = _support(tmp_path) / "Claude-3p" / "configLibrary"
    active = _json(library / "_meta.json")["appliedId"]
    profile_path = library / f"{active}.json"
    profile = _json(profile_path)
    profile["localPreferences"] = {"keep": True}
    profile_path.write_text(json.dumps(profile))
    assert _configure(tmp_path, bearer="b" * 43) is True
    assert _json(profile_path)["localPreferences"] == {"keep": True}


@pytest.mark.parametrize(
    "key", ["inferenceCustomHeaders", "inferenceGatewayOidc", "inferenceModels"]
)
def test_desktop_refuses_to_overwrite_incompatible_profile_edits(tmp_path: Path, key: str) -> None:
    assert _configure(tmp_path) is True
    library = _support(tmp_path) / "Claude-3p" / "configLibrary"
    active = _json(library / "_meta.json")["appliedId"]
    profile_path = library / f"{active}.json"
    profile = _json(profile_path)
    profile[key] = {"custom": "unchanged"}
    profile_path.write_text(json.dumps(profile))
    with pytest.raises(GatewaySettingsError, match="conflicting custom settings"):
        _configure(tmp_path, bearer="b" * 43)
    assert _json(profile_path) == profile


@pytest.mark.parametrize(
    ("relative_path", "body", "message"),
    [
        ("managed/device", "managed", "Managed preferences"),
        ("managed/user", "", "Managed preferences"),
        (
            "Library/Application Support/Claude/saved-org-lock.json",
            "{}",
            "organization lock",
        ),
        (
            "Library/Application Support/Claude-3p/saved-org-lock.json",
            "{}",
            "organization lock",
        ),
        (
            "Library/Application Support/Claude-3p/configLibrary/_meta.json",
            '{"entries":[],"hybridPointer":{"bootstrapUrl":"https://example.invalid"}}',
            "organization connection",
        ),
        (
            "Library/Application Support/Claude-3p/claude_desktop_config.json",
            '{"enterpriseConfig":{}}',
            "legacy enterprise configuration",
        ),
        (
            "Library/Application Support/Claude-3p/configLibrary/_meta.json",
            '{"entries":[{"id":"../invalid","name":"unsafe"}]}',
            "profile list is invalid",
        ),
    ],
)
def test_desktop_configuration_rejects_managed_or_invalid_state(
    tmp_path: Path, relative_path: str, body: str, message: str
) -> None:
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True)
    path.write_text(body)
    with pytest.raises(GatewaySettingsError, match=message):
        _configure(tmp_path)
    assert path.read_text() == body
    assert not list(tmp_path.rglob("credential-*.sh"))


@pytest.mark.parametrize(
    "relative_path",
    [
        "Library",
        "Library/Application Support/Claude-3p/configLibrary",
        "Library/Application Support/Claude-3p/configLibrary/_meta.json",
        "Library/Application Support/Claude-3p/claude_desktop_config.json",
        "state with spaces/desktop",
    ],
)
def test_desktop_configuration_rejects_symlinks(tmp_path: Path, relative_path: str) -> None:
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "untouched"
    target.mkdir()
    path.symlink_to(target, target_is_directory=True)
    with pytest.raises(GatewaySettingsError, match="regular"):
        _configure(tmp_path)
    assert list(target.iterdir()) == []


def test_desktop_rejects_writable_helper_ancestors(tmp_path: Path) -> None:
    state_dir = tmp_path / "state with spaces"
    state_dir.mkdir()
    state_dir.chmod(0o770)
    with pytest.raises(GatewaySettingsError, match="shared writes"):
        _configure(tmp_path)
    assert list(state_dir.iterdir()) == []
    assert not (tmp_path / "Library").exists()


def test_desktop_pins_helper_to_the_canonical_state_directory(tmp_path: Path) -> None:
    state_dir = tmp_path / "canonical"
    state_dir.mkdir()
    (tmp_path / "state with spaces").symlink_to(state_dir, target_is_directory=True)
    output = _configure(tmp_path, export=True)
    assert isinstance(output, Path)
    assert output.is_relative_to(state_dir.resolve())
    helper = Path(_json(output)["inferenceCredentialHelper"])
    assert helper.is_relative_to(state_dir.resolve())
    assert str(state_dir.resolve()) in shlex.split(helper.read_text().splitlines()[1])


def test_direct_configuration_requires_the_supported_app_to_be_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(desktop, "_desktop_running", lambda: True)
    with pytest.raises(GatewaySettingsError, match="Quit Claude Desktop"):
        _configure(tmp_path)
    monkeypatch.setattr(desktop, "_app_metadata", lambda: {})
    with pytest.raises(GatewaySettingsError, match="--export"):
        _configure(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_export_skips_the_app_and_only_writes_private_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected() -> None:
        pytest.fail("Desktop export inspected the app")

    monkeypatch.setattr(desktop, "_app_metadata", unexpected)
    output = _configure(tmp_path, export=True)
    assert isinstance(output, Path)
    exported = _json(output)
    assert exported["defaultModelEffort"] == "max"
    assert _BEARER not in output.read_text()
    assert Path(exported["inferenceCredentialHelper"]).parent == output.parent
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert _configure(tmp_path, export=True) == output
    assert not (tmp_path / "Library").exists()


@pytest.mark.parametrize(("fields", "expected"), [({"effortLevel": "low"}, "low"), ({}, "max")])
def test_desktop_uses_the_first_discovered_models_effort(
    tmp_path: Path, fields: dict[str, str], expected: str
) -> None:
    output = desktop.configure_desktop(
        tmp_path,
        tmp_path / "state",
        8765,
        {
            "env": {
                "ANTHROPIC_AUTH_TOKEN": _BEARER,
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:8765",
            },
            "model": "claudex-sonnet",
            "availableModels": ["claudex-opus", "claudex-sonnet"],
            "modelSettings": {"claudex-opus": fields, "claudex-sonnet": {"effortLevel": "high"}},
        },
        _COMMAND,
        export=True,
    )
    assert isinstance(output, Path)
    assert _json(output)["defaultModelEffort"] == expected


@pytest.mark.parametrize(
    ("global_window", "second_window", "effective"),
    [("872000", "auto", 872_000), ("875000", 272_000, 272_000), ("875000", "auto", 875_000)],
)
def test_desktop_launch_uses_app_scoped_context_and_compaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    global_window: str,
    second_window: int | str,
    effective: int,
) -> None:
    settings = _gateway_settings()
    settings["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = global_window
    settings["availableModels"].append("claudex-another")
    settings["modelSettings"]["claudex-opus"]["autoCompactWindow"] = "auto"
    settings["modelSettings"]["claudex-another"] = {"autoCompactWindow": second_window}
    state_dir = tmp_path / "state with spaces"
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_open(command: list[str], **kwargs: Any) -> None:
        calls.append((command, kwargs))

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", _BEARER)
    monkeypatch.setenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "512")
    monkeypatch.setattr(desktop.subprocess, "run", fake_open)
    assert desktop.configure_desktop(tmp_path, state_dir, 8765, settings, _INSTALLED_COMMAND)
    files = {path: path.stat().st_mtime_ns for path in tmp_path.rglob("*")}
    desktop.launch_desktop(tmp_path, state_dir, 8765, settings)
    assert {path: path.stat().st_mtime_ns for path in tmp_path.rglob("*")} == files
    assert calls == [
        (
            [
                "/usr/bin/open",
                "-a",
                "/Applications/Claude.app",
                "--env",
                f"CLAUDE_CODE_MAX_CONTEXT_TOKENS={effective}",
                "--env",
                f"CLAUDE_CODE_AUTO_COMPACT_WINDOW={effective}",
            ],
            {
                "capture_output": True,
                "check": True,
                "env": {},
                "stdin": subprocess.DEVNULL,
                "timeout": 10,
            },
        )
    ]
    assert _BEARER not in repr(calls)


@pytest.mark.parametrize("change", ["token", "port", "inactive", "tampered", "moved"])
def test_desktop_rejects_an_inactive_or_stale_gateway_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    command = _COMMAND if change == "moved" else _INSTALLED_COMMAND
    assert _configure(tmp_path, command=command)
    state_dir = tmp_path / "state with spaces"
    library = _support(tmp_path) / "Claude-3p" / "configLibrary"
    settings = _gateway_settings()
    port = 8765
    if change == "token":
        settings["env"]["ANTHROPIC_AUTH_TOKEN"] = "b" * 43
    elif change == "port":
        port = 8766
        settings["env"]["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8766"
    elif change == "inactive":
        path = library / "_meta.json"
        path.write_text(json.dumps({**_json(path), "appliedId": ""}))
    elif change == "tampered":
        profile = _json(library / f"{desktop._PROFILE_ID}.json")
        helper = Path(profile["inferenceCredentialHelper"])
        helper.write_text(helper.read_text() + "echo unexpected\n")

    monkeypatch.setattr(desktop, "_open_desktop", lambda _: pytest.fail("Unexpected launch"))
    with pytest.raises(GatewaySettingsError, match="Run claudex configure desktop"):
        desktop.launch_desktop(tmp_path, state_dir, port, settings)


@pytest.mark.parametrize(
    "window",
    [None, True, "872000", "bogus", 900_000, 50_000],
)
def test_configure_rejects_invalid_context_before_writing(tmp_path: Path, window: object) -> None:
    settings = _gateway_settings()
    settings["modelSettings"]["claudex-opus"]["autoCompactWindow"] = window
    with pytest.raises(GatewaySettingsError, match=r"[Cc]ontext"):
        desktop.configure_desktop(tmp_path, tmp_path / "state", 8765, settings, _COMMAND)
    assert list(tmp_path.iterdir()) == []


def test_desktop_refuses_running_app_and_scrubs_launch_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _gateway_settings()
    state_dir = tmp_path / "state with spaces"
    assert _configure(tmp_path, command=_INSTALLED_COMMAND)
    monkeypatch.setattr(desktop, "_desktop_running", lambda: True)
    with pytest.raises(GatewaySettingsError, match=r"Quit Claude Desktop.*claudex desktop"):
        desktop.launch_desktop(tmp_path, state_dir, 8765, settings)
    monkeypatch.setattr(desktop, "_desktop_running", lambda: False)

    def fail_open(command: list[str], **_kwargs: Any) -> None:
        raise subprocess.CalledProcessError(1, command, stderr=_BEARER)

    monkeypatch.setattr(desktop.subprocess, "run", fail_open)
    with pytest.raises(GatewaySettingsError, match="Could not open Claude Desktop") as failure:
        desktop.launch_desktop(tmp_path, state_dir, 8765, settings)
    assert _BEARER not in str(failure.value)


def test_explicit_models_preserve_labels_and_only_offer_supported_efforts(tmp_path: Path) -> None:
    settings = _gateway_settings()
    settings["availableModels"] = ["claudex-opus", "claudex-sonnet", "claudex-custom"]
    settings["modelPicker"] = {
        "options": [
            {"model": "claudex-custom", "label": "Custom"},
            {"model": "claudex-opus", "label": "Opus"},
            {"model": "claudex-sonnet", "label": "Sonnet"},
        ]
    }
    levels = {
        "claudex-opus": ("low", "medium", "high", "xhigh", "max"),
        "claudex-sonnet": ("low", "medium", "high"),
        "claudex-custom": ("low", "high"),
    }
    output = desktop.configure_desktop(
        tmp_path, tmp_path / "state", 8765, settings, _COMMAND, export=True, effort_levels=levels
    )
    assert isinstance(output, Path)
    profile = _json(output)
    assert profile["modelDiscoveryEnabled"] is False
    assert profile["inferenceModels"] == [
        {"name": "claudex-opus", "labelOverride": "Opus", "behavesAs": "claude-sonnet-5"},
        {
            "name": "claudex-sonnet",
            "labelOverride": "Sonnet",
            "behavesAs": "claude-sonnet-5",
            "maxEffort": "high",
        },
        {"name": "claudex-custom", "labelOverride": "Custom"},
    ]


def test_launch_migrates_discovery_and_detects_changed_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _gateway_settings()
    settings["modelPicker"] = {"options": [{"model": "claudex-opus", "label": "Opus"}]}
    state = tmp_path / "state with spaces"
    levels = {"claudex-opus": ("low", "medium", "high", "xhigh", "max")}
    assert desktop.configure_desktop(tmp_path, state, 8765, settings, _INSTALLED_COMMAND)
    monkeypatch.setattr(desktop, "_open_desktop", lambda _: None)
    with pytest.raises(desktop.DesktopProfileSetupError):
        desktop.launch_desktop(tmp_path, state, 8765, settings, effort_levels=levels)
    assert desktop.configure_desktop(
        tmp_path, state, 8765, settings, _INSTALLED_COMMAND, effort_levels=levels
    )
    root = _support(tmp_path) / "Claude-3p"
    receipt = root / desktop._MODEL_RECEIPT
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert not desktop.configure_desktop(
        tmp_path, state, 8765, settings, _INSTALLED_COMMAND, effort_levels=levels
    )
    desktop.launch_desktop(tmp_path, state, 8765, settings, effort_levels=levels)
    levels = {"claudex-opus": ("low", "medium", "high")}
    with pytest.raises(desktop.DesktopProfileSetupError):
        desktop.launch_desktop(tmp_path, state, 8765, settings, effort_levels=levels)
    assert desktop.configure_desktop(
        tmp_path, state, 8765, settings, _INSTALLED_COMMAND, effort_levels=levels
    )
    desktop.launch_desktop(tmp_path, state, 8765, settings, effort_levels=levels)


def test_generated_model_edits_are_not_overwritten(tmp_path: Path) -> None:
    settings = _gateway_settings()
    settings["modelPicker"] = {"options": [{"model": "claudex-opus", "label": "Opus"}]}
    state = tmp_path / "state"
    levels = {"claudex-opus": ("low", "medium", "high")}
    assert desktop.configure_desktop(
        tmp_path, state, 8765, settings, _COMMAND, effort_levels=levels
    )
    path = _support(tmp_path) / "Claude-3p" / "configLibrary" / f"{desktop._PROFILE_ID}.json"
    profile = _json(path)
    profile["inferenceModels"][0]["labelOverride"] = "Edited"
    path.write_text(json.dumps(profile))
    with pytest.raises(GatewaySettingsError, match="conflicting custom settings"):
        desktop.configure_desktop(tmp_path, state, 8765, settings, _COMMAND, effort_levels=levels)
    with pytest.raises(GatewaySettingsError, match="conflicting custom settings"):
        desktop.launch_desktop(tmp_path, state, 8765, settings, effort_levels=levels)
    assert _json(path) == profile


@pytest.mark.parametrize("fail_after_profile", [False, True])
def test_model_receipt_recovers_interrupted_profile_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_after_profile: bool
) -> None:
    settings = _gateway_settings()
    settings["modelPicker"] = {"options": [{"model": "claudex-opus", "label": "Opus"}]}
    state = tmp_path / "state"
    initial = {"claudex-opus": ("low", "medium", "high")}
    updated = {"claudex-opus": ("low", "medium", "high", "xhigh", "max")}
    assert desktop.configure_desktop(
        tmp_path, state, 8765, settings, _COMMAND, effort_levels=initial
    )
    root = _support(tmp_path) / "Claude-3p"
    profile_path = root / "configLibrary" / f"{desktop._PROFILE_ID}.json"
    write_json = desktop._write_private_json

    def interrupted(path: Path, data: desktop.Settings) -> bool:
        if path == profile_path:
            if fail_after_profile:
                write_json(path, data)
            raise OSError("interrupted")
        return write_json(path, data)

    with monkeypatch.context() as context:
        context.setattr(desktop, "_write_private_json", interrupted)
        with pytest.raises(GatewaySettingsError, match="could not access"):
            desktop.configure_desktop(
                tmp_path, state, 8765, settings, _COMMAND, effort_levels=updated
            )
    assert len(_json(root / desktop._MODEL_RECEIPT)["digests"]) == 2
    desktop.configure_desktop(tmp_path, state, 8765, settings, _COMMAND, effort_levels=updated)
    assert len(_json(root / desktop._MODEL_RECEIPT)["digests"]) == 1
    assert "maxEffort" not in _json(profile_path)["inferenceModels"][0]
