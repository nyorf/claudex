from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from claudex.config.models import ModelConfigError, ModelMap
from claudex.domain.models import ModelRoute
from claudex.infrastructure.catalog import CatalogModel

_PROVIDER_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_DISABLE_1M_CONTEXT",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "DISABLE_AUTO_COMPACT",
    "DISABLE_COMPACT",
)
_OTHER_PROVIDER_SWITCHES = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_MANTLE",
)
_FAMILIES = ("opus", "sonnet", "haiku", "fable")
_PREFERRED_CONTEXT_WINDOW = 872_000
_MIN_COMPACTION_WINDOW = 100_000
_MAX_COMPACTION_WINDOW = 1_000_000
_LAUNCH_OPTIONS = {
    "--model",
    "--effort",
    "--settings",
    "--autocompact",
    "--fallback-model",
    "--bare",
    "--desktop",
    "--cloud",
    "--environment",
    "--teleport",
    "--remote",
    "--remote-control",
    "--rc",
    "--bg",
    "--background",
    "--spawn-background",
}
_EXTERNAL_COMMANDS = {
    "agents",
    "attach",
    "auth",
    "doctor",
    "gateway",
    "install",
    "kill",
    "logs",
    "mcp",
    "plugin",
    "remote-control",
    "respawn",
    "rm",
    "setup-token",
    "stop",
    "ultrareview",
    "update",
}


def _no_proxy(environ: Mapping[str, str]) -> str:
    return ",".join(filter(None, (environ.get("NO_PROXY"), environ.get("no_proxy"), "127.0.0.1")))


def gateway_environment(environ: Mapping[str, str], settings: Mapping[str, str]) -> dict[str, str]:
    env = {
        key: value
        for key, value in environ.items()
        if key not in _PROVIDER_ENV and not key.startswith("ANTHROPIC_DEFAULT_")
    }
    no_proxy = _no_proxy(env)
    env.update(settings)
    env.update(NO_PROXY=no_proxy, no_proxy=no_proxy)
    return env


def claude_arguments(model: str, effort: str, settings_path: Path, args: list[str]) -> list[str]:
    if args and args[0] in _EXTERNAL_COMMANDS:
        raise ModelConfigError("Use the Claude executable directly for administrative commands.")
    for arg in args:
        if arg == "--":
            break
        if arg.split("=", 1)[0] in _LAUNCH_OPTIONS:
            raise ModelConfigError(
                "Pass local session flags after the separator. "
                "Set the model and effort on claudex cli."
            )
    return ["--model", model, "--settings", str(settings_path), "--effort", effort, *args]


def _compaction_window(model: str, window: int, maximum: int) -> int | str:
    if _MIN_COMPACTION_WINDOW <= window <= _MAX_COMPACTION_WINDOW:
        return window
    if window == maximum:
        return "auto"
    raise ModelConfigError(
        f"Claude Code cannot set a {window:,}-token window for {model} with this model map. "
        "Use --map with a separate model map for that context window."
    )


