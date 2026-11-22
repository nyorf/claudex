import hmac
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import anyio
import httpx2
import pytest
from starlette.types import Message

from claudex.api.gateway import create_gateway
from claudex.config.launch import plan_launch
from claudex.config.models import load_model_map
from claudex.domain.accounts import AccountError
from claudex.domain.models import ModelRoute
from claudex.infrastructure.catalog import CatalogModel

type Payload = dict[str, Any]
_GATEWAY_TOKEN = "synthetic-gateway-secret"
_AUTH = {"Authorization": f"Bearer {_GATEWAY_TOKEN}"}
_ROUTE = ModelRoute("gpt-test", "Claude alias to GPT", 475000, {"high": "high", "low": "low"})


class Tokens:
    def __init__(self) -> None:
        self.refreshes: list[str | None] = []

    async def access_token(self, *, refresh_if: str | None = None) -> str:
        self.refreshes.append(refresh_if)
        return "current" if refresh_if is None else "replacement"


def _request(**options: Any) -> Payload:
    return {
        "model": "claudex-opus",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 123,
        **options,
    }


def _events(*events: Payload) -> str:
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


def _completed(*items: Payload) -> Payload:
    return {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "output": list(items),
            "usage": {
                "input_tokens": 61,
                "output_tokens": 7,
                "input_tokens_details": {"cached_tokens": 11, "cache_write_tokens": 3},
            },
        },
    }


def _text(value: str) -> Payload:
    return {"type": "message", "content": [{"type": "output_text", "text": value}]}


def _stream(*events: Payload) -> httpx2.Response:
    return httpx2.Response(
        200, headers={"Content-Type": "text/event-stream"}, text=_events(*events)
    )


def _client(upstream: httpx2.AsyncClient, tokens: Tokens) -> httpx2.AsyncClient:
    gateway = create_gateway(
        {"claudex-opus": _ROUTE},
        client=upstream,
        token_provider=tokens,
        gateway_token=_GATEWAY_TOKEN,
    )
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=gateway), base_url="http://local")


def _sse_payloads(response: httpx2.Response) -> list[Payload]:
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


async def test_gateway_resolves_family_versions_without_advertising_extra_routes(
    tmp_path: Path,
) -> None:
    model_map = load_model_map(tmp_path / "models.yaml")
    catalog = {
        route.model: CatalogModel(route.model, 872_000) for route in model_map.routes.values()
    }
    launch = plan_launch(model_map, None, catalog)
    expected = {
        "claude-opus-5": "gpt-6.2-sol",
        "claude-opus-5-5": "gpt-6.2-sol",
        "claude-3-opus-20240229": "gpt-6.2-sol",
        "gpt-6.2-sol": "gpt-6.2-sol",
        "claude-sonnet-5": "gpt-6.1-terra",
        "claude-sonnet-4-6": "gpt-6.1-terra",
        "claude-haiku-4-5-20251001": "gpt-6-luna",
        "claude-fable-5-2": "gpt-6.2-astra",
        "gpt-6.2-astra": "gpt-6.2-astra",
        "claudex-luna-5-6": "gpt-5.6-luna",
        "gpt-5.6-terra": "gpt-5.6-terra",
    }
    observed: list[str] = []

    def send(request: httpx2.Request) -> httpx2.Response:
        observed.append(json.loads(request.content)["model"])
        return _stream(_completed(_text("hello")))

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(send)) as upstream:
        app = create_gateway(
            launch.routes,
            client=upstream,
            token_provider=Tokens(),
            gateway_token=_GATEWAY_TOKEN,
            aliases={
                **launch.aliases,
                **{wire: route for route, wire in launch.desktop_models.items()},
            },
            families=launch.families,
        )
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://local"
        ) as client:
            models = await client.get("/v1/models", headers=_AUTH)
            assert [item["id"] for item in models.json()["data"]] == list(launch.routes)
            for model, target in expected.items():
                response = await client.post(
                    "/v1/messages", headers=_AUTH, json=_request(model=model)
                )
                assert response.status_code == 200
                assert response.json()["model"] == model
                assert observed[-1] == target
            missing = await client.post(
                "/v1/messages", headers=_AUTH, json=_request(model="claude-sonnet-invalid")
            )
            assert missing.status_code == 404


