import argparse
import asyncio
import os
import re
import sys
from hashlib import sha256
from hmac import compare_digest
from importlib.metadata import distribution, version
from pathlib import Path

import httpx2
from pydantic import ValidationError

from claudex.config.launch import plan_launch
from claudex.config.models import ModelConfigError, default_models_yaml, load_model_map
from claudex.config.settings import Settings
from claudex.domain.accounts import AccountError, ReauthenticationRequiredError, TokenProvider
from claudex.infrastructure.catalog import CatalogError, CatalogModel, fetch_catalog
from claudex.infrastructure.desktop import (
    DesktopProfileSetupError,
    configure_desktop,
    launch_desktop,
)
from claudex.infrastructure.desktop_gateway import ensure_gateway, gateway_status, stop_gateway
from claudex.infrastructure.gateway_settings import (
    GatewayLaunch,
    GatewaySettingsError,
    GatewaySettingsStore,
    GatewaySnapshot,
    configure_claude,
    unconfigure_claude,
)
from claudex.infrastructure.terminal import launch_native, launch_terminal
from claudex.infrastructure.update import UpdateError, update_installation
from claudex.main import DEFAULT_PORT, LaunchError, serve_gateway
from claudex.observability.logging import configure_logging
from claudex.services.accounts import AccountManager

_USAGE = "https://chatgpt.com/settings/usage"
_REPOSITORY = "https://github.com/nyorf/claudex"
_DESKTOP_HELPER = "_desktop-credential"
_FINGERPRINT = re.compile(r"[a-f0-9]{64}\Z")


def _port(value: str) -> int:
    message = "The port must be an integer between 1 and 65535."
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(message) from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(message)
    return port


def _launch_options(command: argparse.ArgumentParser, *, prefix: str = "") -> None:
    command.add_argument(f"--{prefix}profile", dest="profile", help="Choose a ChatGPT profile")
    command.add_argument(f"--{prefix}model", dest="model", help="Choose a route or alias")
    command.add_argument(
        f"--{prefix}effort", dest="effort", choices=("low", "medium", "high", "xhigh", "max")
    )
    command.add_argument(
        f"--{prefix}map", type=Path, dest="model_map", help="Read a YAML model map"
    )


