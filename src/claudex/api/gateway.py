import hmac
import json
import re
import secrets
import time
from collections.abc import Callable
from contextlib import aclosing
from secrets import compare_digest
from typing import Annotated
from uuid import UUID

import anyio
import httpx2
from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.requests import ClientDisconnect
from starlette.types import Receive, Scope, Send

from claudex.api.errors import ErrorResponse, gateway_error
from claudex.api.wire import MessagesRequest, ModelInfo, ModelList
from claudex.domain.accounts import AccountError, TokenProvider
from claudex.domain.gateway import GatewayError, InvalidMessagesRequestError
from claudex.domain.models import ModelRoute
from claudex.domain.readiness import readiness_proof
from claudex.domain.translation import InferenceRequest, Payload, prepare_request
from claudex.infrastructure.responses import ResponsesClient
from claudex.services.inference import TranslatedInference

_MAX_BODY_BYTES = 32 * 1024 * 1024
_HEARTBEAT_SECONDS = 15
_READINESS_CHALLENGE = re.compile(r"[a-f0-9]{64}\Z")
_SESSION_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)


class _UnauthorizedError(Exception):
    pass


class _BodyTooLargeError(Exception):
    pass


class _Shutdown:
    def __init__(self, token: bytes, stop: Callable[[], None] | None) -> None:
        self._token = token
        self._stop = stop
        self._challenges: dict[str, float] = {}

    async def challenge(self) -> JSONResponse:
        now = time.monotonic()
        for nonce, expires in list(self._challenges.items()):
            if expires <= now:
                del self._challenges[nonce]
        if len(self._challenges) >= 128:
            return JSONResponse({"error": "Too many pending requests."}, status_code=429)
        nonce = secrets.token_hex(32)
        self._challenges[nonce] = now + 15
        return JSONResponse(
            {"challenge": nonce, "proof": readiness_proof(self._token, nonce)},
            headers={"Cache-Control": "no-store"},
        )

    async def shutdown(self, request: Request) -> Response:
        challenges = request.headers.getlist("x-claudex-challenge")
        proofs = request.headers.getlist("x-claudex-proof")
        if (
            self._stop is None
            or len(challenges) != 1
            or _READINESS_CHALLENGE.fullmatch(challenges[0]) is None
            or len(proofs) != 1
            or _READINESS_CHALLENGE.fullmatch(proofs[0]) is None
        ):
            raise _UnauthorizedError
        challenge = challenges[0]
        expiry = self._challenges.pop(challenge, 0)
        proof = hmac.digest(
            self._token, b"claudex-stop-v1:" + challenge.encode("ascii"), "sha256"
        ).hex()
        if expiry <= time.monotonic() or not compare_digest(proofs[0], proof):
            raise _UnauthorizedError
        self._stop()
        return Response(status_code=204, headers={"Cache-Control": "no-store"})