async def test_readiness_proves_gateway_identity_without_disclosing_the_token() -> None:
    challenge = "ab" * 32
    expected = hmac.digest(
        _GATEWAY_TOKEN.encode(), b"claudex-desktop-ready-v1:" + challenge.encode(), "sha256"
    ).hex()
    async with httpx2.AsyncClient() as upstream, _client(upstream, Tokens()) as client:
        response = await client.get("/health/ready", headers={"X-Claudex-Challenge": challenge})
        models = await client.get("/v1/models")
        live = await client.get("/health/live")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"proof": expected}
    assert _GATEWAY_TOKEN not in response.text
    assert models.status_code == 401
    assert live.json() == {"status": "ok"}


@pytest.mark.parametrize("challenge", [None, "", "a" * 63, "a" * 65, "A" * 64, "g" * 64])
async def test_readiness_rejects_missing_or_invalid_challenges(challenge: str | None) -> None:
    headers = {} if challenge is None else {"X-Claudex-Challenge": challenge}
    async with httpx2.AsyncClient() as upstream, _client(upstream, Tokens()) as client:
        response = await client.get("/health/ready", headers=headers)
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"error": "Invalid readiness request."}


async def test_readiness_rejects_repeated_challenge_headers() -> None:
    headers = [("X-Claudex-Challenge", "a" * 64), ("X-Claudex-Challenge", "b" * 64)]
    async with httpx2.AsyncClient() as upstream, _client(upstream, Tokens()) as client:
        response = await client.get("/health/ready", headers=headers)
    assert response.status_code == 400
    assert "proof" not in response.json()


async def test_shutdown_requires_one_use_gateway_proof() -> None:
    stopped: list[bool] = []
    async with httpx2.AsyncClient() as upstream:
        gateway = create_gateway(
            {"claudex-opus": _ROUTE},
            client=upstream,
            token_provider=Tokens(),
            gateway_token=_GATEWAY_TOKEN,
            stop=lambda: stopped.append(True),
        )
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=gateway), base_url="http://local"
        ) as client:
            rejected_challenge = (await client.get("/control/challenge")).json()["challenge"]
            rejected = {
                "X-Claudex-Challenge": rejected_challenge,
                "X-Claudex-Proof": "0" * 64,
            }
            assert (await client.post("/control/stop", headers=rejected)).status_code == 401
            assert not stopped
            challenge = (await client.get("/control/challenge")).json()["challenge"]
            proof = hmac.digest(
                _GATEWAY_TOKEN.encode(), b"claudex-stop-v1:" + challenge.encode(), "sha256"
            ).hex()
            headers = {"X-Claudex-Challenge": challenge, "X-Claudex-Proof": proof}
            assert (await client.post("/control/stop", headers=headers)).status_code == 204
            assert (await client.post("/control/stop", headers=headers)).status_code == 401
            assert (await client.post("/control/stop")).status_code == 401
    assert stopped == [True]


