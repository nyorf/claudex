import json
import sys
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest

from claudex import cli
from claudex.domain.accounts import ReauthenticationRequiredError
from claudex.infrastructure.gateway_settings import (
    GatewayLaunch,
    GatewaySettingsError,
    GatewaySettingsStore,
    GatewaySnapshot,
)

_PORT = 18888


def _publish(
    directory: Path, binding: str, port: int = _PORT, *, with_efforts: bool = False
) -> tuple[str, dict[str, Any]]:
    store = GatewaySettingsStore(directory)
    token = store.load(port, binding)
    settings = {
        "model": "claudex-opus",
        "env": {
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
            "ANTHROPIC_AUTH_TOKEN": token,
        },
    }
    if with_efforts:
        settings["availableModels"] = ["claudex-opus"]
    store.publish(
        port,
        binding,
        token,
        settings,
        effort_levels={"claudex-opus": ("low", "high")} if with_efforts else None,
    )
    return token, settings


def _isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    state = tmp_path / "state folder"
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDEX_STATE_DIR", str(state))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("HOME", str(tmp_path))
    return state, config


def _invoke(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["claudex", *args])
    return cli.main()


def test_version_prints_repository_on_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COLUMNS", "26")
    with pytest.raises(SystemExit) as exit_info:
        _invoke(monkeypatch, "--version")
    assert exit_info.value.code == 0
    assert (
        capsys.readouterr().out
        == f"claudex {version('claudex')} (https://github.com/nyorf/claudex)\n"
    )


def test_old_configure_grammar_and_settings_are_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state, config = _isolated_paths(tmp_path, monkeypatch)
    token, settings = _publish(state, "profile-a")
    assert cli._parser().parse_args(["configure"]).target is None
    assert cli._parser().parse_args(["configure"]).port == cli.DEFAULT_PORT
    assert _invoke(monkeypatch, "configure", "--port", str(_PORT)) == 0
    assert json.loads((config / "settings.json").read_text(encoding="utf-8")) == settings
    assert _invoke(monkeypatch, "unconfigure") == 0
    assert not (config / "settings.json").exists()
    captured = capsys.readouterr()
    assert "Claude Code now uses Claudex." in captured.out
    assert token not in captured.out and token not in captured.err


