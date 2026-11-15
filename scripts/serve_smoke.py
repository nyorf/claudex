import asyncio
import json
import os
import re
import shlex
import shutil
import stat
import sys
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import httpx2

from claudex.config.launch import Launch
from claudex.domain.models import ModelRoute
from claudex.main import LaunchError, serve_gateway

_WINDOW = 475_000
_ALIAS = "claudex-opus"
_SMALL_ALIAS = "claudex-fable"
_SMALL_WINDOW = 272_000
_UPSTREAM_MODEL = "gpt-synthetic"
_SMALL_MODEL = "gpt-synthetic-small"
_MARKER = "CLAUDEX_SERVE_SMOKE_COMPLETE"
_SMALL_MARKER = "CLAUDEX_SERVE_SMOKE_SMALL_COMPLETE"
_SCRIPT = Path(__file__).resolve()


class SmokeError(Exception):
    pass


class _SyntheticProfile:
    async def access_token(self, *, refresh_if: str | None = None) -> str:
        return "synthetic-openai-token"


def _synthetic_response(request: httpx2.Request) -> httpx2.Response:
    body = json.loads(request.content)
    marker = {_UPSTREAM_MODEL: _MARKER, _SMALL_MODEL: _SMALL_MARKER}.get(body.get("model"))
    if (
        str(request.url) != "https://api.openai.com/v1/responses"
        or request.headers.get("Authorization") != "Bearer synthetic-openai-token"
        or marker is None
        or body.get("stream") is not True
        or body.get("store") is not False
        or body.get("reasoning", {}).get("effort") != "xhigh"
    ):
        return httpx2.Response(400, json={"error": {"code": "unexpected_smoke_request"}})
    event = {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": marker}]}],
            "usage": {"input_tokens": 1000, "output_tokens": 20},
        },
    }
    return httpx2.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        text=f"event: response.completed\ndata: {json.dumps(event)}\n\n",
    )


async def _gateway(port: int, binding: str | None) -> int:
    window = _WINDOW + int(os.environ.get("SYNTHETIC_CONTEXT_INCREMENT", "0"))
    route = ModelRoute(
        _UPSTREAM_MODEL,
        "Opus (gpt-synthetic)",
        window,
        {"high": "xhigh"},
    )
    smaller = ModelRoute(_SMALL_MODEL, "Astra (gpt-synthetic)", _SMALL_WINDOW, {"high": "xhigh"})
    launch = Launch(_ALIAS, {_ALIAS: route, _SMALL_ALIAS: smaller}, window, "synthetic")
    state_dir = Path(os.environ["CLAUDEX_STATE_DIR"]) if binding is not None else None
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(_synthetic_response)) as client:
        return await serve_gateway(
            client,
            _SyntheticProfile(),
            launch,
            "high",
            port=port,
            state_dir=state_dir,
            binding=binding,
        )


def _isolated_environment(root: Path) -> dict[str, str]:
    for directory in ("home", "config", "work", "temp files"):
        (root / directory).mkdir()
    (root / "config" / "settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
                    "ANTHROPIC_AUTH_TOKEN": "unrelated-synthetic-token",
                    "CLAUDE_CODE_EFFORT_LEVEL": "low",
                },
            }
        ),
        encoding="utf-8",
    )
    dead_proxy = "http://127.0.0.1:9"
    return {
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "home"),
        "CLAUDE_CONFIG_DIR": str(root / "config"),
        "CLAUDEX_STATE_DIR": str(root / "state"),
        "TMPDIR": str(root / "temp files"),
        "PATH": "/usr/bin:/bin",
        "CLAUDE_CODE_MAX_RETRIES": "0",
        "HTTP_PROXY": dead_proxy,
        "HTTPS_PROXY": dead_proxy,
        "ALL_PROXY": dead_proxy,
        "NO_PROXY": "example.invalid",
        "no_proxy": "other.example.invalid",
    }


async def _check_gateway(url: str, token: str) -> None:
    async with httpx2.AsyncClient(base_url=url, trust_env=False, timeout=5) as client:
        health = await client.get("/health/live")
        if health.status_code != 200 or health.json() != {"status": "ok"}:
            raise SmokeError("The standalone gateway did not pass its health check.")
        rejected = await client.get("/v1/models")
        if rejected.status_code != 401:
            raise SmokeError("The standalone gateway accepted a request without its credential.")
        models = await client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
        if models.status_code != 200 or [entry["id"] for entry in models.json()["data"]] != [
            _ALIAS,
            _SMALL_ALIAS,
        ]:
            raise SmokeError("The authenticated model list did not match the launch route.")


