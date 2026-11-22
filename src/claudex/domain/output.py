import json
from dataclasses import dataclass, field
from operator import itemgetter
from typing import Any
from uuid import uuid4

from claudex.domain.gateway import UpstreamProtocolError, UpstreamRejectedError
from claudex.domain.translation import TOOL_CALL_ID, TOOL_NAMESPACE, InferenceRequest, Payload


@dataclass(slots=True)
class _Block:
    index: int
    kind: str
    parts: list[str] = field(default_factory=list)
    closed: bool = False


class ResponseTranslation:
    def __init__(self, request: InferenceRequest) -> None:
        self._request = request
        self._message_id = f"msg_{uuid4().hex}"
        self._blocks: dict[tuple[int, int], _Block] = {}
        self._calls: set[str] = set()
        self.complete = False
        self.cache_usage: Payload | None = None

    @property
    def message_id(self) -> str:
        return self._message_id

    def message(self) -> Payload:
        return {
            "id": self._message_id,
            "type": "message",
            "role": "assistant",
            "model": self._request.alias,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    def consume(self, event: Payload) -> list[Payload]:
        match event["type"]:
            case "response.output_text.delta" | "response.refusal.delta":
                return self._text_delta(event)
            case "response.content_part.done":
                return self._text_done(event)
            case "response.output_item.done":
                output_index = _index(event.get("output_index"))
                return self._item_done(output_index, _object(event.get("item")))
            case "response.completed" | "response.incomplete":
                return self._finish(_object(event.get("response")))
            case "response.failed":
                raise _failed(_object(event.get("response")).get("error"))
            case "error":
                raise _failed(event)
            case _:
                return []

    def _open(self, key: tuple[int, int], content: Payload, kind: str) -> Payload:
        index = len(self._blocks)
        self._blocks[key] = _Block(index, kind)
        return {"type": "content_block_start", "index": index, "content_block": content}

    def _delta(self, key: tuple[int, int], value: str) -> Payload:
        block = self._blocks[key]
        if block.closed:
            raise UpstreamProtocolError("OpenAI sent more content after a streaming block ended.")
        block.parts.append(value)
        delta = {"type": "text_delta", "text": value}
        if block.kind == "tool_use":
            delta = {"type": "input_json_delta", "partial_json": value}
        return {"type": "content_block_delta", "index": block.index, "delta": delta}

    def _close(self, key: tuple[int, int], complete_text: str) -> list[Payload]:
        block = self._blocks[key]
        if block.closed:
            return []
        partial = "".join(block.parts)
        if not complete_text.startswith(partial):
            raise UpstreamProtocolError(
                "OpenAI completed a block with different streaming content."
            )
        events = []
        if remainder := complete_text[len(partial) :]:
            events.append(self._delta(key, remainder))
        block.closed = True
        events.append({"type": "content_block_stop", "index": block.index})
        return events

    def _text_delta(self, event: Payload) -> list[Payload]:
        key = _content_key(event)
        events = self._open_text(key)
        events.append(self._delta(key, _string(event.get("delta"))))
        return events

    def _open_text(self, key: tuple[int, int]) -> list[Payload]:
        if key in self._blocks:
            return []
        return [self._open(key, {"type": "text", "text": ""}, "text")]

    def _text_done(self, event: Payload) -> list[Payload]:
        return self._text_part(_content_key(event), _object(event.get("part")))

    def _text_part(self, key: tuple[int, int], part: Payload) -> list[Payload]:
        match part.get("type"):
            case "output_text":
                text = _string(part.get("text"))
            case "refusal":
                text = _string(part.get("refusal"))
            case _:
                raise UpstreamProtocolError("OpenAI returned an unsupported message content type.")
        return [*self._open_text(key), *self._close(key, text)]

    def _item_done(self, index: int, item: Payload) -> list[Payload]:
        match item.get("type"):
            case "message":
                return [
                    event
                    for part_index, part in enumerate(_array(item.get("content")))
                    for event in self._text_part((index, part_index), _object(part))
                ]
            case "function_call":
                return self._tool_done((index, 0), item)
            case "reasoning":
                return []
            case _:
                raise UpstreamProtocolError("OpenAI returned an unsupported output item.")

    def _tool_done(self, key: tuple[int, int], item: Payload) -> list[Payload]:
        if key in self._blocks or item.get("status") == "incomplete":
            return []
        name = _string(item.get("name"))
        if name not in self._request.tools or item.get("namespace") not in (None, TOOL_NAMESPACE):
            raise UpstreamProtocolError(
                "OpenAI returned a tool outside the local tool definitions."
            )
        call_id = _string(item.get("call_id"))
        if not TOOL_CALL_ID.fullmatch(call_id) or call_id in self._calls:
            raise UpstreamProtocolError("OpenAI returned an invalid tool call ID.")
        raw = _string(item.get("arguments"))
        try:
            arguments = json.loads(raw)
            json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode()
        except ValueError:
            raise UpstreamProtocolError("OpenAI returned malformed tool arguments.") from None
        _object(arguments)
        self._calls.add(call_id)
        # a complete argument object is required before Claude can dispatch the local tool
        content = {"type": "tool_use", "id": call_id, "name": name, "input": {}}
        return [self._open(key, content, "tool_use"), *self._close(key, raw)]

    def _finish(self, response: Payload) -> list[Payload]:
        if response.get("status") not in ("completed", "incomplete"):
            raise UpstreamProtocolError("OpenAI ended its response in an unexpected state.")
        events = [
            event
            for index, item in enumerate(_array(response.get("output")))
            for event in self._item_done(index, _object(item))
        ]
        if any(not block.closed for block in self._blocks.values()):
            raise UpstreamProtocolError(
                "OpenAI ended its response before a content block finished."
            )
        stop_reason = _stop_reason(response, self._blocks)
        usage = _usage(response.get("usage"))
        details = _object(response["usage"].get("input_tokens_details") or {})
        if "cached_tokens" in details:
            self.cache_usage = usage
        self.complete = True
        events.extend(
            [
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": usage,
                },
                {"type": "message_stop"},
            ]
        )
        return events