def _saved_port_option(
    command: argparse.ArgumentParser, *, prefix: str = "", default: int | None = DEFAULT_PORT
) -> None:
    command.add_argument(
        f"--{prefix}port",
        dest="port",
        type=_port,
        default=default,
        help=f"Saved gateway port (default {DEFAULT_PORT})",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claudex",
        description="Run Claude Code with inference from your ChatGPT plan.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {version('claudex')} ({_REPOSITORY})"
    )
    commands = parser.add_subparsers(dest="command")
    login = commands.add_parser("login", help="Continue with ChatGPT in your browser")
    login.add_argument("profile")
    for name, help_text in (
        ("use", "Select the default profile for new launches"),
        ("logout", "Sign a saved profile out"),
        ("remove", "Forget a saved profile"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("profile")
    commands.add_parser("profiles", help="List saved profile names")
    status = commands.add_parser("status", help="Show the proxy status and default profile")
    _saved_port_option(status, default=None)
    models = commands.add_parser("models", help="Show model routes or account availability")
    formats = models.add_mutually_exclusive_group()
    formats.add_argument("--example", action="store_true", help="Print the bundled YAML map")
    formats.add_argument("--available", action="store_true", help="Query the selected account")
    models.add_argument("--profile", help="Use this profile for the catalog request")
    models.add_argument("--map", type=Path, dest="model_map", help="Read this YAML model map")
    _saved_port_option(models, default=None)
    commands.add_parser("cli", aliases=["run"], help="Open Claude Code through Claudex")
    desktop = commands.add_parser("desktop", help="Open Claude Desktop through Claudex")
    serve = commands.add_parser("serve", help="Start the proxy in the background")
    for command in (desktop, serve):
        _launch_options(command)
        _saved_port_option(command, default=None)
        command.add_argument("--saved", action="store_true", help="Reuse the saved proxy options")
    serve.add_argument(
        "-f", "--foreground", action="store_true", help="Keep the proxy in this terminal"
    )
    stop = commands.add_parser("stop", help="Stop the running proxy")
    _saved_port_option(stop, default=None)
    commands.add_parser("update", help="Update a bundle installed by the Claudex installer")
    configure = commands.add_parser("configure", help="Connect plain Claude Code to Claudex")
    configure.add_argument(
        "target", choices=("desktop",), nargs="?", help="Configure the Claude Desktop Code tab"
    )
    _saved_port_option(configure)
    configure.add_argument(
        "--export", action="store_true", help="Export a Claude Desktop profile for manual import"
    )
    commands.add_parser("unconfigure", help="Restore Claude Code settings from before Claudex")
    commands.add_parser("license", help="Show the Claudex license")
    return parser


def _parse_args(argv: list[str]) -> argparse.Namespace:
    if argv[:1] not in (["cli"], ["run"]):
        return _parser().parse_args(argv)
    legacy = argv[0] == "run"
    prefix = "" if legacy else "proxy-"
    parser = argparse.ArgumentParser(prog=f"claudex {argv[0]}", add_help=legacy, allow_abbrev=False)
    _launch_options(parser, prefix=prefix)
    _saved_port_option(parser, prefix=prefix, default=None)
    parser.add_argument(f"--{prefix}saved", dest="saved", action="store_true")
    parser.add_argument("--proxy-help", action="help", help="Show Claudex proxy options")
    args, forwarded = parser.parse_known_args(argv[1:])
    args.command = "cli"
    args.claude_args = forwarded[1:] if forwarded[:1] == ["--"] else forwarded
    return args


async def _profile(accounts: AccountManager, requested: str | None) -> str:
    if requested is not None:
        return requested
    profile, _, known = await _default_profile(accounts)
    if profile is None:
        raise AccountError(_profile_hint(known))
    return profile


async def _default_profile(accounts: AccountManager) -> tuple[str | None, bool, list[str]]:
    selected = await accounts.selected()
    known = await accounts.profiles()
    if selected is not None:
        return selected, True, known
    return (known[0] if len(known) == 1 else None), False, known


def _profile_hint(known: list[str]) -> str:
    if not known:
        return "No profiles are saved. Run claudex login personal to sign in."
    return "No default profile is set. Run claudex profiles, then claudex use PROFILE."


async def _catalog(provider: TokenProvider, client: httpx2.AsyncClient) -> dict[str, CatalogModel]:
    token = await provider.access_token()
    return await fetch_catalog(client, token)


def _client() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(timeout=httpx2.Timeout(180, connect=15), trust_env=False)


def _overrides(args: argparse.Namespace) -> bool:
    return any(
        value is not None for value in (args.profile, args.model, args.effort, args.model_map)
    )


def _saved_snapshot(settings: Settings, port: int) -> GatewaySnapshot | None:
    try:
        return GatewaySettingsStore(settings.state_dir).snapshot(port)
    except GatewaySettingsError:
        return None


def _prompt_profile(known: list[str]) -> str:
    if not sys.stdin.isatty():
        raise AccountError(_profile_hint(known))
    if known:
        print(f"Saved ChatGPT profiles: {', '.join(known)}.", file=sys.stderr)
        prompt = "Enter a saved profile name or a new profile name: "
    else:
        prompt = "Choose a name for your ChatGPT profile [personal]: "
    try:
        print(prompt, end="", file=sys.stderr, flush=True)
        name = input().strip()
    except EOFError:
        raise AccountError(_profile_hint(known)) from None
    if known and not name:
        raise AccountError(_profile_hint(known))
    return name or "personal"


async def _require_login(accounts: AccountManager, profile: str, *, interactive: bool) -> None:
    try:
        await accounts.provider(profile).access_token()
    except ReauthenticationRequiredError:
        if not interactive or not sys.stdin.isatty():
            raise AccountError(f"Sign in with claudex login {profile}, then try again.") from None
        print(f"Continue to ChatGPT in your browser for profile {profile}.", file=sys.stderr)
        await accounts.login(profile)


async def _startup_options(
    args: argparse.Namespace, settings: Settings, existing: GatewayLaunch | None
) -> tuple[str, bool]:
    interactive = args.command in {"cli", "desktop"}
    async with _client() as client:
        accounts = AccountManager(settings.state_dir, client=client)
        default, _, known = await _default_profile(accounts)
        if args.saved and existing is None:
            raise GatewaySettingsError("No proxy launch is saved on this port. Run claudex serve.")
        resume = existing is not None and (
            args.saved or (not _overrides(args) and default == existing.profile)
        )
        requested = existing.profile if resume and existing is not None else args.profile or default
        choose = requested is None and interactive
        profile = _prompt_profile(known) if choose else requested
        if profile is None:
            raise AccountError(_profile_hint(known))
        await _require_login(accounts, profile, interactive=interactive)
        if choose or (interactive and not known):
            await accounts.select(profile)
        return profile, resume


async def _check_live_login(settings: Settings, profile: str) -> None:
    async with _client() as client:
        accounts = AccountManager(settings.state_dir, client=client)
        await _require_login(accounts, profile, interactive=True)


def _check_live_options(args: argparse.Namespace, recipe: GatewayLaunch) -> None:
    same = args.profile in (None, recipe.profile) and args.effort in (None, recipe.effort)
    if args.model_map is not None:
        same = same and args.model_map.absolute() == recipe.model_map
    if args.model is not None:
        model_map = load_model_map(recipe.model_map, required=recipe.require_model_map)
        same = same and model_map.resolve(args.model) == recipe.model
    if not same:
        raise GatewaySettingsError(
            f"The proxy uses different launch options. Run claudex stop --port {args.port} first."
        )


def _check_other_port(state: Path, port: int) -> None:
    for other in GatewaySettingsStore(state).ports():
        if other != port and gateway_status(state, other) == "running":
            raise GatewaySettingsError(
                f"The proxy already runs on port {other}. Use that port or run "
                f"claudex stop --port {other}."
            )


def _active_port(state: Path) -> int:
    for port in GatewaySettingsStore(state).ports():
        if gateway_status(state, port) == "running":
            return port
    return DEFAULT_PORT


def _acquire_proxy(args: argparse.Namespace, settings: Settings) -> GatewaySnapshot:
    port = args.port
    state = settings.state_dir
    if args.saved and _overrides(args):
        raise LaunchError("Use --saved without profile, model, effort, or map overrides.")
    status = gateway_status(state, port)
    if status != "stopped":
        snapshot = ensure_gateway(state, port, None)
        if snapshot.launch is None:
            raise GatewaySettingsError("Restart this proxy with claudex serve to save its options.")
        _check_live_options(args, snapshot.launch)
        if args.command in {"cli", "desktop"}:
            asyncio.run(_check_live_login(settings, snapshot.launch.profile))
        return snapshot

    _check_other_port(state, port)
    prior = _saved_snapshot(settings, port)
    profile, resume = asyncio.run(_startup_options(args, settings, prior.launch if prior else None))
    command = [*_desktop_helper_command(), "serve", "--foreground", "--port", str(port)]
    expected = None
    if resume and prior is not None:
        command.append("--saved")
        expected = prior.settings["env"]["ANTHROPIC_AUTH_TOKEN"]
    else:
        command.extend(("--profile", profile))
        for flag, value in (
            ("--model", args.model),
            ("--effort", args.effort),
            ("--map", args.model_map),
        ):
            if value is not None:
                command.extend((flag, str(value)))
    snapshot = ensure_gateway(state, port, tuple(command), expected_token=expected)
    if snapshot.launch is None or snapshot.launch.profile != profile:
        raise GatewaySettingsError("A proxy for a different profile started on this port.")
    _check_live_options(args, snapshot.launch)
    return snapshot


def _show_live_models(snapshot: GatewaySnapshot) -> None:
    recipe = snapshot.launch
    if recipe is None:
        raise GatewaySettingsError("This proxy has no saved model map.")
    print(f"Model map path  {recipe.model_map}")
    print(f"Running proxy profile  {recipe.profile}")
    settings = snapshot.settings
    maximum = int(settings["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"])
    for option in settings["modelPicker"]["options"]:
        name = option["model"]
        context = settings["modelSettings"][name]["autoCompactWindow"]
        window = context if isinstance(context, int) else maximum
        print(f"{name}  {option['label']}  {window:,} context tokens (running proxy)")


async def _models_or_gateway(
    args: argparse.Namespace,
    settings: Settings,
    accounts: AccountManager,
    client: httpx2.AsyncClient,
) -> int:
    if args.command == "models" and args.available:
        profile = await _profile(accounts, args.profile)
        catalog = await _catalog(accounts.provider(profile), client)
        for slug, model in catalog.items():
            window = model.context_window
            context = f"{window:,} context tokens" if window else "context window unreported"
            print(f"{slug}  {context}")
        return 0

    map_path = args.model_map or settings.config_dir / "models.yaml"
    if args.command == "models":
        if args.model_map is None and gateway_status(settings.state_dir, args.port) == "running":
            snapshot = GatewaySettingsStore(settings.state_dir).snapshot(args.port)
            if snapshot.launch and args.profile in (None, snapshot.launch.profile):
                _show_live_models(snapshot)
                return 0
        default, _, _ = await _default_profile(accounts)
        profile = args.profile or default
        model_map = load_model_map(map_path, required=args.model_map is not None)
        print(f"Model map path  {map_path}")
        if profile is not None:
            print(f"Profile  {profile}")
        elif any(model_map.context_window_overrides.values()):
            print("Use claudex models --profile PROFILE to show that profile's context overrides.")
        for name, route in model_map.routes.items():
            aliases = ", ".join(
                alias for alias, target in model_map.aliases.items() if target == name
            )
            suffix = f" [{aliases}]" if aliases else ""
            override = model_map.context_window_overrides.get(name, {}).get(profile or "")
            window = override if override is not None else route.context_window
            source = "profile override" if override is not None else "base YAML"
            print(f"{name}{suffix}  {route.label}  {window:,} context tokens ({source})")
        return 0

    model_map = load_model_map(map_path, required=args.model_map is not None)
    profile = await _profile(accounts, args.profile)
    provider = accounts.provider(profile)
    launch = plan_launch(model_map, args.model, await _catalog(provider, client), profile=profile)
    requested_effort = launch.effective_effort(args.effort)
    return await serve_gateway(
        client,
        provider,
        launch,
        args.effort,
        port=args.port,
        claude_bin=settings.claude_bin,
        state_dir=settings.state_dir,
        binding=provider.registration_id,
        restart=GatewayLaunch(
            profile, launch.model, requested_effort, map_path.absolute(), args.model_map is not None
        ),
    )


async def _serve_saved(
    settings: Settings, port: int, accounts: AccountManager, client: httpx2.AsyncClient
) -> int:
    snapshot = await asyncio.to_thread(GatewaySettingsStore(settings.state_dir).snapshot, port)
    recipe = snapshot.launch
    if recipe is None:
        raise GatewaySettingsError(
            "Start Claudex once with claudex serve to save the launch options."
        )
    provider = accounts.provider(recipe.profile)
    model_map = load_model_map(recipe.model_map, required=recipe.require_model_map)
    launch = plan_launch(
        model_map, recipe.model, await _catalog(provider, client), profile=recipe.profile
    )
    return await serve_gateway(
        client,
        provider,
        launch,
        recipe.effort,
        port=port,
        claude_bin=settings.claude_bin,
        state_dir=settings.state_dir,
        binding=provider.registration_id,
        expected_binding=snapshot.binding,
    )


async def _accounts(args: argparse.Namespace, accounts: AccountManager) -> None:
    match args.command:
        case "login":
            print("Continue with ChatGPT in your browser.", file=sys.stderr)
            await accounts.login(args.profile)
            print(f"Signed in to {args.profile}. Select it with claudex use {args.profile}.")
        case "use":
            await accounts.select(args.profile)
            print(f"Selected {args.profile} for new launches.")
        case "logout" | "remove":
            action = accounts.logout if args.command == "logout" else accounts.remove
            revoked = await action(args.profile)
            print(f"Cleared local credentials for {args.profile}.")
            if not revoked:
                print(f"Check remote access for this grant in ChatGPT Usage. {_USAGE}")
        case "profiles":
            selected = await accounts.selected()
            for profile in await accounts.profiles():
                print(f"{profile} (selected)" if profile == selected else profile)


async def _status(args: argparse.Namespace, settings: Settings, accounts: AccountManager) -> None:
    port = args.port
    status = gateway_status(settings.state_dir, port)
    print(f"Proxy  {status} (http://127.0.0.1:{port})")
    snapshot = _saved_snapshot(settings, port) if status == "running" else None
    if snapshot is not None and snapshot.launch is not None:
        print(f"Proxy profile  {snapshot.launch.profile}")
    profile, explicit, known = await _default_profile(accounts)
    if profile is None:
        print("Default profile  none")
        print(_profile_hint(known))
    elif explicit:
        print(f"Default profile  {profile} (selected)")
    else:
        print(f"Default profile  {profile} (only saved profile)")
        print(f"To select it explicitly, run claudex use {profile}.")
    print(f"ChatGPT Usage  {_USAGE}")


async def _dispatch(args: argparse.Namespace, settings: Settings) -> int:
    if args.command in {"configure", "unconfigure"}:
        return await asyncio.to_thread(_configure, args, settings)
    async with _client() as client:
        accounts = AccountManager(settings.state_dir, client=client)
        if args.command == "serve" and args.saved:
            if _overrides(args):
                raise LaunchError("Use --saved without profile, model, effort, or map overrides.")
            return await _serve_saved(settings, args.port, accounts, client)
        if args.command in {"models", "serve"}:
            return await _models_or_gateway(args, settings, accounts, client)
        if args.command in {None, "status"}:
            await _status(args, settings, accounts)
            return 0
        await _accounts(args, accounts)
        return 0


def _configure(args: argparse.Namespace, settings: Settings) -> int:
    if args.command == "configure" and args.target == "desktop":
        return _configure_desktop(settings.state_dir, args.port, export=args.export)
    if args.command == "configure" and args.export:
        raise GatewaySettingsError("Use --export with claudex configure desktop.")
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    try:
        claude_config_dir = Path(override).expanduser() if override else Path.home() / ".claude"
    except (OSError, RuntimeError) as exc:
        raise GatewaySettingsError("Cannot resolve the Claude Code settings directory.") from exc
    if args.command == "configure":
        gateway_settings = GatewaySettingsStore(settings.state_dir).settings(args.port)
        changed = configure_claude(claude_config_dir, gateway_settings)
        print("Claude Code now uses Claudex." if changed else "Claude Code already uses Claudex.")
    else:
        changed = unconfigure_claude(claude_config_dir)
        print(
            "Restored Claude Code settings." if changed else "Claude Code has no Claudex settings."
        )
    return 0


def _desktop_paths(state_dir: Path) -> tuple[Path, Path]:
    if sys.platform != "darwin":
        raise GatewaySettingsError("The Claudex Desktop integration is available on macOS.")
    try:
        return Path.home(), state_dir.expanduser().absolute()
    except OSError, RuntimeError:
        raise GatewaySettingsError("Could not resolve the Desktop configuration paths.") from None


def _open_desktop(args: argparse.Namespace, settings: Settings) -> int:
    home, state_dir = _desktop_paths(settings.state_dir)
    port = args.port
    published = _acquire_proxy(args, settings).settings
    try:
        launch_desktop(home, state_dir, port, published)
    except DesktopProfileSetupError:
        configure_desktop(home, state_dir, port, published, _desktop_helper_command())
        launch_desktop(home, state_dir, port, published)
    print("Opened Claude Desktop for Claudex. Start a session in the Code tab.")
    return 0


def _open_cli(args: argparse.Namespace, settings: Settings) -> int:
    snapshot = _acquire_proxy(args, settings)
    if snapshot.launch is not None:
        print(f"Claudex proxy profile  {snapshot.launch.profile}", file=sys.stderr)
    return launch_terminal(settings.claude_bin, snapshot, args.claude_args)


def _serve(args: argparse.Namespace, settings: Settings) -> int:
    if args.foreground and gateway_status(settings.state_dir, args.port) == "stopped":
        if args.saved and _overrides(args):
            raise LaunchError("Use --saved without profile, model, effort, or map overrides.")
        _check_other_port(settings.state_dir, args.port)
        prior = _saved_snapshot(settings, args.port)
        profile, resume = asyncio.run(
            _startup_options(args, settings, prior.launch if prior else None)
        )
        args.saved = resume
        if not resume:
            args.profile = profile
        return asyncio.run(_dispatch(args, settings))
    snapshot = _acquire_proxy(args, settings)
    label = f" with profile {snapshot.launch.profile}" if snapshot.launch else ""
    print(f"Proxy running at http://127.0.0.1:{args.port}{label}.")
    return 0


def _stop_for_update(settings: Settings) -> None:
    for port in GatewaySettingsStore(settings.state_dir).ports():
        if gateway_status(settings.state_dir, port) == "running":
            stop_gateway(settings.state_dir, port)


def _configure_desktop(state_dir: Path, port: int, *, export: bool) -> int:
    home, state_dir = _desktop_paths(state_dir)
    published = GatewaySettingsStore(state_dir).settings(port)
    result = configure_desktop(
        home, state_dir, port, published, _desktop_helper_command(), export=export
    )
    if isinstance(result, Path):
        print(result)
        return 0
    port_option = f" --port {port}" if port != DEFAULT_PORT else ""
    try:
        launch_desktop(home, state_dir, port, published)
    except GatewaySettingsError as exc:
        if result:
            raise GatewaySettingsError(
                f"Saved the Claude Desktop profile. {exc} Retry with claudex desktop{port_option}."
            ) from None
        raise
    print("Opened Claude Desktop for Claudex. Start a session in the Code tab.")
    print(f"Open future sessions with claudex desktop{port_option}.")
    return 0


def _desktop_helper_command() -> tuple[str, ...]:
    executable = Path(sys.executable)
    if not executable.is_absolute() or not executable.is_file():
        raise GatewaySettingsError("Claudex needs an installed executable to configure Desktop.")
    # resolving a virtualenv interpreter symlink would lose its installed packages
    return (
        (str(executable),)
        if getattr(sys, "frozen", False)
        else (str(executable), "-I", "-m", "claudex")
    )


class _CredentialParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise GatewaySettingsError("The Desktop gateway credential request is invalid.")


def _desktop_credential(args: list[str]) -> int:
    parser = _CredentialParser(
        prog=f"claudex {_DESKTOP_HELPER}", add_help=False, allow_abbrev=False
    )
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--port", type=_port, required=True)
    parser.add_argument("--fingerprint", required=True)
    try:
        parsed = parser.parse_args(args)
        token = _saved_desktop_credential(parsed.state_dir, parsed.port, parsed.fingerprint)
    except GatewaySettingsError:
        print(
            "Desktop gateway credential is unavailable. Configure Claude Desktop again.",
            file=sys.stderr,
        )
        return 1
    print(token)
    return 0


def _saved_desktop_credential(state_dir: Path, port: int, fingerprint: str) -> str:
    if not state_dir.is_absolute() or _FINGERPRINT.fullmatch(fingerprint) is None:
        raise GatewaySettingsError("The Desktop gateway credential request is invalid.")
    settings = GatewaySettingsStore(state_dir).settings(port)
    token = settings["env"]["ANTHROPIC_AUTH_TOKEN"]
    if not compare_digest(sha256(token.encode("ascii")).hexdigest(), fingerprint):
        raise GatewaySettingsError("The gateway credential was replaced.")
    return token


def _show_license() -> int:
    license_text = distribution("claudex").read_text("licenses/LICENSE")
    if license_text is None:
        source_root = Path(__file__).resolve().parent.parent
        if source_root.name == "src" and not getattr(sys, "frozen", False):
            try:
                license_text = (source_root.parent / "LICENSE").read_text(encoding="utf-8")
            except OSError as exc:
                raise LaunchError("Could not read the Claudex license file.") from exc
    if license_text is None:
        raise LaunchError("The Claudex license file is missing. Reinstall Claudex.")
    sys.stdout.write(license_text)
    return 0


def _execute(args: argparse.Namespace, settings: Settings) -> int:
    if args.command == "cli" and args.claude_args in (
        ["--help"],
        ["-h"],
        ["--version"],
        ["-v"],
    ):
        return launch_native(settings.claude_bin, args.claude_args)
    if args.command == "models" and args.example:
        print(default_models_yaml(), end="")
        return 0
    if args.command in {None, "status", "models", "serve", "stop", "cli", "desktop"}:
        args.port = getattr(args, "port", None) or _active_port(settings.state_dir)
    match args.command:
        case "desktop":
            return _open_desktop(args, settings)
        case "cli":
            return _open_cli(args, settings)
        case "serve":
            return _serve(args, settings)
        case "stop":
            stopped = stop_gateway(settings.state_dir, args.port)
            print("Proxy stopped." if stopped else "No proxy is running on this port.")
            return 0
        case "update":
            return update_installation(lambda: _stop_for_update(settings))
        case _:
            return asyncio.run(_dispatch(args, settings))


def main() -> int:
    if sys.argv[1:2] == [_DESKTOP_HELPER]:
        return _desktop_credential(sys.argv[2:])
    if sys.platform == "linux" and getattr(sys, "frozen", False):
        # external programs need the library search path from before the bundle started
        original = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
        if original is None:
            os.environ.pop("LD_LIBRARY_PATH", None)
        else:
            os.environ["LD_LIBRARY_PATH"] = original
    args = _parse_args(sys.argv[1:])
    configure_logging()
    try:
        if args.command == "license":
            return _show_license()
        return _execute(args, Settings())
    except (
        AccountError,
        CatalogError,
        GatewaySettingsError,
        LaunchError,
        ModelConfigError,
        UpdateError,
    ) as exc:
        print(f"Claudex error. {exc}", file=sys.stderr)
        return 1
    except ValidationError:
        print(
            "Claudex settings are invalid. Check CLAUDEX_ environment variables.", file=sys.stderr
        )
        return 1
    except KeyboardInterrupt:
        return 130