def create_gateway(
    routes: dict[str, ModelRoute],
    *,
    client: httpx2.AsyncClient,
    token_provider: TokenProvider,
    gateway_token: str,
    stop: Callable[[], None] | None = None,
    aliases: dict[str, str] | None = None,
    families: dict[str, str] | None = None,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_exception_handler(ValidationError, _validation_error)
    app.add_exception_handler(_BodyTooLargeError, _body_size_error)
    app.add_exception_handler(_UnauthorizedError, _auth_error)
    app.add_exception_handler(ClientDisconnect, _client_disconnect)
    router = APIRouter()
    upstream = ResponsesClient(client, token_provider)
    route_snapshot = dict(routes)
    alias_snapshot = dict(aliases or {})
    family_snapshot = dict(families or {})
    expected_token = gateway_token.encode("utf-8")
    shutdown = _Shutdown(expected_token, stop)
    router.add_api_route("/control/challenge", shutdown.challenge, methods=["GET"])
    router.add_api_route("/control/stop", shutdown.shutdown, methods=["POST"])

    async def authenticated(request: Request) -> None:
        scheme, _, bearer = request.headers.get("authorization", "").partition(" ")
        supplied = bearer if scheme.lower() == "bearer" else request.headers.get("x-api-key", "")
        if not supplied or not compare_digest(supplied.encode("utf-8"), expected_token):
            raise _UnauthorizedError

    @router.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/health/ready")
    async def ready(request: Request) -> JSONResponse:
        challenges = request.headers.getlist("x-claudex-challenge")
        headers = {"Cache-Control": "no-store"}
        if len(challenges) != 1 or _READINESS_CHALLENGE.fullmatch(challenges[0]) is None:
            return JSONResponse(
                {"error": "Invalid readiness request."}, status_code=400, headers=headers
            )
        return JSONResponse(
            {"proof": readiness_proof(expected_token, challenges[0])}, headers=headers
        )

    @router.head("/api/hello", status_code=204)
    async def hello() -> Response:
        return Response(status_code=204)

    @router.get("/v1/models", dependencies=[Depends(authenticated)])
    async def models() -> ModelList:
        data = [
            ModelInfo(id=alias, display_name=route.label) for alias, route in route_snapshot.items()
        ]
        return ModelList(
            data=data,
            first_id=data[0].id if data else None,
            last_id=data[-1].id if data else None,
        )

    @router.post("/v1/messages", response_model=None, dependencies=[Depends(authenticated)])
    async def messages(
        request: Request,
        body: Annotated[MessagesRequest, Depends(_messages_body)],
    ) -> Response:
        try:
            inference = prepare_request(
                body.model_dump(exclude_none=True), route_snapshot, alias_snapshot, family_snapshot
            )
        except InvalidMessagesRequestError as exc:
            return _error_json(gateway_error(exc))
        session_ids = request.headers.getlist("x-claude-code-session-id")
        session_id = None
        if len(session_ids) == 1 and _SESSION_ID.fullmatch(session_ids[0]):
            normalized = session_ids[0].lower().encode("ascii")
            raw = hmac.digest(expected_token, b"claudex-cache-session-v1:" + normalized, "sha256")
            session_id = str(UUID(bytes=raw[:16], version=4))
        return _InferenceResponse(upstream, inference, session_id)

    app.include_router(router)
    return app


async def _messages_body(request: Request) -> MessagesRequest:
    length = request.headers.get("content-length")
    if length is not None and length.isascii() and length.isdecimal():
        digits = length.lstrip("0") or "0"
        if len(digits) > 8 or int(digits) > _MAX_BODY_BYTES:
            raise _BodyTooLargeError
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > _MAX_BODY_BYTES:
            raise _BodyTooLargeError
        data.extend(chunk)
    return MessagesRequest.model_validate_json(data)


async def _validation_error(_request: Request, _exc: Exception) -> JSONResponse:
    return _error_json(
        ErrorResponse(400, "invalid_request_error", "Send a valid Claude Messages request body.")
    )


async def _body_size_error(_request: Request, _exc: Exception) -> JSONResponse:
    return _error_json(ErrorResponse(413, "request_too_large", "The request exceeds 32 MiB."))


async def _auth_error(_request: Request, _exc: Exception) -> JSONResponse:
    return _error_json(
        ErrorResponse(401, "authentication_error", "Supply this launch's Claudex gateway token.")
    )


async def _client_disconnect(_request: Request, _exc: Exception) -> Response:
    return Response(status_code=499)


def _error_json(error: ErrorResponse) -> JSONResponse:
    return JSONResponse(error.payload(), status_code=error.status, headers=error.headers())


def _sse(payload: Payload) -> bytes:
    return (
        f"event: {payload['type']}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()
    )


class _InferenceResponse(Response):
    def __init__(
        self, upstream: ResponsesClient, inference: InferenceRequest, session_id: str | None
    ) -> None:
        super().__init__()
        self._upstream = upstream
        self._inference = inference
        self._session_id = session_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async with anyio.create_task_group() as tasks:

            async def cancel_on_disconnect() -> None:
                while (await receive())["type"] != "http.disconnect":
                    pass
                tasks.cancel_scope.cancel()

            tasks.start_soon(cancel_on_disconnect)
            try:
                await self._respond(scope, receive, send)
            finally:
                tasks.cancel_scope.cancel()

    async def _respond(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            async with self._upstream.events(
                self._inference.payload, session_id=self._session_id
            ) as events:
                translated = TranslatedInference(self._inference, events)
                if self._inference.stream:
                    await _write_stream(translated, send)
                else:
                    await JSONResponse(await translated.collect())(scope, receive, send)
        except (GatewayError, InvalidMessagesRequestError, AccountError) as exc:
            await _error_json(gateway_error(exc))(scope, receive, send)


async def _write_stream(translated: TranslatedInference, send: Send) -> None:
    headers = [(b"content-type", b"text/event-stream"), (b"cache-control", b"no-cache")]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    lock = anyio.Lock()
    terminal: Payload = {"type": "message_stop"}

    async def write(payload: Payload) -> None:
        async with lock:
            await send({"type": "http.response.body", "body": _sse(payload), "more_body": True})

    async def keepalive() -> None:
        while True:
            await anyio.sleep(_HEARTBEAT_SECONDS)
            await write({"type": "ping"})

    await write({"type": "message_start", "message": translated.message()})
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(keepalive)
        try:
            async with aclosing(translated.events()) as events:
                async for event in events:
                    if event["type"] == "message_stop":
                        break
                    await write(event)
        except GatewayError as exc:
            terminal = gateway_error(exc).payload()
        finally:
            tasks.cancel_scope.cancel()
    await send({"type": "http.response.body", "body": _sse(terminal), "more_body": False})