def apply_stop_sequences(message: Payload, sequences: tuple[str, ...]) -> None:
    if not sequences or message["stop_reason"] != "end_turn":
        return
    blocks = message["content"]
    text = "".join(block["text"] for block in blocks)
    matches = ((text.find(sequence), sequence) for sequence in sequences)
    earliest = min((match for match in matches if match[0] >= 0), key=itemgetter(0), default=None)
    if earliest is None:
        return
    remaining, sequence = earliest
    clipped = []
    for block in blocks:
        if remaining == 0:
            break
        clipped.append({**block, "text": block["text"][:remaining]})
        remaining -= min(remaining, len(block["text"]))
    message.update(content=clipped, stop_reason="stop_sequence", stop_sequence=sequence)


def _stop_reason(response: Payload, blocks: dict[tuple[int, int], _Block]) -> str:
    if response.get("status") == "incomplete":
        reason = _object(response.get("incomplete_details")).get("reason")
        if reason == "max_output_tokens":
            return "max_tokens"
        if reason == "content_filter":
            return "refusal"
        raise UpstreamProtocolError("OpenAI did not complete the response.")
    if any(block.kind == "tool_use" for block in blocks.values()):
        return "tool_use"
    if any(
        part.get("type") == "refusal"
        for item in response["output"]
        if item.get("type") == "message"
        for part in item["content"]
    ):
        return "refusal"
    return "end_turn"


def _usage(value: Any) -> Payload:
    usage = _object(value)
    input_tokens = _index(usage.get("input_tokens"))
    output_tokens = _index(usage.get("output_tokens"))
    details = usage.get("input_tokens_details") or {}
    cached = _index(_object(details).get("cached_tokens", 0))
    written = _index(details.get("cache_write_tokens", 0))
    if cached + written > input_tokens:
        raise UpstreamProtocolError("OpenAI returned inconsistent token usage.")
    return {
        "input_tokens": input_tokens - cached - written,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": written,
    }


def _failed(value: Any) -> UpstreamProtocolError | UpstreamRejectedError:
    code = value.get("code") if isinstance(value, dict) else None
    if code in ("context_length_exceeded", "subscription_sharing_unsupported_capability"):
        return UpstreamRejectedError(400, code)
    if code in ("rate_limit_exceeded", "subscription_sharing_usage_limit_exceeded"):
        return UpstreamRejectedError(429, code)
    return UpstreamProtocolError("OpenAI reported a failure during generation.")


def _content_key(event: Payload) -> tuple[int, int]:
    return _index(event.get("output_index")), _index(event.get("content_index"))


def _index(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise UpstreamProtocolError("OpenAI returned an invalid index or token count.")
    return value


def _object(value: Any) -> Payload:
    if not isinstance(value, dict):
        raise UpstreamProtocolError("OpenAI returned an invalid JSON object.")
    return value


def _array(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise UpstreamProtocolError("OpenAI returned an invalid JSON array.")
    return value


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise UpstreamProtocolError("OpenAI returned an invalid string.")
    try:
        value.encode()
    except UnicodeError:
        raise UpstreamProtocolError("OpenAI returned invalid Unicode content.") from None
    return value
