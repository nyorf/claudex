import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from claudex.domain.gateway import InvalidMessagesRequestError, UnknownModelError
from claudex.domain.models import ModelRoute, resolve_route

type Payload = dict[str, Any]
TOOL_NAMESPACE = "claude_code"
_TOOL_NAME = re.compile(r"[a-zA-Z0-9_-]{1,128}")
TOOL_CALL_ID = re.compile(r"[a-zA-Z0-9_-]{1,64}")
_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


@dataclass(frozen=True, slots=True)
class InferenceRequest:
    alias: str
    payload: Payload
    tools: frozenset[str]
    stream: bool
    stop_sequences: tuple[str, ...] = ()


def prepare_request(
    body: Payload,
    routes: Mapping[str, ModelRoute],
    aliases: Mapping[str, str] | None = None,
    families: Mapping[str, str] | None = None,
) -> InferenceRequest:
    alias = body["model"]
    target = resolve_route(alias, routes, aliases or {}, families or {})
    if target is None:
        raise UnknownModelError("Choose a model alias configured for this Claudex launch.")
    route = routes[target]
    if body.get("context_management") is not None:
        raise InvalidMessagesRequestError("context_management: Extra inputs are not permitted")
    if body.get("mcp_servers") or body.get("container"):
        raise InvalidMessagesRequestError(
            "Use MCP tools configured in Claude Code for this gateway."
        )
    stops = _stop_sequences(body)
    tools = _tools(body.get("tools") or [])
    messages = _messages(body["messages"], tools)
    if not messages:
        raise InvalidMessagesRequestError("Send at least one text, image, or tool result message.")
    instructions = _instructions(body.get("system"))
    if instructions:
        messages.insert(0, _message("system", [{"type": "input_text", "text": instructions}]))
    payload: Payload = {"model": route.model, "input": messages, "stream": True, "store": False}
    if tools:
        payload["tools"] = [
            {
                "type": "namespace",
                "name": TOOL_NAMESPACE,
                "description": "Tools executed by the local Claude Code session.",
                "tools": [tools[name] for name in sorted(tools)],
            }
        ]
    payload.update(_tool_choice(body.get("tool_choice"), tools))
    payload.update(_output_options(body, route))
    return InferenceRequest(alias, payload, frozenset(tools), body.get("stream", False), stops)


def _stop_sequences(body: Payload) -> tuple[str, ...]:
    sequences: list[str] | None = body.get("stop_sequences")
    if not sequences:
        return ()
    if any(not sequence for sequence in sequences):
        raise InvalidMessagesRequestError("Stop sequences must be nonempty strings.")
    if (
        body.get("stream")
        or body.get("tools")
        or body.get("tool_choice")
        or (body.get("output_config") or {}).get("format") is not None
        or body.get("output_format") is not None
        or not all(_plain_text(message) for message in body["messages"])
    ):
        raise InvalidMessagesRequestError(
            "Stop sequences are supported for non-streaming, tool-free text requests only."
        )
    return tuple(sequences)


def _plain_text(message: Payload) -> bool:
    if (message.get("output_config") or {}).get("format") is not None:
        return False
    blocks = message.get("content", [])
    return isinstance(blocks, str) or all(block.get("type") == "text" for block in blocks)


def _instructions(system: str | list[Payload] | None) -> str:
    if isinstance(system, str):
        return system
    return "\n\n".join(_text(block) for block in system or [])


def _text(block: Payload) -> str:
    if block.get("type") != "text" or not isinstance(block.get("text"), str):
        raise InvalidMessagesRequestError("A text content block must contain a string.")
    return block["text"]


def _messages(messages: list[Payload], tools: dict[str, Payload]) -> list[Payload]:
    items: list[Payload] = []
    calls: dict[str, str] = {}
    last_user = _last_user_index(messages)
    for index, message in enumerate(messages):
        role = message["role"]
        if _cleared_system(message, index, last_user):
            continue
        content = message.get("content", [])
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        pending: list[Payload] = []
        for block in blocks:
            item = _message_block(role, block, tools, calls)
            if item is None:
                continue
            if item["type"] in {"input_text", "input_image"}:
                pending.append(item)
                continue
            if pending:
                items.append(_message(role, pending))
                pending = []
            items.append(item)
        if pending:
            items.append(_message(role, pending))
    return items


