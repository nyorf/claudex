import json
import sys
from pathlib import Path
from typing import Any

import pytest

from claudex.config.launch import Launch
from claudex.domain.models import ModelRoute
from claudex.infrastructure.gateway_settings import GatewaySettingsError, GatewaySnapshot
from claudex.infrastructure.terminal import launch_terminal


def _snapshot() -> GatewaySnapshot:
    route = ModelRoute("gpt-synthetic", "Synthetic", 872_000, {"high": "high"})
    launch = Launch("claudex-opus", {"claudex-opus": route}, 872_000, "test")
    settings = launch.persistent_settings("http://127.0.0.1:8765", "test-token", "high", environ={})
    return GatewaySnapshot(settings, "test-binding", None)


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX executable fixture")
def test_terminal_executes_with_private_settings_and_gateway_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "claude"
    output = tmp_path / "captured.json"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, stat, sys\n"
        "from pathlib import Path\n"
        "path = Path(sys.argv[sys.argv.index('--settings') + 1])\n"
        "settings = json.loads(path.read_text())\n"
        "result = {'args': sys.argv[1:], 'settings': settings, "
        "'mode': stat.S_IMODE(path.stat().st_mode), "
        "'env': {key: os.environ.get(key) for key in "
        "['ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_DEFAULT_OPUS_MODEL', "
        "'CLAUDE_CODE_USE_VERTEX', 'NO_PROXY', 'no_proxy']}}\n"
        "Path(os.environ['CAPTURE_PATH']).write_text(json.dumps(result))\n"
        "raise SystemExit(23)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("CAPTURE_PATH", str(output))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "other-token")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "wrong-model")
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("NO_PROXY", "example.invalid")
    monkeypatch.delenv("no_proxy", raising=False)
    assert launch_terminal(str(executable), _snapshot(), ["--resume", "synthetic"]) == 23
    captured = json.loads(output.read_text(encoding="utf-8"))
    arguments = captured["args"]
    assert arguments[:2] == ["--model", "claudex-opus"]
    assert arguments[-4:] == ["--effort", "high", "--resume", "synthetic"]
    assert "test-token" not in " ".join(arguments)
    settings_path = Path(arguments[arguments.index("--settings") + 1])
    assert not settings_path.exists()
    assert captured["mode"] == 0o600
    assert captured["settings"]["modelSettings"]["claudex-opus"]["autoCompactWindow"] == 872_000
    assert captured["env"] == {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:8765",
        "ANTHROPIC_AUTH_TOKEN": "test-token",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "claudex-opus",
        "CLAUDE_CODE_USE_VERTEX": "0",
        "NO_PROXY": "example.invalid,127.0.0.1",
        "no_proxy": "example.invalid,127.0.0.1",
    }


def test_terminal_reports_missing_binary() -> None:
    with pytest.raises(GatewaySettingsError, match="Claude Code was not found"):
        launch_terminal("not-a-command", _snapshot(), [])


@pytest.mark.parametrize(
    "arguments",
    [
        ["--model", "claudex-sonnet", "--effort", "low", "-p", "hello"],
        ["--model=claudex-sonnet", "--effort=low", "--settings={}", "--bare"],
        ["--settings", '{"model":"claudex-sonnet","effortLevel":"low"}'],
        ["auth", "--help"],
        ["--", "--model", "literal prompt"],
    ],
)
def test_terminal_forwards_native_arguments(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    seen: list[str] = []

    def run(command: list[str], **kwargs: Any) -> Any:
        nonlocal seen
        seen = command[1:]
        assert kwargs["env"]["ANTHROPIC_AUTH_TOKEN"] == "test-token"
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("claudex.infrastructure.terminal.subprocess.run", run)
    assert launch_terminal("claude", _snapshot(), arguments) == 0
    assert seen[-len(arguments) :] == arguments
    defaults = seen[: -len(arguments)]
    visible = arguments[: arguments.index("--")] if "--" in arguments else arguments
    for flag in ("--model", "--effort", "--settings"):
        explicit = any(arg.split("=", 1)[0] == flag for arg in visible)
        custom_settings = any(arg.split("=", 1)[0] == "--settings" for arg in visible)
        assert (flag in defaults) == (not explicit and not custom_settings)


@pytest.mark.parametrize("invalid", [{}, {"model": "claudex-opus", "env": {}, "modelSettings": {}}])
def test_terminal_rejects_invalid_saved_settings(invalid: dict[str, Any]) -> None:
    with pytest.raises(GatewaySettingsError, match="settings are invalid"):
        launch_terminal("not-a-command", GatewaySnapshot(invalid, "binding", None), [])