async def test_stream_maps_text_effort_image_and_usage() -> None:
    captured: list[Payload] = []

    def send(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["authorization"] == "Bearer current"
        captured.append(json.loads(request.content))
        return _stream(
            {
                "type": "response.output_text.delta",
                "output_index": 1,
                "content_index": 0,
                "delta": "he",
            },
            _completed({"type": "reasoning", "summary": []}, _text("hello")),
        )

    body = _request(
        stream=True,
        system=[
            {"type": "text", "text": "Act as the coding assistant.", "cache_control": {}},
            {"type": "text", "text": "Preserve the project conventions."},
        ],
        messages=[
            {"role": "user", "content": "One"},
            {"role": "assistant", "content": "The previous answer."},
            {"role": "system", "content": "Expired reminder", "clear_at": "next_user_message"},
            {"role": "system", "content": [], "output_config": {"effort": "high"}},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe the image."},
                    {
                        "type": "image",
                        "source": {"type": "url", "url": "https://example.com/a.png"},
                    },
                ],
            },
            {"role": "system", "content": "Current reminder", "clear_at": "next_user_message"},
        ],
        output_config={"effort": "low"},
        metadata={"session_id": "sensitive-metadata"},
    )
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(send)) as upstream,
        _client(upstream, Tokens()) as client,
    ):
        response = await client.post("/v1/messages?beta=true", json=body, headers=_AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    payload = captured[0]
    assert payload == {
        "model": "gpt-test",
        "stream": True,
        "store": False,
        "reasoning": {"effort": "high"},
        "input": [
            {
                "type": "message",
                "role": "developer",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Act as the coding assistant.\n\nPreserve the project conventions.",
                    }
                ],
            },
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "One"}]},
            {"role": "assistant", "content": "The previous answer."},
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Describe the image."},
                    {"type": "input_image", "image_url": "https://example.com/a.png"},
                ],
            },
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "Current reminder"}],
            },
        ],
    }
    events = _sse_payloads(response)
    assert [event["type"] for event in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert events[0]["message"]["model"] == "claudex-opus"
    assert events[2]["delta"]["text"] + events[3]["delta"]["text"] == "hello"
    assert events[-2]["usage"] == {
        "input_tokens": 47,
        "output_tokens": 7,
        "cache_read_input_tokens": 11,
        "cache_creation_input_tokens": 3,
    }


@pytest.mark.parametrize("system", [None, "", [], "Follow the supplied instructions."])
async def test_system_message_uses_leading_developer_input(system: Any) -> None:
    captured: list[Payload] = []

    def send(request: httpx2.Request) -> httpx2.Response:
        captured.append(json.loads(request.content))
        return _stream(_completed(_text("Synthetic reply")))

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(send)) as upstream,
        _client(upstream, Tokens()) as client,
    ):
        response = await client.post("/v1/messages", json=_request(system=system), headers=_AUTH)
    assert response.status_code == 200
    payload = captured[0]
    assert "instructions" not in payload
    if system:
        assert payload["input"][0] == {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": system}],
        }
    assert payload["input"][-1] == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "Hello"}],
    }
    assert len(payload["input"]) == (2 if system else 1)


async def test_cache_session_identity_is_scoped_validated_and_not_forwarded_raw() -> None:
    sessions: list[str | None] = []
    requests: list[Payload] = []
    original = "a1b2c3d4-1234-4123-8123-0123456789ab"
    other = "a1b2c3d4-1234-4123-8123-0123456789ac"
    header = "x-claude-code-session-id"

    def send(request: httpx2.Request) -> httpx2.Response:
        sessions.append(request.headers.get("session-id"))
        requests.append(json.loads(request.content))
        assert "x-claude-code-session-id" not in request.headers
        return _stream(_completed(_text("Synthetic reply")))

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(send)) as upstream:
        async with _client(upstream, Tokens()) as client:
            for session in (original, original.upper(), other):
                response = await client.post(
                    "/v1/messages", json=_request(), headers={**_AUTH, header: session}
                )
                assert response.status_code == 200

            for extra in (
                [],
                [(header, "")],
                [(header, "a1b2c3d41234412381230123456789ab")],
                [(header, original + ", " + other)],
                [(header, original), (header, other)],
                [("session-id", original)],
            ):
                response = await client.post(
                    "/v1/messages",
                    json=_request(metadata={"session_id": original}),
                    headers=[("Authorization", _AUTH["Authorization"]), *extra],
                )
                assert response.status_code == 200

        gateway = create_gateway(
            {"claudex-opus": _ROUTE},
            client=upstream,
            token_provider=Tokens(),
            gateway_token="another-synthetic-gateway-secret",
        )
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=gateway), base_url="http://local"
        ) as client:
            response = await client.post(
                "/v1/messages",
                json=_request(),
                headers={
                    "Authorization": "Bearer another-synthetic-gateway-secret",
                    header: original,
                },
            )
            assert response.status_code == 200

    assert sessions[0] == sessions[1]
    assert sessions[0] != sessions[2]
    assert sessions[0] != sessions[-1]
    assert sessions[0] != original
    assert sessions[0] is not None and UUID(sessions[0]).version == 4
    assert all(session is None for session in sessions[3:-1])
    assert all("metadata" not in payload and "session-id" not in payload for payload in requests)


