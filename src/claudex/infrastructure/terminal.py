import json
import os
import subprocess  # nosec B404
import sys
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from claudex.config.launch import gateway_environment
from claudex.infrastructure.gateway_settings import GatewaySettingsError, GatewaySnapshot

_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


def _selection(snapshot: GatewaySnapshot) -> tuple[str, str, dict[str, str]]:
    settings = snapshot.settings
    model = settings.get("model")
    env = settings.get("env")
    models = settings.get("modelSettings")
    if (
        not isinstance(model, str)
        or not model
        or not isinstance(env, dict)
        or not all(isinstance(key, str) and isinstance(value, str) for key, value in env.items())
        or not isinstance(models, dict)
        or not isinstance(models.get(model), dict)
    ):
        raise GatewaySettingsError("The saved gateway settings are invalid. Restart Claudex.")
    effort = env.get("CLAUDE_CODE_EFFORT_LEVEL") or models[model].get("effortLevel") or "max"
    if not isinstance(effort, str) or effort not in _EFFORTS:
        raise GatewaySettingsError("The saved gateway effort is invalid. Restart Claudex.")
    return model, effort, env


def _write_settings(directory: Path, settings: dict[str, Any]) -> Path:
    if sys.platform == "win32":
        from claudex.infrastructure.windows_security import make_private

        make_private(directory)
    path = directory / "settings.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if sys.platform == "win32":
            make_private(path)
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            descriptor = -1
            json.dump(settings, destination)
    finally:
        if descriptor != -1:
            os.close(descriptor)
    return path


def _arguments(model: str, effort: str, settings: Path, args: list[str]) -> list[str]:
    options = set()
    for arg in args:
        if arg == "--":
            break
        options.add(arg.split("=", 1)[0])
    defaults = []
    if "--model" not in options and "--settings" not in options:
        defaults.extend(("--model", model))
    if "--settings" not in options:
        defaults.extend(("--settings", str(settings)))
    if "--effort" not in options and "--settings" not in options:
        defaults.extend(("--effort", effort))
    return [*defaults, *args]


def launch_terminal(claude_bin: str, snapshot: GatewaySnapshot, args: list[str]) -> int:
    model, effort, settings_env = _selection(snapshot)
    env = gateway_environment(os.environ, settings_env)
    settings = deepcopy(snapshot.settings)
    settings["env"]["NO_PROXY"] = env["NO_PROXY"]
    settings["env"]["no_proxy"] = env["no_proxy"]
    with TemporaryDirectory(prefix="claudex-cli-") as temporary:
        settings_path = _write_settings(Path(temporary), settings)
        arguments = _arguments(model, effort, settings_path, args)
        return launch_native(claude_bin, arguments, env=env)


def launch_native(claude_bin: str, args: list[str], *, env: dict[str, str] | None = None) -> int:
    try:
        completed = subprocess.run(  # noqa: S603  # nosec B603
            [claude_bin, *args], env=env, check=False
        )
    except FileNotFoundError:
        raise GatewaySettingsError(
            "Claude Code was not found. Install it or set CLAUDEX_CLAUDE_BIN."
        ) from None
    return 128 - completed.returncode if completed.returncode < 0 else completed.returncode