def _last_user_index(messages: list[Payload]) -> int:
    return max((index for index, item in enumerate(messages) if item["role"] == "user"), default=-1)


def _cleared_system(message: Payload, index: int, last_user: int) -> bool:
    return (
        message["role"] == "system"
        and message.get("clear_at") == "next_user_message"
        and index < last_user
    )


def _message(role: str, content: list[Payload]) -> Payload:
    if role == "assistant":
        # the transcript preserves text without the upstream output item ID
        return {"role": "assistant", "content": "\n\n".join(block["text"] for block in content)}
    return {
        "type": "message",
        "role": "developer" if role == "system" else role,
        "content": content,
    }


def _message_block(
    role: str, block: Payload, tools: dict[str, Payload], calls: dict[str, str]
) -> Payload | None:
    match block.get("type"), role:
        case "text", _:
            return {"type": "input_text", "text": _text(block)}
        case "image", "user":
            return _image(block)
        case "tool_use", "assistant":
            return _tool_call(block, calls)
        case "tool_result", "user":
            return _tool_result(block, calls)
        case "thinking" | "redacted_thinking", "assistant":
            # reasoning signatures are bound to the original provider
            return None
        case "tool_addition" | "tool_removal", "system":
            _update_tools(block, tools)
            return None
        case _:
            raise InvalidMessagesRequestError("This message contains an unsupported content block.")


def _image(block: Payload) -> Payload:
    source = block.get("source")
    if not isinstance(source, dict):
        raise InvalidMessagesRequestError("An image block must contain an image source.")
    if source.get("type") == "base64":
        media_type, data = source.get("media_type"), source.get("data")
        if not isinstance(media_type, str) or media_type not in _IMAGE_TYPES:
            raise InvalidMessagesRequestError("Use a JPEG, PNG, GIF, or WebP image.")
        if isinstance(data, str) and data:
            return {"type": "input_image", "image_url": f"data:{media_type};base64,{data}"}
    if source.get("type") == "url" and isinstance(url := source.get("url"), str):
        try:
            parsed = urlsplit(url)
        except ValueError:
            parsed = None
        if (
            parsed is not None
            and parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
        ):
            return {"type": "input_image", "image_url": url}
    raise InvalidMessagesRequestError("Use a base64 image or an HTTPS image URL.")


def _checked_name(value: Any) -> str:
    if not isinstance(value, str) or not _TOOL_NAME.fullmatch(value):
        raise InvalidMessagesRequestError(
            "Tool names must use letters, numbers, underscores, or hyphens."
        )
    return value


def _checked_id(value: Any) -> str:
    if not isinstance(value, str) or not TOOL_CALL_ID.fullmatch(value):
        raise InvalidMessagesRequestError("A tool call or result has an invalid call ID.")
    return value


def _tool_call(block: Payload, calls: dict[str, str]) -> Payload:
    name = _checked_name(block.get("name"))
    call_id = _checked_id(block.get("id"))
    arguments = block.get("input")
    if not isinstance(arguments, dict) or call_id in calls:
        raise InvalidMessagesRequestError("A tool call needs an input object and a unique call ID.")
    calls[call_id] = name
    return {
        "type": "function_call",
        "name": name,
        "namespace": TOOL_NAMESPACE,
        "call_id": call_id,
        "arguments": _arguments_json(arguments),
    }


def _arguments_json(arguments: Payload) -> str:
    try:
        return json.dumps(arguments, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except ValueError:
        raise InvalidMessagesRequestError(
            "The tool arguments contain invalid JSON values."
        ) from None


def _tool_result(block: Payload, calls: dict[str, str]) -> Payload:
    call_id = _checked_id(block.get("tool_use_id"))
    if call_id not in calls:
        raise InvalidMessagesRequestError(
            "A tool result must follow its tool call in the transcript."
        )
    content = block.get("content", "")
    if block.get("is_error") is True:
        if isinstance(content, str):
            content = f"Tool execution failed.\n{content}"
        elif isinstance(content, list):
            content = [{"type": "text", "text": "Tool execution failed."}, *content]
    if isinstance(content, list):
        content = [_tool_result_block(item) for item in content] or ""
    if not isinstance(content, str | list):
        raise InvalidMessagesRequestError(
            "A tool result must contain text or a list of content blocks."
        )
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "name": calls[call_id],
        "namespace": TOOL_NAMESPACE,
        "output": content,
    }


