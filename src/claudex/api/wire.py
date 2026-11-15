from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from claudex.domain.translation import Payload


class OutputConfig(BaseModel):
    model_config = ConfigDict(strict=True)

    effort: str | None = None
    format: Payload | None = None


class IncomingMessage(BaseModel):
    model_config = ConfigDict(strict=True)

    role: Literal["assistant", "system", "user"]
    content: str | list[Payload] = Field(default_factory=list)
    output_config: OutputConfig | None = None
    clear_at: Literal["never", "next_user_message"] = "never"


class MessagesRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    model: Annotated[str, Field(min_length=1, max_length=128)]
    messages: Annotated[list[IncomingMessage], Field(min_length=1)]
    max_tokens: Annotated[int, Field(gt=0)]
    stream: bool = False
    system: str | list[Payload] | None = None
    output_config: OutputConfig | None = None
    output_format: Payload | None = None
    tools: list[Payload] | None = None
    tool_choice: Payload | None = None
    stop_sequences: list[str] | None = None
    context_management: Payload | None = None
    mcp_servers: list[Payload] | None = None
    container: str | Payload | None = None


class ModelInfo(BaseModel):
    type: Literal["model"] = "model"
    id: str
    display_name: str
    created_at: str = "1970-01-01T00:00:00Z"


class ModelList(BaseModel):
    data: list[ModelInfo]
    has_more: Literal[False] = False
    first_id: str | None
    last_id: str | None
