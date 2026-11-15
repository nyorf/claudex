import asyncio
import json
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx2

from claudex.config.launch import Launch
from claudex.domain.models import ModelRoute
from claudex.main import run_claude

_MARKER = "CLAUDEX_SMOKE_FILE_CONTENT"
_COMPLETE = "CLAUDEX_SMOKE_COMPLETE"


class _SyntheticProfile:
    async def access_token(self, *, refresh_if: str | None = None) -> str:
        return "synthetic-openai-token"


class _Upstream:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.called = False
        self.returned = False
        self.effort: str | None = None

    def send(self, request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == "https://api.openai.com/v1/responses"
        body = json.loads(request.content)
        assert body["model"] == "gpt-synthetic"
        assert body["stream"] and not body["store"]
        result = [item for item in body["input"] if item.get("type") == "function_call_output"]
        if result:
            assert result[-1]["call_id"] == "call_claudex_smoke"
            assert _MARKER in json.dumps(result[-1]["output"])
            self.returned = True
            item = _text(_COMPLETE)
        elif _has_read_tool(body) and not self.called:
            self.called = True
            self.effort = body.get("reasoning", {}).get("effort")
            item = {
                "type": "function_call",
                "namespace": "claude_code",
                "name": "Read",
                "call_id": "call_claudex_smoke",
                "arguments": json.dumps({"file_path": str(self.path)}),
            }
        else:
            item = _text("Claudex smoke")
        event = {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [item],
                "usage": {"input_tokens": 1042, "output_tokens": 12},
            },
        }
        return httpx2.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text=f"event: response.completed\ndata: {json.dumps(event)}\n\n",
        )


def _text(value: str) -> dict[str, Any]:
    return {"type": "message", "content": [{"type": "output_text", "text": value}]}


def _has_read_tool(body: dict[str, Any]) -> bool:
    return any(
        tool.get("name") == "Read"
        for namespace in body.get("tools", [])
        for tool in namespace.get("tools", [])
    )


async def _run(binary: str, root: Path) -> None:
    for name in ("home", "config", "tmp"):
        (root / name).mkdir()
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
    file = root / "input.txt"
    file.write_text(f"{_MARKER}\n", encoding="ascii")
    (root / "config" / "settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
                    "ANTHROPIC_AUTH_TOKEN": "unrelated-synthetic-token",
                    "CLAUDE_CODE_EFFORT_LEVEL": "low",
                }
            }
        ),
        encoding="utf-8",
    )
    route = ModelRoute("gpt-synthetic", "Opus (gpt-synthetic)", 475000, {"high": "high"})
    launch = Launch("claudex-opus", {"claudex-opus": route}, 475000, "synthetic")
    upstream = _Upstream(file)
    async with (
        asyncio.timeout(45),
        httpx2.AsyncClient(transport=httpx2.MockTransport(upstream.send)) as client,
    ):
        code = await run_claude(
            client,
            _SyntheticProfile(),
            binary,
            launch,
            "high",
            [
                "--strict-mcp-config",
                "--no-session-persistence",
                "--tools",
                "Read",
                "--allowedTools",
                "Read",
                "--output-format",
                "json",
                "-p",
                "Read the input file in this workspace.",
            ],
        )
    if code != 0 or not upstream.returned or upstream.effort != "high":
        raise SystemExit("Claude Code did not complete the expected tool and effort round trip.")
    print("Claude Code executed Read and returned the result to the synthetic GPT request.")


def main() -> None:
    binary = shutil.which(os.environ.get("CLAUDEX_CLAUDE_BIN", "claude"))
    if binary is None:
        raise SystemExit("Install Claude Code or set CLAUDEX_CLAUDE_BIN.")
    with TemporaryDirectory(prefix="claudex-cli-") as temporary:
        cwd = Path.cwd()
        try:
            asyncio.run(_run(binary, Path(temporary)))
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    main()