async def test_stream_accepts_responses_sse_without_content_type() -> None:
    def send(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["accept"] == "text/event-stream"
        sse = ": keepalive\n\n" + _events(_completed(_text("Synthetic reply")))
        return httpx2.Response(200, content=sse.encode())

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(send)) as upstream,
        _client(upstream, Tokens()) as client,
    ):
        response = await client.post("/v1/messages", json=_request(stream=True), headers=_AUTH)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    events = _sse_payloads(response)
    assert events[2]["delta"]["text"] == "Synthetic reply"
    assert events[-1] == {"type": "message_stop"}


async def test_responses_explicit_content_type_still_rejects_an_incompatible_media_type() -> None:
    def send(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=_events(_completed(_text("Synthetic reply"))).encode(),
        )

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(send)) as upstream,
        _client(upstream, Tokens()) as client,
    ):
        response = await client.post("/v1/messages", json=_request(stream=True), headers=_AUTH)

    assert response.status_code == 502
    assert "outside the streaming protocol" in response.json()["error"]["message"]


async def test_responses_headerless_sse_must_still_contain_valid_events() -> None:
    def send(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"data: malformed\n\n")

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(send)) as upstream,
        _client(upstream, Tokens()) as client,
    ):
        response = await client.post("/v1/messages", json=_request(stream=True), headers=_AUTH)

    assert response.status_code == 200
    events = _sse_payloads(response)
    assert [event["type"] for event in events] == ["message_start", "error"]
    assert "malformed streaming JSON" in events[-1]["error"]["message"]


async def test_tool_order_is_stable_across_equivalent_requests() -> None:
    captured: list[Payload] = []

    def send(request: httpx2.Request) -> httpx2.Response:
        captured.append(json.loads(request.content))
        return _stream(_completed(_text("Synthetic reply")))

    tools = [
        {
            "name": "zeta",
            "description": "Look up zeta.",
            "input_schema": {"type": "object", "properties": {"z": {}, "a": {}}},
            "strict": True,
        },
        {"name": "alpha", "input_schema": {"type": "object", "properties": {}}},
    ]
    choice = {"type": "tool", "name": "zeta", "disable_parallel_tool_use": True}
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(send)) as upstream,
        _client(upstream, Tokens()) as client,
    ):
        for definitions in (tools, list(reversed(tools))):
            response = await client.post(
                "/v1/messages", json=_request(tools=definitions, tool_choice=choice), headers=_AUTH
            )
            assert response.status_code == 200

    assert captured[0]["tools"] == captured[1]["tools"]
    ordered = captured[0]["tools"][0]["tools"]
    assert [tool["name"] for tool in ordered] == ["alpha", "zeta"]
    assert list(ordered[1]["parameters"]["properties"]) == ["z", "a"]
    assert ordered[1]["strict"] is True
    for payload in captured:
        assert payload["tool_choice"] == {"type": "function", "name": "claude_code.zeta"}
        assert payload["parallel_tool_calls"] is False


