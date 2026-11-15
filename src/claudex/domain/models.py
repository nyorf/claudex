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