async def _claude_result(
    binary: str,
    arguments: list[str],
    env: dict[str, str],
    cwd: Path,
    prompt: str,
) -> dict[str, object]:
    process = await asyncio.create_subprocess_exec(
        binary,
        *arguments,
        "--strict-mcp-config",
        "--no-session-persistence",
        "--tools",
        "",
        "--output-format",
        "json",
        "-p",
        prompt,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(35):
            output, _ = await process.communicate()
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    if process.returncode != 0:
        raise SmokeError("Claude Code did not complete through the standalone gateway.")
    result = json.loads(output)
    if not isinstance(result, dict) or result.get("is_error"):
        raise SmokeError("Claude Code returned an unexpected standalone gateway result.")
    return result


def _check_context(context: dict[str, object], window: int = _WINDOW) -> None:
    result = context.get("result")
    if (
        not isinstance(result, str)
        or not re.search(rf"/ {window // 1000}k\b", result)
        or "Autocompact buffer" not in result
        or context.get("num_turns") != 0
        or context.get("duration_api_ms") != 0
    ):
        raise SmokeError("The standalone launch did not show the expected native context window.")


def _launch_details(url: str, command: str) -> tuple[list[str], Path, str]:
    address = urlsplit(url)
    if (
        address.scheme != "http"
        or address.hostname != "127.0.0.1"
        or not address.port
        or address.path
        or address.query
        or address.fragment
    ):
        raise SmokeError("The gateway did not report a loopback URL.")
    argv = shlex.split(command)
    if argv[:4] != ["claude", "--model", _ALIAS, "--settings"] or argv[5:] != [
        "--effort",
        "high",
    ]:
        raise SmokeError("The gateway reported an unexpected Claude Code command.")
    settings_path = Path(argv[4])
    if stat.S_IMODE(settings_path.stat().st_mode) != 0o600:
        raise SmokeError("The temporary Claude settings file is accessible to other users.")
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    token = settings["env"]["ANTHROPIC_AUTH_TOKEN"]
    if (
        not token
        or token in command
        or token in url
        or settings["env"]["ANTHROPIC_BASE_URL"] != url
    ):
        raise SmokeError("The gateway command or settings did not protect the local credential.")
    return argv[1:], settings_path, token


async def _stop_gateway(process: asyncio.subprocess.Process) -> bytes:
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
    try:
        async with asyncio.timeout(10):
            output, _ = await process.communicate()
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise SmokeError("The standalone gateway did not shut down on SIGTERM.") from None
    return output


async def _start_gateway(
    env: dict[str, str],
    cwd: Path,
    port: int,
    *,
    capture_errors: bool = False,
    binding: str | None = None,
) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        str(_SCRIPT),
        "--synthetic-gateway",
        str(port),
        *([binding] if binding is not None else []),
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE if capture_errors else asyncio.subprocess.DEVNULL,
    )


async def _check_occupied_port(url: str, env: dict[str, str], cwd: Path) -> None:
    port = urlsplit(url).port
    assert port is not None
    process = await _start_gateway(env, cwd, port, capture_errors=True)
    try:
        async with asyncio.timeout(10):
            output, errors = await process.communicate()
    finally:
        if process.returncode is None:
            await _stop_gateway(process)
    if process.returncode != 1 or output or b"is already in use" not in errors:
        raise SmokeError("The standalone gateway did not report that its port was occupied.")


async def _readiness(
    process: asyncio.subprocess.Process,
) -> tuple[str, list[str], Path, str]:
    assert process.stdout is not None
    async with asyncio.timeout(15):
        url = (await process.stdout.readline()).decode().strip()
        command = (await process.stdout.readline()).decode().strip()
    arguments, settings_path, token = _launch_details(url, command)
    return url, arguments, settings_path, token


async def _check_completion(
    binary: str, arguments: list[str], env: dict[str, str], cwd: Path, marker: str = _MARKER
) -> None:
    completion = await _claude_result(binary, arguments, env, cwd, "Run the synthetic check.")
    if completion.get("result") != marker:
        raise SmokeError("Claude Code did not complete the expected model and effort route.")


async def _check_ephemeral(binary: str, env: dict[str, str], cwd: Path) -> int:
    process = await _start_gateway(env, cwd, 0)
    try:
        url, arguments, settings_path, token = await _readiness(process)
        await _check_gateway(url, token)
        await _check_occupied_port(url, env, cwd)
        _check_context(await _claude_result(binary, arguments, env, cwd, "/context"))
        await _check_completion(binary, arguments, env, cwd)
    finally:
        output = await _stop_gateway(process)
    if process.returncode != 0 or settings_path.exists():
        raise SmokeError("SIGTERM did not stop the gateway and remove its temporary settings.")
    if token.encode() in output:
        raise SmokeError("The gateway exposed its credential in output during shutdown.")
    port = urlsplit(url).port
    assert port is not None
    return port