async def test_tool_call_and_result_round_trip_with_structured_output() -> None:
    captured: list[Payload] = []
    sessions: list[str | None] = []
    tool_call = {
        "type": "function_call",
        "namespace": "claude_code",
        "name": "mcp__project__read",
        "call_id": "call_123",
        "arguments": '{"path":"README.md"}',
    }

    def send(request: httpx2.Request) -> httpx2.Response:
        captured.append(json.loads(request.content))
        sessions.append(request.headers.get("session-id"))
        return _stream(_completed(tool_call if len(captured) == 1 else _text('{"ok":true}')))

    tools = [{"name": "mcp__project__read", "input_schema": {"type": "object"}}]
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(send)) as upstream,
        _client(upstream, Tokens()) as client,
    ):
        initial = _request(tools=tools, tool_choice={"type": "tool", "name": "mcp__project__read"})
        session = "f3e4a5b6-0111-4222-8333-444455556666"
        headers = {**_AUTH, "x-claude-code-session-id": session}
        response = await client.post("/v1/messages", json=initial, headers=headers)
        assert response.status_code == 200
        call = response.json()["content"][0]
        assert call == {
            "type": "tool_use",
            "id": "call_123",
            "name": "mcp__project__read",
            "input": {"path": "README.md"},
        }
        assert response.json()["stop_reason"] == "tool_use"
        continued = _request(
            messages=[
                *initial["messages"],
                {"role": "assistant", "content": [call]},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_123",
                            "is_error": True,
                            "content": [
                                {"type": "text", "text": "Cannot open the requested file."},
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": "YQ==",
                                    },
                                },
                            ],
                        },
                        {"type": "text", "text": "Return whether it worked."},
                    ],
                },
            ],
            tools=tools,
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        response = await client.post("/v1/messages", json=continued, headers=headers)
    assert response.status_code == 200
    assert sessions[0] == sessions[1] and sessions[0] != session
    assert sessions[0] is not None and UUID(sessions[0]).version == 4
    assert response.json()["content"] == [{"type": "text", "text": '{"ok":true}'}]
    assert captured[0]["tool_choice"] == {
        "type": "function",
        "name": "claude_code.mcp__project__read",
    }
    namespace = captured[0]["tools"][0]
    assert namespace["type"] == "namespace"
    assert namespace["tools"] == [
        {
            "type": "function",
            "name": "mcp__project__read",
            "parameters": {"type": "object"},
            "strict": False,
        }
    ]
    assert captured[1]["input"][1] == tool_call
    assert captured[1]["input"][2] == {
        "type": "function_call_output",
        "call_id": "call_123",
        "name": "mcp__project__read",
        "namespace": "claude_code",
        "output": [
            {"type": "input_text", "text": "Tool execution failed."},
            {"type": "input_text", "text": "Cannot open the requested file."},
            {"type": "input_image", "image_url": "data:image/png;base64,YQ=="},
        ],
    }
    assert captured[1]["text"]["format"]["schema"] == schema


@pytest.mark.parametrize(
    ("pieces", "stops", "expected_text", "matched"),
    [
        (
            ["Verdict: re", "view<E", "ND> tail<OTHER>"],
            ["<OTHER>", "<END>"],
            ["Verdict: re", "view"],
            "<END>",
        ),
        (["<END>ignore"], ["<END>"], [], "<END>"),
        (["Keep all text"], ["<END>"], ["Keep all text"], None),
    ],
)
async def test_nonstreaming_stop_sequences_cut_only_completed_upstream_text(
    pieces: list[str], stops: list[str], expected_text: list[str], matched: str | None
) -> None:
    def send(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        assert payload == {
            "model": "gpt-test",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Hello"}],
                }
            ],
            "stream": True,
            "store": False,
        }
        return _stream(
            {
                "type": "response.output_text.delta",
                "output_index": 1,
                "content_index": 0,
                "delta": pieces[0][:2],
            },
            _completed({"type": "reasoning", "summary": []}, *(_text(piece) for piece in pieces)),
        )

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(send)) as upstream,
        _client(upstream, Tokens()) as client,
    ):
        response = await client.post(
            "/v1/messages",
            json=_request(max_tokens=2112, stop_sequences=stops, tools=[]),
            headers=_AUTH,
        )

    assert response.status_code == 200
    message = response.json()
    assert message["content"] == [{"type": "text", "text": text} for text in expected_text]
    assert message["stop_reason"] == ("stop_sequence" if matched else "end_turn")
    assert message["stop_sequence"] == matched
    assert message["usage"]["output_tokens"] == 7


