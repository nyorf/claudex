import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing

from claudex.domain.gateway import UpstreamProtocolError
from claudex.domain.output import ResponseTranslation, apply_stop_sequences
from claudex.domain.translation import InferenceRequest, Payload
from claudex.observability.logging import log_cache_usage


class TranslatedInference:
    def __init__(self, request: InferenceRequest, upstream: AsyncIterator[Payload]) -> None:
        self._translation = ResponseTranslation(request)
        self._upstream = upstream
        self._stop_sequences = request.stop_sequences
        self._model: str = request.payload["model"]

    def message(self) -> Payload:
        return self._translation.message()

    async def events(self) -> AsyncGenerator[Payload]:
        async for upstream_event in self._upstream:
            events = self._translation.consume(upstream_event)
            if self._translation.cache_usage is not None:
                usage = self._translation.cache_usage
                log_cache_usage(
                    request_id=self._translation.message_id,
                    model=self._model,
                    cached_input_tokens=usage["cache_read_input_tokens"],
                    uncached_input_tokens=(
                        usage["input_tokens"] + usage["cache_creation_input_tokens"]
                    ),
                    cache_write_input_tokens=usage["cache_creation_input_tokens"],
                )
            for event in events:
                yield event
            if self._translation.complete:
                return
        raise UpstreamProtocolError("OpenAI closed the stream before the response finished.")

    async def collect(self) -> Payload:
        message = self.message()
        blocks = message["content"]
        arguments: dict[int, list[str]] = {}
        async with aclosing(self.events()) as events:
            async for event in events:
                match event["type"]:
                    case "content_block_start":
                        blocks.append(event["content_block"])
                    case "content_block_delta":
                        _append_delta(blocks, arguments, event)
                    case "message_delta":
                        message.update(event["delta"])
                        message["usage"] = event["usage"]
        for index, parts in arguments.items():
            blocks[index]["input"] = json.loads("".join(parts))
        apply_stop_sequences(message, self._stop_sequences)
        return message


def _append_delta(blocks: list[Payload], arguments: dict[int, list[str]], event: Payload) -> None:
    index, delta = event["index"], event["delta"]
    if delta["type"] == "input_json_delta":
        arguments.setdefault(index, []).append(delta["partial_json"])
    else:
        blocks[index]["text"] += delta["text"]