def _tool_result_block(block: Any) -> Payload:
    if not isinstance(block, dict):
        raise InvalidMessagesRequestError("A tool result content block must be an object.")
    if block.get("type") == "image":
        return _image(block)
    return {"type": "input_text", "text": _text(block)}


def _tools(definitions: Iterable[Payload]) -> dict[str, Payload]:
    tools: dict[str, Payload] = {}
    for definition in definitions:
        tool = _tool(definition)
        if tool["name"] in tools:
            raise InvalidMessagesRequestError("Each tool must have a unique name.")
        tools[tool["name"]] = tool
    return tools


def _tool(definition: Payload) -> Payload:
    if definition.get("type", "custom") != "custom" or definition.get("defer_loading"):
        raise InvalidMessagesRequestError("Use eagerly loaded local tools with this gateway.")
    schema = definition.get("input_schema")
    if not isinstance(schema, dict):
        raise InvalidMessagesRequestError("A tool must include its input schema.")
    tool = {
        "type": "function",
        "name": _checked_name(definition.get("name")),
        "parameters": schema,
        "strict": definition.get("strict") is True,
    }
    if isinstance(description := definition.get("description"), str):
        tool["description"] = description
    return tool


def _update_tools(block: Payload, tools: dict[str, Payload]) -> None:
    reference = block.get("tool")
    if not isinstance(reference, dict):
        raise InvalidMessagesRequestError("A tool change must include a local tool.")
    if block["type"] == "tool_removal" and reference.get("type") == "tool_reference":
        tools.pop(_checked_name(reference.get("name")), None)
    elif block["type"] == "tool_addition" and isinstance(reference.get("definition"), dict):
        tool = _tool(reference["definition"])
        tools[tool["name"]] = tool
    elif block["type"] == "tool_addition" and _checked_name(reference.get("name")) in tools:
        return
    else:
        raise InvalidMessagesRequestError("Include the definition when adding a local tool.")


def _tool_choice(choice: Payload | None, tools: Mapping[str, Payload]) -> Payload:
    if not choice:
        return {}
    translated: Payload = {}
    match choice.get("type"):
        case "auto" | "none":
            translated["tool_choice"] = choice["type"]
        case "any" if tools:
            translated["tool_choice"] = "required"
        case "tool" if _checked_name(choice.get("name")) in tools:
            translated["tool_choice"] = {
                "type": "function",
                "name": f"{TOOL_NAMESPACE}.{choice['name']}",
            }
        case _:
            raise InvalidMessagesRequestError(
                "Choose an available local tool or use automatic selection."
            )
    if choice.get("disable_parallel_tool_use") is True:
        translated["parallel_tool_calls"] = False
    return translated


def _output_options(body: Payload, route: ModelRoute) -> Payload:
    config = body.get("output_config") or {}
    effort = config.get("effort")
    last_user = _last_user_index(body["messages"])
    for index, message in enumerate(body["messages"]):
        override = (message.get("output_config") or {}).get("effort")
        if override is not None and not _cleared_system(message, index, last_user):
            if message["role"] != "system":
                raise InvalidMessagesRequestError("Set per-turn effort on a system message.")
            effort = override
    options: Payload = {}
    upstream_effort = route.default_effort if effort is None else route.efforts.get(effort)
    if effort is not None and upstream_effort is None:
        raise InvalidMessagesRequestError(
            "Add an effort mapping for the selected Claude effort in YAML."
        )
    if upstream_effort is not None:
        options["reasoning"] = {"effort": upstream_effort}
    output_format = config.get("format") or body.get("output_format")
    if output_format:
        schema = output_format.get("schema")
        if output_format.get("type") != "json_schema" or not isinstance(schema, dict):
            raise InvalidMessagesRequestError("Structured output must use a JSON schema.")
        options["text"] = {
            "format": {
                "type": "json_schema",
                "name": "claude_output",
                "schema": schema,
                "strict": True,
            }
        }
    return options