@pytest.mark.parametrize(("export", "changed"), [(True, False), (False, True), (False, False)])
@pytest.mark.parametrize("with_efforts", [True, False])
def test_desktop_configuration_passes_an_absolute_helper_and_preserves_cli_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    export: bool,
    changed: bool,
    with_efforts: bool,
) -> None:
    state, config = _isolated_paths(tmp_path, monkeypatch)
    config.mkdir()
    source = config / "settings.json"
    original = '{"theme":"dark"}'
    source.write_text(original, encoding="utf-8")
    token, published = _publish(state, "profile-a", with_efforts=with_efforts)
    output_path = state / "desktop-profile.json"
    calls: list[str] = []

    def configure(
        home: Path,
        state_dir: Path,
        port: int,
        settings: dict[str, Any],
        helper_command: tuple[str, ...],
        *,
        export: bool = False,
        effort_levels: dict[str, tuple[str, ...]] | None = None,
    ) -> bool | Path:
        assert home == tmp_path and state_dir == state and port == _PORT
        assert settings == published
        assert effort_levels == ({"claudex-opus": ("low", "high")} if with_efforts else None)
        assert helper_command == (sys.executable, "-I", "-m", "claudex")
        assert Path(helper_command[0]).is_absolute()
        assert all(token not in part for part in helper_command)
        calls.append("export" if export else "configure")
        return output_path if export else changed

    def launch(
        home: Path,
        state_dir: Path,
        port: int,
        gateway_settings: dict[str, Any],
        *,
        effort_levels: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        assert home == tmp_path and state_dir == state and port == _PORT
        assert gateway_settings == published
        assert effort_levels == ({"claudex-opus": ("low", "high")} if with_efforts else None)
        calls.append("launch")

    monkeypatch.setattr(cli, "configure_desktop", configure)
    monkeypatch.setattr(cli, "launch_desktop", launch)
    monkeypatch.setattr(sys, "platform", "darwin")
    extra = ["--export"] if export else []
    assert _invoke(monkeypatch, "configure", "desktop", "--port", str(_PORT), *extra) == 0
    assert calls == (["export"] if export else ["configure", "launch"])
    assert source.read_text(encoding="utf-8") == original
    assert not (config / ".claudex-settings.json").exists()
    captured = capsys.readouterr()
    assert captured.out.strip() == (
        str(output_path)
        if export
        else "Opened Claude Desktop for Claudex. Start a session in the Code tab.\n"
        f"Open future sessions with claudex desktop --port {_PORT}."
    )
    assert token not in captured.out and token not in captured.err


def test_configure_export_requires_desktop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, config = _isolated_paths(tmp_path, monkeypatch)
    assert _invoke(monkeypatch, "configure", "--export") == 1
    assert "Use --export with claudex configure desktop." in capsys.readouterr().err
    assert not config.exists()


def test_desktop_configuration_refuses_linux_without_reading_gateway_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state, config = _isolated_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    assert _invoke(monkeypatch, "configure", "desktop") == 1
    assert "available on macOS" in capsys.readouterr().err
    assert not state.exists() and not config.exists()


@pytest.mark.parametrize("port", [cli.DEFAULT_PORT, _PORT])
@pytest.mark.parametrize("setup", [False, True])
@pytest.mark.parametrize("with_efforts", [True, False])
def test_desktop_launch_configures_a_new_profile_only_when_needed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    port: int,
    setup: bool,
    with_efforts: bool,
) -> None:
    state, config = _isolated_paths(tmp_path, monkeypatch)
    token, published = _publish(state, "profile-a", port, with_efforts=with_efforts)
    calls: list[str] = []

    def launch(
        home: Path,
        state_dir: Path,
        selected_port: int,
        gateway_settings: dict[str, Any],
        *,
        effort_levels: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        assert (home, state_dir, selected_port) == (tmp_path, state, port)
        assert gateway_settings == published
        assert effort_levels == ({"claudex-opus": ("low", "high")} if with_efforts else None)
        if setup and "configure" not in calls:
            raise cli.DesktopProfileSetupError("Desktop profile needs setup.")
        calls.append("open")

    def ensure(args: Any, settings: Any) -> GatewaySnapshot:
        assert (settings.state_dir, args.port) == (state, port)
        calls.append("gateway")
        return GatewaySettingsStore(state).snapshot(port)

    def configure(*args: object, **kwargs: object) -> bool:
        assert setup and args[:4] == (tmp_path, state, port, published)
        assert kwargs["effort_levels"] == (
            {"claudex-opus": ("low", "high")} if with_efforts else None
        )
        calls.append("configure")
        return True

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cli, "launch_desktop", launch)
    monkeypatch.setattr(cli, "_acquire_proxy", ensure)
    monkeypatch.setattr(cli, "gateway_status", lambda *_: "stopped")
    monkeypatch.setattr(cli, "configure_desktop", configure)
    options = ["--port", str(port)] if port != cli.DEFAULT_PORT else []
    assert _invoke(monkeypatch, "desktop", *options) == 0
    assert calls == (["gateway", "configure", "open"] if setup else ["gateway", "open"])
    assert not config.exists()
    captured = capsys.readouterr()
    assert "Opened Claude Desktop" in captured.out
    assert token not in captured.out and token not in captured.err


def test_cli_passes_native_options_and_consumes_only_proxy_options() -> None:
    native = ["--model", "opus", "--effort=high", "-p", "test", "--output-format", "json"]
    args = cli._parse_args(["cli", "--proxy-profile", "personal", *native])
    assert args.claude_args == native
    assert args.profile == "personal" and args.model is None and args.effort is None


