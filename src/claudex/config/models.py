from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from claudex.domain.models import ModelRoute, resolve_route

_RouteName = Annotated[str, Field(pattern=r"^claudex-[a-z0-9]+(?:-[a-z0-9]+)*$")]
_Alias = Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")]
_ProfileName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,47}$")]
_ClaudeEffort = Literal["low", "medium", "high", "xhigh", "max"]
_GptEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
_Window = Annotated[int, Field(ge=1, le=2_000_000, strict=True)]


class ModelConfigError(Exception):
    pass


class _UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict:
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ConstructorError(
                    None, None, "Expected unique string keys", key_node.start_mark
                )
            result[key] = self.construct_object(value_node, deep=deep)
        return result


class _Route(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$")
    aliases: list[_Alias] = Field(default_factory=list)
    model: _Alias
    context_window: _Window
    context_window_overrides: dict[_ProfileName, _Window] = Field(default_factory=dict)
    efforts: dict[_ClaudeEffort, _GptEffort] = Field(min_length=1)
    claude_default_effort: _ClaudeEffort = "high"
    default_effort: _GptEffort | None = None


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: _RouteName
    routes: dict[_RouteName, _Route] = Field(min_length=1)


@dataclass(frozen=True)
class ModelMap:
    default: str
    routes: dict[str, ModelRoute]
    aliases: dict[str, str]
    context_window_overrides: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def families(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for family in ("opus", "sonnet", "haiku", "fable"):
            target = self.aliases.get(family, f"claudex-{family}")
            if target in self.routes:
                result[family] = target
        return result

    def resolve(self, name: str | None) -> str:
        if name is None:
            return self.default
        target = resolve_route(name, self.routes, self.aliases, self.families)
        if target is not None:
            return target
        raise ModelConfigError("Unknown model. Run claudex models to see the configured routes.")


def default_models_yaml() -> str:
    return files(__package__).joinpath("models.yaml").read_text(encoding="utf-8")


def load_model_map(path: Path, *, required: bool = False) -> ModelMap:
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        if required:
            raise ModelConfigError("The requested model map does not exist.") from exc
        source = default_models_yaml()
    except (OSError, UnicodeError) as exc:
        raise ModelConfigError("Cannot read the model map.") from exc

    loader = _UniqueLoader(source)
    try:
        config = _Config.model_validate(loader.get_single_data())
    except (yaml.YAMLError, ValidationError) as exc:
        raise ModelConfigError(
            "Invalid model map. Compare it with claudex models --example."
        ) from exc
    finally:
        loader.dispose()

    if config.default not in config.routes:
        raise ModelConfigError("The default model must name a route.")
    aliases: dict[str, str] = {}
    routes: dict[str, ModelRoute] = {}
    for name, route in config.routes.items():
        if "claude-" in name or "anthropic" in name:
            raise ModelConfigError("Use a custom route ID without an embedded Claude model ID.")
        if route.claude_default_effort not in route.efforts:
            raise ModelConfigError("Every route needs a mapping for its default Claude effort.")
        for alias in route.aliases:
            if alias in aliases or alias in config.routes:
                raise ModelConfigError("Model aliases must be unique across routes.")
            aliases[alias] = name
        efforts: dict[str, str] = dict(route.efforts.items())
        routes[name] = ModelRoute(
            model=route.model,
            label=route.label if route.label == route.model else f"{route.label} ({route.model})",
            context_window=route.context_window,
            efforts=efforts,
            default_effort=route.default_effort,
            claude_default_effort=route.claude_default_effort,
        )
    overrides = {
        name: route.context_window_overrides
        for name, route in config.routes.items()
        if route.context_window_overrides
    }
    return ModelMap(config.default, routes, aliases, overrides)
