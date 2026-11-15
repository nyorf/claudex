import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import httpx2

from claudex.domain.accounts import TokenProvider
from claudex.domain.gateway import (
    InvalidMessagesRequestError,
    UpstreamProtocolError,
    UpstreamRejectedError,
    UpstreamUnavailableError,
)
from claudex.domain.translation import Payload

_RESPONSES_URL = "https://api.openai.com/v1/responses"
_MAX_ERROR_BYTES = 64 * 1024
_MAX_EVENT_BYTES = 16 * 1024 * 1024


class ResponsesClient:
    def __init__(self, client: httpx2.AsyncClient, token_provider: TokenProvider) -> None:
        self._client = client
        self._token_provider = token_provider

    @asynccontextmanager
    async def events(self, payload: Payload) -> AsyncIterator[AsyncIterator[Payload]]:
        token = await self._token_provider.access_token()
        async with self._stream(payload, token) as response:
            if response.status_code != 401:
                yield await _stream_events(response)
                return
        # only a pre-stream credential failure is safe to retry
        token = await self._token_provider.access_token(refresh_if=token)
        async with self._stream(payload, token) as response:
            yield await _stream_events(response)

    @asynccontextmanager
    async def _stream(self, payload: Payload, token: str) -> AsyncIterator[httpx2.Response]:
        try:
            request = self._client.build_request(
                "POST",
                _RESPONSES_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "text/event-stream",
                    "Accept-Encoding": "identity",
                },
                json=payload,
            )
        except ValueError:
            raise InvalidMessagesRequestError(
                "The request contains values invalid in JSON."
            ) from None
        try:
            response = await self._client.send(request, stream=True, follow_redirects=False)
        except httpx2.HTTPError:
            raise UpstreamUnavailableError("Could not reach the OpenAI Responses API.") from None
        try:
            yield response
        finally:
            with anyio.CancelScope(shield=True):
                await response.aclose()


async def _stream_events(response: httpx2.Response) -> AsyncIterator[Payload]:
    if not response.is_success:
        raise await _rejection(response)
    content_type = response.headers.get("content-type")
    if content_type is not None:
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "text/event-stream":
            raise UpstreamProtocolError(
                "OpenAI returned a response outside the streaming protocol."
            )
    encoding = response.headers.get("content-encoding", "identity").lower()
    if encoding != "identity":
        raise UpstreamProtocolError("OpenAI returned an unsupported stream encoding.")
    return _events(_body(response))


async def _rejection(response: httpx2.Response) -> UpstreamRejectedError:
    content = bytearray()
    try:
        async for chunk in _body(response):
            if len(content) + len(chunk) > _MAX_ERROR_BYTES:
                break
            content.extend(chunk)
    except httpx2.HTTPError:
        pass
    try:
        body = json.loads(content)
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    retry_after = response.headers.get("retry-after")
    if retry_after is not None and (not retry_after.isascii() or not retry_after.isdecimal()):
        retry_after = None
    return UpstreamRejectedError(
        response.status_code, code if isinstance(code, str) else None, retry_after
    )


def _body(response: httpx2.Response) -> httpx2.AsyncByteStream:
    # the HTTP iterator closes under cancellation before the shielded response owner can close it
    stream = response.stream
    if not isinstance(stream, httpx2.AsyncByteStream):
        raise UpstreamProtocolError("OpenAI returned a non-asynchronous stream.")
    return stream


async def _events(stream: httpx2.AsyncByteStream) -> AsyncIterator[Payload]:
    try:
        async for event in _read_events(stream):
            yield event
    except httpx2.HTTPError:
        raise UpstreamUnavailableError("The OpenAI Responses stream was interrupted.") from None


async def _read_events(stream: httpx2.AsyncByteStream) -> AsyncIterator[Payload]:
    lines: list[bytes] = []
    size = 0
    async for line in _lines(stream):
        if line.startswith(b"data:"):
            size += len(line) + 1
            if size > _MAX_EVENT_BYTES:
                raise UpstreamProtocolError("OpenAI returned an oversized streaming event.")
            lines.append(line[5:].removeprefix(b" "))
        elif not line and lines:
            event = _decode_event(lines)
            if event is None:
                return
            yield event
            lines = []
            size = 0


async def _lines(stream: httpx2.AsyncByteStream) -> AsyncIterator[bytes]:
    pending = bytearray()
    async for chunk in stream:
        lines = chunk.split(b"\n")
        for line in lines[:-1]:
            pending.extend(line)
            if len(pending) > _MAX_EVENT_BYTES:
                raise UpstreamProtocolError("OpenAI returned an oversized streaming line.")
            yield bytes(pending).removesuffix(b"\r")
            pending.clear()
        pending.extend(lines[-1])
        if len(pending) > _MAX_EVENT_BYTES:
            raise UpstreamProtocolError("OpenAI returned an oversized streaming line.")


def _decode_event(lines: list[bytes]) -> Payload | None:
    data = b"\n".join(lines)
    if data == b"[DONE]":
        return None
    try:
        event = json.loads(data)
    except ValueError:
        raise UpstreamProtocolError("OpenAI returned malformed streaming JSON.") from None
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise UpstreamProtocolError("OpenAI returned an invalid streaming event.")
    return event
