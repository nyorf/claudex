import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing

from claudex.domain.gateway import UpstreamProtocolError
from claudex.domain.output import ResponseTranslation, apply_stop_sequences
from claudex.domain.translation import InferenceRequest, Payload


class TranslatedInference:
    def __init__(self, request: InferenceRequest, upstream: AsyncIterator[Payload]) -> None:
        self._translation = ResponseTranslation(request)
        self._upstream = upstream
        self._stop_sequences = request.stop_sequences

    def message(self) -> Payload:
        return self._translation.message()

    async def events(self) -> AsyncGenerator[Payload]:
        async for upstream_event in self._upstream:
            for event in self._translation.consume(upstream_event):
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