@pytest.mark.parametrize(("incomplete", "reason"), [(False, "refusal"), (True, "max_tokens")])
async def test_stop_sequences_preserve_refusal_and_truncation(
    incomplete: bool, reason: str
) -> None:
    text = "Unchanged<END> content"
    if incomplete:
        event = _completed(_text(text))
        event["type"] = "response.incomplete"
        event["response"].update(
            status="incomplete", incomplete_details={"reason": "max_output_tokens"}
        )
    else:
        event = _completed({"type": "message", "content": [{"type": "refusal", "refusal": text}]})

    def send(_request: httpx2.Request) -> httpx2.Response:
        return _stream(event)

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(send)) as upstream,
        _client(upstream, Tokens()) as client,
    ):
        response = await client.post(
            "/v1/messages", json=_request(stop_sequences=["<END>"]), headers=_AUTH
        )

    assert response.status_code == 200
    assert response.json()["content"] == [{"type": "text", "text": text}]
    assert response.json()["stop_reason"] == reason
    assert response.json()["stop_sequence"] is None


async def test_stop_sequences_preserve_upstream_failure_after_text() -> None:
    def send(_request: httpx2.Request) -> httpx2.Response:
        return _stream(
            {
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "delta": "Partial<END> text",
            },
            {"type": "response.failed", "response": {"error": {"message": "raw private error"}}},
        )

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(send)) as upstream,
        _client(upstream, Tokens()) as client,
    ):
        response = await client.post(
            "/v1/messages", json=_request(stop_sequences=["<END>"]), headers=_AUTH
        )

    assert response.status_code == 502
    assert "raw private error" not in response.text
    assert "Partial" not in response.text


@pytest.mark.parametrize(
    "options",
    [
        {"stream": True},
        {"tools": [{"name": "Read", "input_schema": {"type": "object"}}]},
        {"tool_choice": {"type": "any"}},
        {"stop_sequences": [""]},
        {"output_format": {"type": "json_schema", "schema": {"type": "object"}}},
        {"output_config": {"format": {"type": "json_schema", "schema": {"type": "object"}}}},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "url", "url": "https://example.com/a.png"},
                        }
                    ],
                }
            ],
        },
        {
            "messages": [
                {"role": "user", "content": "Check the report."},
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "call_12", "name": "Read", "input": {}}],
                },
            ],
        },
    ],
)
async def test_stop_sequences_reject_unsupported_requests(options: Payload) -> None:
    def send(_request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("Invalid requests must not reach OpenAI")

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(send)) as upstream,
        _client(upstream, Tokens()) as client,
    ):
        body = _request(stop_sequences=["<END>"])
        body.update(options)
        response = await client.post("/v1/messages", json=body, headers=_AUTH)

    assert response.status_code == 400
    assert "stop sequences" in response.json()["error"]["message"].lower()


async def test_authentication_validation_and_context_rejection_use_messages_errors() -> None:
    def send(_request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("Invalid requests must not reach OpenAI")

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(send)) as upstream,
        _client(upstream, Tokens()) as client,
    ):
        unauthenticated = await client.post("/v1/messages", content="invalid")
        assert unauthenticated.status_code == 401
        unknown = await client.post("/v1/messages", json=_request(model="gpt-test"), headers=_AUTH)
        assert unknown.status_code == 404
        malformed = await client.post("/v1/messages", json={"messages": "sensitive"}, headers=_AUTH)
        assert malformed.status_code == 400
        assert "sensitive" not in malformed.text
        effort = await client.post(
            "/v1/messages", json=_request(output_config={"effort": "max"}), headers=_AUTH
        )
        assert effort.status_code == 400
        assert "effort mapping" in effort.json()["error"]["message"]
        compaction = await client.post(
            "/v1/messages", json=_request(context_management={"edits": []}), headers=_AUTH
        )
        assert compaction.status_code == 400
        assert "context_management: Extra inputs are not permitted" in compaction.text
        assert (await client.get("/v1/models")).status_code == 401
        models = await client.get("/v1/models", headers={"x-api-key": _GATEWAY_TOKEN})
        assert [item["id"] for item in models.json()["data"]] == ["claudex-opus"]
        assert (await client.get("/health/live")).json() == {"status": "ok"}
        assert (await client.head("/api/hello")).status_code == 204