@dataclass(frozen=True)
class Launch:
    model: str
    routes: dict[str, ModelRoute]
    context_window: int
    context_source: str

    @property
    def _maximum_window(self) -> int:
        return max(route.context_window for route in self.routes.values())

    def _model_settings(self) -> dict[str, dict[str, int | str]]:
        maximum = self._maximum_window
        settings: dict[str, dict[str, int | str]] = {}
        for model, route in self.routes.items():
            row = {"autoCompactWindow": _compaction_window(model, route.context_window, maximum)}
            if route.claude_default_effort != "max":
                row["effortLevel"] = route.claude_default_effort
            settings[model] = row
        return settings

    def settings(
        self, base_url: str, token: str, *, environ: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        no_proxy = _no_proxy(environ or {})
        return {
            "modelPicker": {
                "replaceBuiltInOptions": True,
                "options": [
                    {"model": model, "label": route.label} for model, route in self.routes.items()
                ],
            },
            "modelOverrides": {},
            "fallbackModel": [],
            "autoCompactEnabled": True,
            "modelSettings": self._model_settings(),
            "availableModels": list(self.routes),
            "enforceAvailableModels": True,
            "env": {
                **self._session_env(base_url),
                "ANTHROPIC_AUTH_TOKEN": token,
                "NO_PROXY": no_proxy,
                "no_proxy": no_proxy,
            },
        }

    def persistent_settings(
        self, base_url: str, token: str, effort: str | None, *, environ: Mapping[str, str]
    ) -> dict[str, Any]:
        settings = self.settings(base_url, token, environ=environ)
        settings["model"] = self.model
        selected_effort = self.effective_effort(effort)
        if selected_effort == "max":
            settings["env"]["CLAUDE_CODE_EFFORT_LEVEL"] = "max"
            settings["modelSettings"][self.model].pop("effortLevel", None)
        else:
            settings["modelSettings"][self.model]["effortLevel"] = selected_effort
        return settings

    def _session_env(self, base_url: str) -> dict[str, str]:
        env: dict[str, str] = dict.fromkeys(_OTHER_PROVIDER_SWITCHES, "0")
        env.update(
            ANTHROPIC_BASE_URL=base_url,
            ANTHROPIC_API_KEY="",
            ANTHROPIC_CUSTOM_HEADERS="",
            ANTHROPIC_MODEL=self.model,
            ANTHROPIC_DEFAULT_MODEL=self.model,
            # clear an inherited login token
            CLAUDE_CODE_OAUTH_TOKEN="",  # nosec B106
            CLAUDE_CODE_MAX_CONTEXT_TOKENS=str(self._maximum_window),
            CLAUDE_CODE_AUTO_COMPACT_WINDOW="",
            CLAUDE_CODE_DISABLE_1M_CONTEXT="0",
            CLAUDE_CODE_EFFORT_LEVEL="",
            CLAUDE_CODE_AUTO_MODE_SERVER="0",
            DISABLE_COMPACT="0",
            DISABLE_AUTO_COMPACT="0",
            DISABLE_COST_WARNINGS="1",
            CLAUDE_CODE_ATTRIBUTION_HEADER="0",
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY="0",
            ENABLE_TOOL_SEARCH="false",
            ENABLE_CLAUDEAI_MCP_SERVERS="false",
        )
        for family in _FAMILIES:
            alias = f"claudex-{family}"
            env[f"ANTHROPIC_DEFAULT_{family.upper()}_MODEL"] = (
                alias if alias in self.routes else self.model
            )
        return env

    def environment(self, environ: dict[str, str], base_url: str, token: str) -> dict[str, str]:
        settings = self._session_env(base_url)
        settings["ANTHROPIC_AUTH_TOKEN"] = token
        return gateway_environment(environ, settings)

    def arguments(self, settings_path: Path, effort: str | None, args: list[str]) -> list[str]:
        return claude_arguments(self.model, self.effective_effort(effort), settings_path, args)

    def effective_effort(self, effort: str | None) -> str:
        route = self.routes[self.model]
        selected = effort or route.claude_default_effort
        if selected not in route.efforts:
            raise ModelConfigError("This model route has no mapping for the requested effort.")
        return selected


def _context(
    alias: str, route: ModelRoute, model: CatalogModel, override: int | None
) -> tuple[int, str]:
    if override is None:
        if (
            max(model.context_window or 0, model.max_context_window or 0)
            >= _PREFERRED_CONTEXT_WINDOW
        ):
            return _PREFERRED_CONTEXT_WINDOW, "Claudex default window"
        if model.context_window is not None:
            return model.context_window, "ChatGPT model catalog"
        return route.context_window, "configured window"

    maximum = model.max_context_window
    if maximum is None or (model.context_window is not None and maximum < model.context_window):
        raise ModelConfigError(
            f"The ChatGPT catalog has no valid maximum context window for {alias}. "
            "Remove its profile override from the model map."
        )
    if override > maximum:
        raise ModelConfigError(
            f"The {override:,}-token profile override for {alias} exceeds the "
            f"ChatGPT catalog maximum of {maximum:,}. Lower or remove it in the model map."
        )
    return override, "profile override"


def plan_launch(
    model_map: ModelMap,
    name: str | None,
    catalog: dict[str, CatalogModel],
    *,
    profile: str | None = None,
) -> Launch:
    selected = model_map.resolve(name)
    route = model_map.routes[selected]
    if route.model not in catalog:
        raise ModelConfigError(
            f"The selected ChatGPT profile does not list {route.model}. "
            "Run claudex models --available to check available models."
        )
    contexts = {
        alias: _context(
            alias,
            candidate,
            catalog[candidate.model],
            model_map.context_window_overrides.get(alias, {}).get(profile) if profile else None,
        )
        for alias, candidate in model_map.routes.items()
        if candidate.model in catalog
    }
    routes = {
        alias: replace(model_map.routes[alias], context_window=window)
        for alias, (window, _) in contexts.items()
    }
    maximum = max(candidate.context_window for candidate in routes.values())
    for alias, candidate in routes.items():
        _compaction_window(alias, candidate.context_window, maximum)
    window, source = contexts[selected]
    return Launch(selected, routes, window, source)
