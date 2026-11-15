import asyncio
import inspect
import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

import httpx2

from claudex.config.launch import Launch
from claudex.domain.models import ModelRoute
from claudex.main import run_claude

type Payload = dict[str, Any]
_INITIAL = "CLAUDEX_WORKFLOW_INITIAL"
_RESUME = "CLAUDEX_WORKFLOW_RESUME"
_HOOK = "CLAUDEX_WORKFLOW_HOOK"
_SKILL = "CLAUDEX_WORKFLOW_SKILL"
_MCP = "CLAUDEX_WORKFLOW_MCP"
_AGENT = "CLAUDEX_WORKFLOW_AGENT"
_STRUCTURED = "CLAUDEX_WORKFLOW_STRUCTURED"
_COMPACTED = "CLAUDEX_WORKFLOW_COMPACTED"
_AFTER_COMPACT = "CLAUDEX_WORKFLOW_AFTER_COMPACT"
_AUTO_START = "CLAUDEX_WORKFLOW_AUTO_START"
_AUTO_COMPACTED = "CLAUDEX_WORKFLOW_AUTO_COMPACTED"
_AUTO_END = "CLAUDEX_WORKFLOW_AUTO_END"
_MIXED_FIRST = "CLAUDEX_MIXED_FIRST"
_MIXED_LOAD = "CLAUDEX_MIXED_LOAD"
_MIXED_FOLLOWUP = "CLAUDEX_MIXED_FOLLOWUP"
_MIXED_SUMMARY = "CLAUDEX_MIXED_SUMMARY"
_BASE_MODEL = "gpt-synthetic"
_SMALL_MODEL = "gpt-synthetic-small"
_LARGE_MODEL = "gpt-synthetic-large"
_MIXED_USAGE = 265_000
_DENY = "CLAUDEX_WORKFLOW_DENY"
_PRIVATE = "CLAUDEX_WORKFLOW_DENIED_FILE"
_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+XbcEAAAAASUVORK5CYII="
)
_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
    "additionalProperties": False,
}


class _Profile:
    async def access_token(self, *, refresh_if: str | None = None) -> str:
        return "synthetic"


def _mcp_server() -> None:
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        match request["method"]:
            case "initialize":
                result = {
                    "protocolVersion": request["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "claudex-smoke", "version": "1"},
                }
            case "tools/list":
                result = {
                    "tools": [
                        {"name": "sample", "inputSchema": {"type": "object", "properties": {}}}
                    ]
                }
            case "tools/call":
                assert request["params"]["name"] == "sample"
                result = {
                    "content": [
                        {"type": "text", "text": _MCP},
                        {"type": "image", "mimeType": "image/png", "data": _PNG},
                    ]
                }
            case _:
                result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


def _text(text: str) -> Payload:
    return {"type": "message", "content": [{"type": "output_text", "text": text}]}


def _call(name: str, arguments: Payload) -> Payload:
    return {
        "type": "function_call",
        "namespace": "claude_code",
        "name": name,
        "call_id": f"call_{uuid4().hex}",
        "arguments": json.dumps(arguments),
    }


def _result(item: Payload, *, input_tokens: int = 1800) -> httpx2.Response:
    event = {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "output": [item],
            "usage": {"input_tokens": input_tokens, "output_tokens": 40},
        },
    }
    return httpx2.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        text=f"event: response.completed\ndata: {json.dumps(event)}\n\n",
    )


