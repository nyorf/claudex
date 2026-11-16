import asyncio
import errno
import json
import os
import secrets
import shlex
import signal
import socket
import subprocess  # nosec B404
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, nullcontext, suppress
from dataclasses import asdict, dataclass, field, replace
from functools import partial
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx2
import uvicorn
from anyio import to_thread

from claudex.api.gateway import create_gateway
from claudex.config.launch import Launch
from claudex.domain.accounts import TokenProvider
from claudex.infrastructure.gateway_settings import GatewayLaunch, GatewaySettingsStore

DEFAULT_PORT = 8765
_LOOPBACK = "127.0.0.1"
_BACKLOG = 2048


class LaunchError(Exception):
    pass


class _LocalServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # the launcher owns signals so it can clean up the temporary settings
        yield


@dataclass(frozen=True)
class _LocalGateway:
    url: str
    settings_path: Path
    token: str = field(repr=False)
    task: asyncio.Task[None] = field(repr=False)


@dataclass(frozen=True)
class _PersistentGateway:
    store: GatewaySettingsStore
    binding: str = field(repr=False)
    effort: str
    launch: GatewayLaunch | None = None
    reuse_only: bool = False
    previous_binding: str | None = field(default=None, repr=False)


def _terminate(child: asyncio.subprocess.Process) -> None:
    with suppress(ProcessLookupError):
        child.terminate()


def _register_child_signals(
    loop: asyncio.AbstractEventLoop, child: asyncio.subprocess.Process
) -> dict[signal.Signals, Any]:
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, lambda *_: None)
        signal.signal(signal.SIGTERM, lambda *_: loop.call_soon_threadsafe(_terminate, child))
    else:
        loop.add_signal_handler(signal.SIGINT, lambda: None)
        loop.add_signal_handler(signal.SIGTERM, _terminate, child)
    return previous


def _register_stop_signals(
    loop: asyncio.AbstractEventLoop, stop: asyncio.Event
) -> dict[signal.Signals, Any]:
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    for sig in previous:
        if sys.platform == "win32":
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
        else:
            loop.add_signal_handler(sig, stop.set)
    return previous


def _restore_signals(loop: asyncio.AbstractEventLoop, previous: dict[signal.Signals, Any]) -> None:
    for sig, handler in previous.items():
        if sys.platform != "win32":
            loop.remove_signal_handler(sig)
        signal.signal(sig, handler)


async def _wait_for_start(server: _LocalServer, task: asyncio.Task[None]) -> None:
    async with asyncio.timeout(10):
        while not server.started:
            if task.done():
                await task
                raise LaunchError("The local gateway stopped before startup.")
            await asyncio.sleep(0.02)


def _bind(listener: socket.socket, port: int, backlog: int) -> str:
    try:
        if sys.platform == "win32":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((_LOOPBACK, port))
        # listening claims the port before handing the socket to Uvicorn
        listener.listen(backlog)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise LaunchError(f"Port {port} is already in use on {_LOOPBACK}.") from exc
        raise LaunchError(f"Cannot bind the local gateway to {_LOOPBACK}.") from exc
    _, bound_port = listener.getsockname()
    return f"http://{_LOOPBACK}:{bound_port}"


def _gateway_binding(
    registration_id: str, launch: Launch, effort: str, *, legacy: bool = False
) -> str:
    # a stale Claude configuration must fail authentication when routing or context changes
    routes = {
        alias: asdict(replace(route, efforts=dict(route.efforts)))
        for alias, route in launch.routes.items()
    }
    configuration = {
        "registration_id": registration_id,
        "model": launch.model,
        "context_window": launch.context_window,
        "routes": routes,
        "effort": effort,
    }
    changed_ids = launch.desktop_models != {alias: alias for alias in launch.routes}
    if not legacy and (launch.aliases or launch.families or changed_ids):
        configuration.update(
            aliases=launch.aliases, families=launch.families, desktop_models=launch.desktop_models
        )
    canonical = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode()).hexdigest()


