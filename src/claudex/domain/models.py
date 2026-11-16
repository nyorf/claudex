import re
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelRoute:
    model: str
    label: str
    context_window: int
    efforts: Mapping[str, str]
    default_effort: str | None = None
    claude_default_effort: str = "high"


_CLAUDE_FAMILY = re.compile(
    r"^(?:(?:us|us-gov|eu|ap|apac|jp|au|ca|sa|global)\.)?(?:anthropic\.)?"
    r"claude-(?:[0-9]+(?:[-.][0-9]+)*-)?"
    r"(opus|sonnet|haiku|fable)(?:[-.@_][0-9]+)*(?:[-.]v[0-9]+(?::[0-9]+)?)?$",
    re.IGNORECASE,
)


def model_family(name: str) -> str | None:
    if name.lower().endswith("[1m]"):
        name = name[:-4]
    family = name.lower() if name.lower() in ("opus", "sonnet", "haiku", "fable") else None
    if match := _CLAUDE_FAMILY.fullmatch(name):
        family = match.group(1).lower()
    return family


def resolve_route(
    name: str,
    routes: Mapping[str, ModelRoute],
    aliases: Mapping[str, str],
    families: Mapping[str, str],
) -> str | None:
    if name in routes:
        return name
    family = model_family(name)
    if family is not None and family in families:
        return families[family]
    target = aliases.get(name)
    return target if target in routes else None