class _Upstream:
    def __init__(self, denied_file: Path) -> None:
        self.observed: set[str] = set()
        self._denied_file = denied_file

    def send(self, request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == "https://api.openai.com/v1/responses"
        body = json.loads(request.content)
        assert body["model"] in {_BASE_MODEL, _SMALL_MODEL, _LARGE_MODEL}
        assert body["stream"] and not body["store"]
        if body["model"] != _BASE_MODEL:
            return self._mixed_compaction(body)
        return self._respond(body)

    def _mixed_compaction(self, body: Payload) -> httpx2.Response:
        route = "small" if body["model"] == _SMALL_MODEL else "large"
        transcript = json.dumps(body["input"])
        if "<summary>" in transcript.lower() and _MIXED_FIRST in transcript:
            self.observed.add(f"{route}-mixed-compaction")
            return _result(_text(f"<summary>{_MIXED_SUMMARY}</summary>"))
        if _is_user_prompt(body, _MIXED_FOLLOWUP):
            if _MIXED_SUMMARY in transcript:
                self.observed.add(f"{route}-mixed-summary-replay")
            elif _MIXED_FIRST in transcript and _MIXED_LOAD in transcript:
                self.observed.add(f"{route}-mixed-original-replay")
        elif _is_user_prompt(body, _MIXED_LOAD) and _MIXED_FIRST in transcript:
            self.observed.add(f"{route}-mixed-usage")
            return _result(_text("Claudex mixed window load"), input_tokens=_MIXED_USAGE)
        return _result(_text("Claudex mixed window reply"))

    def _respond(self, body: Payload) -> httpx2.Response:
        tool_names = {
            tool["name"] for group in body.get("tools", []) for tool in group.get("tools", [])
        }
        result_items = [
            item for item in body["input"] if item.get("type") == "function_call_output"
        ]
        text = json.dumps(body["input"])
        self._observe_history(text)
        if _is_user_prompt(body, _AGENT):
            self.observed.add("subagent")
            return _result(_text(f"{_AGENT}_DONE"))
        if _INITIAL in text and "<summary>" in text.lower():
            self.observed.add("compaction")
            return _result(_text(f"<summary>{_COMPACTED}</summary>"))
        if _AFTER_COMPACT in text and _COMPACTED in text:
            self.observed.add("compacted-resume")
        if _AUTO_START in text and "<summary>" in text.lower():
            self.observed.add("auto-compaction")
            return _result(_text(f"<summary>{_AUTO_COMPACTED}</summary>"))
        if _AUTO_END in text and _AUTO_COMPACTED in text:
            self.observed.add("auto-compacted-resume")
        if _AUTO_START in text and _AUTO_END not in text:
            return _result(_text("Conversation to compact"), input_tokens=460000)
        if _STRUCTURED in text:
            return _result(self._structured(body, tool_names, result_items))
        if _DENY in text:
            return _result(self._permission(result_items))
        return _result(self._workflow(tool_names, result_items))

    def _observe_history(self, text: str) -> None:
        for marker, name in ((_HOOK, "hook"), (_SKILL, "skill")):
            if marker in text:
                self.observed.add(name)
        if _RESUME in text and _INITIAL in text:
            self.observed.add("resume")

    def _permission(self, results: list[Payload]) -> Payload:
        if not results:
            return _call("Read", {"file_path": str(self._denied_file)})
        evidence = json.dumps(results)
        if "Tool execution failed." in evidence and _PRIVATE not in evidence:
            self.observed.add("permission-denial")
        return _text("Claudex permission reply")

    def _workflow(self, tool_names: set[str], results: list[Payload]) -> Payload:
        if "mcp__smoke__sample" in tool_names and "mcp-dispatched" not in self.observed:
            self.observed.add("mcp-dispatched")
            return _call("mcp__smoke__sample", {})
        if _MCP in json.dumps(results):
            self.observed.add("mcp")
            if "data:image/png;base64," in json.dumps(results):
                self.observed.add("image")
        if "Skill" in tool_names and "skill-dispatched" not in self.observed:
            self.observed.add("skill-dispatched")
            return _call("Skill", {"skill": "workflow-smoke"})
        if "Agent" in tool_names and "agent-dispatched" not in self.observed:
            self.observed.add("agent-dispatched")
            return _call(
                "Agent",
                {
                    "description": "Test a local agent",
                    "prompt": _AGENT,
                    "subagent_type": "general-purpose",
                    "run_in_background": False,
                },
            )
        if f"{_AGENT}_DONE" in json.dumps(results):
            self.observed.add("subagent-result")
        return _text("Claudex workflow reply")

    def _structured(self, body: Payload, tools: set[str], results: list[Payload]) -> Payload:
        value = {"value": _STRUCTURED}
        if body.get("text", {}).get("format", {}).get("schema") == _SCHEMA:
            self.observed.add("structured-output")
            return _text(json.dumps(value))
        if "StructuredOutput" in tools and not results:
            self.observed.add("structured-output-tool")
            return _call("StructuredOutput", value)
        return _text(json.dumps(value))


def _is_user_prompt(body: Payload, prompt: str) -> bool:
    return any(
        item.get("role") == "user"
        and any(block.get("text") == prompt for block in item.get("content", []))
        for item in body["input"]
    )


def _write_config(root: Path) -> Path:
    skill = root / ".claude" / "skills" / "workflow-smoke"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: workflow-smoke\ndescription: Test local skill loading.\n---\n\n{_SKILL}\n",
        encoding="utf-8",
    )
    hooks = {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": f"printf '{_HOOK}'"}]}]}
    (root / "config" / "settings.json").write_text(
        json.dumps({"hooks": hooks, "permissions": {"ask": ["Read"]}}), encoding="utf-8"
    )
    server = root / "mcp.py"
    server.write_text(
        "import json\nimport sys\n"
        f"_MCP = {_MCP!r}\n_PNG = {_PNG!r}\n"
        f"{inspect.getsource(_mcp_server)}\n_mcp_server()\n",
        encoding="utf-8",
    )
    mcp = root / "mcp.json"
    mcp.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "smoke": {
                        "command": sys.executable,
                        "args": [
                            str(server),
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return mcp


async def _run(binary: str, root: Path) -> None:
    for directory in ("home", "config", "tmp"):
        (root / directory).mkdir()
    mcp = _write_config(root)
    denied_file = root / "denied.txt"
    denied_file.write_text(_PRIVATE, encoding="ascii")
    os.chdir(root)
    os.environ.clear()
    os.environ.update(
        HOME=str(root / "home"),
        XDG_CONFIG_HOME=str(root / "home"),
        CLAUDE_CONFIG_DIR=str(root / "config"),
        TMPDIR=str(root / "tmp"),
        PATH="/usr/bin:/bin",
        HTTP_PROXY="http://127.0.0.1:9",
        HTTPS_PROXY="http://127.0.0.1:9",
        ALL_PROXY="http://127.0.0.1:9",
        CLAUDE_CODE_MAX_RETRIES="0",
    )
    route = ModelRoute(_BASE_MODEL, "Opus (gpt-synthetic)", 475000, {"high": "high"})
    launch = Launch("claudex-opus", {"claudex-opus": route}, 475000, "synthetic")
    upstream = _Upstream(denied_file)
    session = str(uuid4())
    auto_session = str(uuid4())
    common = ["--strict-mcp-config", "--output-format", "json"]
    phases = [
        [
            "--mcp-config",
            str(mcp),
            "--allowedTools",
            "Agent,Skill,mcp__smoke__sample",
            "--session-id",
            session,
            "-p",
            _INITIAL,
        ],
        ["--resume", session, "-p", _RESUME],
        ["--resume", session, "-p", "/compact"],
        ["--resume", session, "-p", _AFTER_COMPACT],
        ["--json-schema", json.dumps(_SCHEMA), "--no-session-persistence", "-p", _STRUCTURED],
        ["--session-id", auto_session, "-p", _AUTO_START],
        ["--resume", auto_session, "-p", _AUTO_END],
        ["--tools", "Read", "--no-session-persistence", "-p", _DENY],
    ]
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(upstream.send)) as client:
        for phase in phases:
            await _run_phase(client, binary, launch, [*common, *phase])
        await _probe_mixed_compaction(client, binary, common)
    await _probe_auto_mode(binary, root)
    expected = {
        "hook",
        "mcp",
        "image",
        "skill",
        "subagent",
        "subagent-result",
        "resume",
        "structured-output-tool",
        "compaction",
        "compacted-resume",
        "auto-compaction",
        "auto-compacted-resume",
        "small-mixed-usage",
        "small-mixed-compaction",
        "small-mixed-summary-replay",
        "large-mixed-usage",
        "large-mixed-original-replay",
        "permission-denial",
    }
    missing = expected - upstream.observed
    print(f"Observed: {', '.join(sorted(upstream.observed))}")
    if missing:
        raise SystemExit(f"Missing workflow evidence: {', '.join(sorted(missing))}")
    if {"large-mixed-compaction", "large-mixed-summary-replay"} & upstream.observed:
        raise SystemExit("The larger route compacted at the smaller route's usage.")


async def _probe_mixed_compaction(
    client: httpx2.AsyncClient,
    binary: str,
    common: list[str],
) -> None:
    routes = {
        "claudex-opus": ModelRoute(_SMALL_MODEL, "Opus", 272_000, {"high": "high"}),
        "claudex-sonnet": ModelRoute(_LARGE_MODEL, "Sonnet", 500_000, {"high": "high"}),
    }
    layout = Launch("claudex-opus", routes, 272_000, "synthetic")
    # the compaction request needs two completed rounds so it can preserve the newer one
    for alias, route in routes.items():
        selection = replace(layout, model=alias, context_window=route.context_window)
        session = str(uuid4())
        for prompt, flag in (
            (_MIXED_FIRST, "--session-id"),
            (_MIXED_LOAD, "--resume"),
            (_MIXED_FOLLOWUP, "--resume"),
        ):
            await _run_phase(client, binary, selection, [*common, flag, session, "-p", prompt])


async def _probe_auto_mode(binary: str, root: Path) -> None:
    classifier = "gpt-synthetic-classifier"
    routes = {
        "claudex-opus": ModelRoute(_BASE_MODEL, "Opus", 475_000, {"high": "high"}),
        "claudex-sonnet": ModelRoute(classifier, "Sonnet", 475_000, {"high": "high"}),
    }
    launch = Launch("claudex-opus", routes, 475_000, "synthetic")
    forbidden = "<reasoning>The user forbids this action.</reasoning><block>yes</block>"
    for label, verdict, expected in (
        ("allow", "<block>no</block>", True),
        ("deny", forbidden, False),
        ("invalid verdict", "No verdict", False),
        ("upstream error", None, False),
    ):
        # a write outside the working directory requires a classifier verdict
        marker = root / "tmp" / f"auto-{uuid4().hex}"
        attempts = 0
        classifier_attempts = 0

        def respond(
            request: httpx2.Request, verdict: str | None = verdict, marker: Path = marker
        ) -> httpx2.Response:
            nonlocal attempts, classifier_attempts
            body = json.loads(request.content)
            if not body.get("tools"):
                if body["model"] == classifier:
                    classifier_attempts += 1
                return _result(_text(verdict)) if verdict is not None else httpx2.Response(503)
            attempts += 1
            if attempts == 1:
                return _result(_call("Bash", {"command": f"touch '{marker}'"}))
            return _result(_text("Auto mode smoke reply"))

        working_directory = root / "work"
        working_directory.mkdir(exist_ok=True)
        os.chdir(working_directory)
        try:
            async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
                await _run_phase(
                    client,
                    binary,
                    launch,
                    [
                        "--permission-mode",
                        "auto",
                        "--strict-mcp-config",
                        "--no-session-persistence",
                        "--tools",
                        "Bash",
                        "-p",
                        "Check the proposed shell command.",
                    ],
                )
        finally:
            os.chdir(root)
        if not classifier_attempts or marker.exists() != expected:
            raise SystemExit(f"Auto mode did not honor the synthetic {label} response.")
    print("Auto mode honored a verdict and blocked denied or unreviewed actions.")


async def _run_phase(
    client: httpx2.AsyncClient,
    binary: str,
    launch: Launch,
    args: list[str],
) -> None:
    async with asyncio.timeout(30):
        code = await run_claude(client, _Profile(), binary, launch, "high", args)
    if code:
        raise SystemExit("Claude Code exited before completing the workflow smoke.")


def main() -> None:
    binary = shutil.which(os.environ.get("CLAUDEX_CLAUDE_BIN", "claude"))
    if binary is None:
        raise SystemExit("Install Claude Code or set CLAUDEX_CLAUDE_BIN.")
    with TemporaryDirectory(prefix="claudex-workflow-") as temporary:
        cwd = Path.cwd()
        try:
            asyncio.run(_run(binary, Path(temporary)))
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    main()