async def _configure(env: dict[str, str], cwd: Path, token: str, *arguments: str) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "claudex",
        *arguments,
        env=env,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(10):
            output, errors = await process.communicate()
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    if process.returncode != 0 or token.encode() in output + errors:
        raise SmokeError("The Claude settings command failed or exposed the gateway credential.")


async def _check_persistent(binary: str, env: dict[str, str], cwd: Path, port: int) -> None:
    settings_path = Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json"
    original = json.loads(settings_path.read_bytes())
    binding = "synthetic-registration"
    server = await _start_gateway(env, cwd, port, binding=binding)
    try:
        url, _, _, token = await _readiness(server)
        await _configure(env, cwd, token, "configure", "--port", str(port))
        if stat.S_IMODE(settings_path.stat().st_mode) != 0o600:
            raise SmokeError("Configured Claude settings are accessible to other users.")
        _check_context(await _claude_result(binary, [], env, cwd, "/context"))
        smaller = await _claude_result(binary, ["--model", _SMALL_ALIAS], env, cwd, "/context")
        _check_context(smaller, _SMALL_WINDOW)
        await _check_completion(binary, [], env, cwd)
        await _check_completion(
            binary, ["--model", _SMALL_ALIAS, "--effort", "high"], env, cwd, _SMALL_MARKER
        )
    finally:
        await _stop_gateway(server)
    saved = settings_path.read_bytes()
    server = await _start_gateway(env, cwd, port, binding=binding)
    try:
        restarted_url, _, _, restarted_token = await _readiness(server)
        if (restarted_url, restarted_token) != (url, token) or settings_path.read_bytes() != saved:
            raise SmokeError("The gateway URL, credential, or Claude settings changed on restart.")
        await _check_completion(binary, [], env, cwd)
    finally:
        await _stop_gateway(server)
    changed_env = {**env, "SYNTHETIC_CONTEXT_INCREMENT": "1000"}
    server = await _start_gateway(changed_env, cwd, port, binding=binding)
    try:
        changed_url, _, _, changed_token = await _readiness(server)
        await _check_replaced_credential(changed_url, token, changed_token)
    finally:
        await _stop_gateway(server)
    server = await _start_gateway(env, cwd, port, binding="another-synthetic-registration")
    try:
        replacement_url, _, _, replacement_token = await _readiness(server)
        await _check_replaced_credential(replacement_url, changed_token, replacement_token)
    finally:
        await _stop_gateway(server)
    await _configure(env, cwd, token, "unconfigure")
    if json.loads(settings_path.read_bytes()) != original:
        raise SmokeError("Unconfigure did not restore the original Claude settings.")


async def _check_replaced_credential(url: str, previous: str, current: str) -> None:
    async with httpx2.AsyncClient(base_url=url, trust_env=False, timeout=5) as client:
        rejected = await client.get("/v1/models", headers={"Authorization": f"Bearer {previous}"})
    if previous == current or rejected.status_code != 401:
        raise SmokeError("The gateway accepted an old credential after its configuration changed.")


async def _run(binary: str, root: Path) -> None:
    env = _isolated_environment(root)
    cwd = root / "work"
    port = await _check_ephemeral(binary, env, cwd)
    await _check_persistent(binary, env, cwd, port)


def main() -> int:
    if len(sys.argv) in (3, 4) and sys.argv[1] == "--synthetic-gateway":
        try:
            binding = sys.argv[3] if len(sys.argv) == 4 else None
            return asyncio.run(_gateway(int(sys.argv[2]), binding))
        except LaunchError as exc:
            raise SystemExit(str(exc)) from None
    binary = shutil.which(os.environ.get("CLAUDEX_CLAUDE_BIN", "claude"))
    if binary is None:
        raise SystemExit("Install Claude Code or set CLAUDEX_CLAUDE_BIN.")
    try:
        with TemporaryDirectory(prefix="claudex-serve-smoke-") as temporary:
            asyncio.run(_run(binary, Path(temporary)))
    except SmokeError as exc:
        raise SystemExit(str(exc)) from None
    except ValueError, KeyError, OSError, TimeoutError, httpx2.RequestError:
        raise SystemExit("The standalone smoke test could not complete.") from None
    print(
        "Standalone gateway checks passed, including plain Claude Code "
        "before and after restart and settings restoration."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