def test_first_cli_launch_prompts_and_selects_a_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state, _ = _isolated_paths(tmp_path, monkeypatch)
    token, published = _publish(state, "binding-a", _PORT)
    actions = []

    class Accounts:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def selected(self) -> None:
            return None

        async def profiles(self) -> list[str]:
            return []

        def provider(self, profile: str) -> None:
            assert profile == "personal"
            raise ReauthenticationRequiredError("Unknown profile.")

        async def login(self, profile: str) -> None:
            actions.append(("login", profile))

        async def select(self, profile: str) -> None:
            actions.append(("select", profile))

    def ensure(*args: Any, **kwargs: Any) -> GatewaySnapshot:
        assert args[:2] == (state, _PORT)
        assert args[2][-2:] == ("--profile", "personal")
        recipe = GatewayLaunch("personal", "claudex-opus", "high", state / "models.yaml", False)
        return GatewaySnapshot(published, "binding-a", recipe)

    def launch(_binary: str, snapshot: GatewaySnapshot, args: list[str]) -> int:
        assert snapshot.settings == published and args == ["-p", "hello"]
        return 0

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "")
    monkeypatch.setattr(cli, "AccountManager", Accounts)
    monkeypatch.setattr(cli, "gateway_status", lambda *_: "stopped")
    monkeypatch.setattr(cli, "ensure_gateway", ensure)
    monkeypatch.setattr(cli, "launch_terminal", launch)
    assert _invoke(monkeypatch, "cli", "--proxy-port", str(_PORT), "-p", "hello") == 0
    assert actions == [("login", "personal"), ("select", "personal")]
    assert token not in capsys.readouterr().err


def test_desktop_configuration_reports_launch_failure_after_saving_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state, _ = _isolated_paths(tmp_path, monkeypatch)
    token, _ = _publish(state, "profile-a", cli.DEFAULT_PORT)

    def configure(*args: object, **kwargs: object) -> bool:
        return True

    def launch(*args: object, **kwargs: object) -> None:
        raise GatewaySettingsError("Claudex could not open Claude Desktop.")

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cli, "configure_desktop", configure)
    monkeypatch.setattr(cli, "launch_desktop", launch)
    assert _invoke(monkeypatch, "configure", "desktop") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Saved the Claude Desktop profile." in captured.err
    assert "could not open Claude Desktop" in captured.err
    assert "Retry with claudex desktop." in captured.err
    assert token not in captured.err


def test_desktop_credential_helper_fails_closed_after_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state, _ = _isolated_paths(tmp_path, monkeypatch)
    first, _ = _publish(state, "profile-a")
    fingerprint = sha256(first.encode("ascii")).hexdigest()
    args = [
        "_desktop-credential",
        "--state-dir",
        str(state),
        "--port",
        str(_PORT),
        "--fingerprint",
        fingerprint,
    ]
    monkeypatch.setenv("CLAUDEX_STATE_DIR", "ignored by the helper")
    assert _invoke(monkeypatch, *args) == 0
    captured = capsys.readouterr()
    assert captured.out == f"{first}\n" and not captured.err

    second, _ = _publish(state, "profile-b")
    assert _invoke(monkeypatch, *args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Configure Claude Desktop again." in captured.err
    assert all(value not in captured.err for value in (first, second, fingerprint))


def test_desktop_helper_diagnostics_do_not_repeat_arguments(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe = "synthetic-sensitive-input"
    assert _invoke(monkeypatch, "_desktop-credential", probe) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and probe not in captured.err


def test_desktop_helper_ignores_stale_nonloopback_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state, _ = _isolated_paths(tmp_path, monkeypatch)
    token, _ = _publish(state, "profile-a")
    record = state / "gateways" / f"{_PORT}.json"
    published = json.loads(record.read_text(encoding="utf-8"))
    published["settings"]["env"]["ANTHROPIC_BASE_URL"] = "https://example.invalid"
    record.write_text(json.dumps(published), encoding="utf-8")
    result = _invoke(
        monkeypatch,
        "_desktop-credential",
        "--state-dir",
        str(state),
        "--port",
        str(_PORT),
        "--fingerprint",
        sha256(token.encode("ascii")).hexdigest(),
    )
    assert result == 1
    captured = capsys.readouterr()
    assert captured.out == "" and token not in captured.err