@asynccontextmanager
async def _local_gateway(
    client: httpx2.AsyncClient,
    provider: TokenProvider,
    launch: Launch,
    port: int,
    persistence: _PersistentGateway | None = None,
    stop: asyncio.Event | None = None,
) -> AsyncIterator[_LocalGateway]:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener,
        TemporaryDirectory(prefix="claudex-launch-") as settings_directory,
    ):
        base_url = _bind(listener, port, _BACKLOG)
        if persistence is not None and persistence.previous_binding is not None:
            await to_thread.run_sync(
                persistence.store.migrate_binding,
                port,
                persistence.previous_binding,
                persistence.binding,
            )
        gateway_token = (
            await to_thread.run_sync(
                partial(persistence.store.load, reuse_only=persistence.reuse_only),
                port,
                persistence.binding,
            )
            if persistence is not None
            else secrets.token_urlsafe(32)
        )
        app = create_gateway(
            launch.routes,
            client=client,
            token_provider=provider,
            gateway_token=gateway_token,
            stop=stop.set if stop is not None else None,
            aliases={
                **launch.aliases,
                **{wire: route for route, wire in launch.desktop_models.items()},
            },
            families=launch.families,
        )
        server = _LocalServer(
            uvicorn.Config(
                app,
                access_log=False,
                log_config=None,
                proxy_headers=False,
                timeout_graceful_shutdown=5,
                backlog=_BACKLOG,
            )
        )
        settings_path = Path(settings_directory) / "settings.json"
        if sys.platform == "win32":
            from claudex.infrastructure.windows_security import make_private

            await to_thread.run_sync(make_private, Path(settings_directory))
        # the highest local settings scope keeps a saved gateway token from replacing this one
        fd = os.open(settings_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            if sys.platform == "win32":
                await to_thread.run_sync(make_private, settings_path)
            json.dump(launch.settings(base_url, gateway_token, environ=os.environ), file)
        if persistence is not None:
            settings = launch.persistent_settings(
                base_url, gateway_token, persistence.effort, environ=os.environ
            )
            await to_thread.run_sync(
                partial(
                    persistence.store.publish,
                    effort_levels={
                        alias: tuple(route.efforts) for alias, route in launch.routes.items()
                    },
                    desktop_models=launch.desktop_models,
                    desktop_families=launch.desktop_families,
                ),
                port,
                persistence.binding,
                gateway_token,
                settings,
                persistence.launch,
            )
        gateway = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            try:
                await _wait_for_start(server, gateway)
            except TimeoutError as exc:
                raise LaunchError("The local gateway did not start in time.") from exc
            yield _LocalGateway(base_url, settings_path, gateway_token, gateway)
        finally:
            server.should_exit = True
            await gateway


async def run_claude(
    client: httpx2.AsyncClient,
    provider: TokenProvider,
    claude_bin: str,
    launch: Launch,
    effort: str | None,
    args: list[str],
) -> int:
    loop = asyncio.get_running_loop()
    async with _local_gateway(client, provider, launch, 0) as gateway:
        env = launch.environment(dict(os.environ), gateway.url, gateway.token)
        flags = launch.arguments(gateway.settings_path, effort, args)
        child = None
        signals: dict[signal.Signals, Any] = {}
        try:
            child = await asyncio.create_subprocess_exec(claude_bin, *flags, env=env)
            signals = _register_child_signals(loop, child)
            code = await child.wait()
            return 128 - code if code < 0 else code
        except FileNotFoundError as exc:
            raise LaunchError(
                "Claude Code was not found. Install it or set CLAUDEX_CLAUDE_BIN."
            ) from exc
        finally:
            if child is not None:
                _restore_signals(loop, signals)
                if child.returncode is None:
                    with suppress(ProcessLookupError):
                        child.kill()
                    await child.wait()


async def serve_gateway(
    client: httpx2.AsyncClient,
    provider: TokenProvider,
    launch: Launch,
    effort: str | None = None,
    *,
    port: int = DEFAULT_PORT,
    claude_bin: str = "claude",
    state_dir: Path | None = None,
    binding: str | None = None,
    restart: GatewayLaunch | None = None,
    expected_binding: str | None = None,
) -> int:
    if not 0 <= port <= 65535:
        raise LaunchError("The port must be between 0 and 65535.")
    selected_effort = launch.effective_effort(effort)
    persistence = None
    if state_dir is not None and binding:
        if port == 0:
            raise LaunchError("Saved gateway settings require a fixed port.")
        fingerprint = _gateway_binding(binding, launch, selected_effort)
        legacy_fingerprint = _gateway_binding(binding, launch, selected_effort, legacy=True)
        if expected_binding is not None and expected_binding not in (
            fingerprint,
            legacy_fingerprint,
        ):
            raise LaunchError(
                "The saved profile or routing changed. Start the gateway with claudex serve "
                "--profile PROFILE, then run claudex configure or claudex desktop again."
            )
        persistence = _PersistentGateway(
            store=GatewaySettingsStore(state_dir),
            binding=fingerprint,
            effort=selected_effort,
            launch=restart,
            reuse_only=expected_binding is not None,
            previous_binding=(
                expected_binding
                if expected_binding is not None and expected_binding != fingerprint
                else None
            ),
        )
    elif any(option is not None for option in (state_dir, binding, restart, expected_binding)):
        raise LaunchError("Saved gateway settings require a state directory and profile binding.")
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    signals = _register_stop_signals(loop, stop)
    try:
        instance = (
            persistence.store.running_instance() if persistence is not None else nullcontext()
        )
        with instance:
            async with _local_gateway(client, provider, launch, port, persistence, stop) as gateway:
                if stop.is_set():
                    return 0
                arguments = [claude_bin, *launch.arguments(gateway.settings_path, effort, [])]
                command = (
                    subprocess.list2cmdline(arguments)
                    if sys.platform == "win32"
                    else shlex.join(arguments)
                )
                print(gateway.url, command, sep="\n", flush=True)
                return await _wait_for_stop(gateway.task, stop)
    finally:
        _restore_signals(loop, signals)


async def _wait_for_stop(gateway: asyncio.Task[None], stop: asyncio.Event) -> int:
    stopping = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait((gateway, stopping), return_when=asyncio.FIRST_COMPLETED)
        if not stop.is_set():
            await gateway
            raise LaunchError("The local gateway stopped unexpectedly.")
        return 0
    finally:
        stopping.cancel()
        with suppress(asyncio.CancelledError):
            await stopping