async def test_upstream_refreshes_once_and_releases_rate_limit_metadata() -> None:
    tokens = Tokens()
    credentials: list[str] = []
    sessions: list[str | None] = []

    def send(request: httpx2.Request) -> httpx2.Response:
        credentials.append(request.headers["authorization"])
        sessions.append(request.headers.get("session-id"))
        if len(credentials) == 1:
            return httpx2.Response(401, json={"error": {"message": "sensitive raw message"}})
        return httpx2.Response(
            429,
            headers={"Retry-After": "123"},
            json={
                "error": {
                    "code": "subscription_sharing_usage_limit_exceeded",
                    "message": "sensitive raw message",
                }
            },
        )

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(send)) as upstream,
        _client(upstream, tokens) as client,
    ):
        response = await client.post(
            "/v1/messages",
            json=_request(stream=True),
            headers={**_AUTH, "x-claude-code-session-id": "f3e4a5b6-0111-4222-8333-444455556666"},
        )
    assert credentials == ["Bearer current", "Bearer replacement"]
    assert sessions[0] is not None and sessions[0] == sessions[1]
    assert tokens.refreshes == [None, "current"]
    assert response.status_code == 429
    assert response.headers["retry-after"] == "123"
    assert response.headers["x-should-retry"] == "false"
    assert "sensitive" not in response.text


async def test_premature_stream_exit_sends_error_without_success_stop() -> None:
    def send(_request: httpx2.Request) -> httpx2.Response:
        return _stream({"type": "response.in_progress"})

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(send)) as upstream,
        _client(upstream, Tokens()) as client,
    ):
        streamed = await client.post("/v1/messages", json=_request(stream=True), headers=_AUTH)
        collected = await client.post("/v1/messages", json=_request(), headers=_AUTH)
    assert streamed.status_code == 200
    assert [event["type"] for event in _sse_payloads(streamed)] == ["message_start", "error"]
    assert collected.status_code == 502


class BlockedStream(httpx2.AsyncByteStream):
    def __init__(self) -> None:
        self.read_started = anyio.Event()
        self.closed = anyio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.read_started.set()
        await anyio.sleep_forever()
        yield b""

    async def aclose(self) -> None:
        await anyio.lowlevel.checkpoint()
        self.closed.set()


async def test_client_disconnect_cancels_and_closes_inference_stream() -> None:
    stream = BlockedStream()

    def open_stream(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)

    request_sent = False
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": json.dumps(_request(stream=True)).encode()}
        await stream.read_started.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(open_stream)) as upstream:
        app = create_gateway(
            {"claudex-opus": _ROUTE},
            client=upstream,
            token_provider=Tokens(),
            gateway_token=_GATEWAY_TOKEN,
        )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "path": "/v1/messages",
            "query_string": b"",
            "scheme": "http",
            "headers": [(b"authorization", _AUTH["Authorization"].encode())],
        }
        with anyio.fail_after(2):
            await app(scope, receive, send)
    assert stream.closed.is_set()
    assert sent[-1]["more_body"]


async def test_temporary_account_failure_is_reported_before_stream_starts() -> None:
    class Unavailable(Tokens):
        async def access_token(self, *, refresh_if: str | None = None) -> str:
            raise AccountError("The credential store is unavailable.", status_code=503)

    async with httpx2.AsyncClient() as upstream, _client(upstream, Unavailable()) as client:
        response = await client.post("/v1/messages", json=_request(stream=True), headers=_AUTH)
    assert response.status_code == 503
    assert response.json()["error"]["message"] == "The credential store is unavailable."
