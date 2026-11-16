import json
import os
import plistlib
import re
import shlex
import stat
import subprocess  # nosec B404
from collections.abc import Callable, Mapping
from contextlib import suppress
from hashlib import sha256
from hmac import compare_digest
from pathlib import Path
from tempfile import NamedTemporaryFile

from claudex.infrastructure.gateway_settings import (
    GatewaySettingsError,
    Settings,
    _file_info,
    _locked,
    _open_regular,
    _port_filename,
    _read,
    _write,
)

_APP_ID = "com.anthropic.claudefordesktop"
_APP_PATH = Path("/Applications/Claude.app")
_SUPPORTED_BUILD = "5.11492.5"
_PROFILE_ID = "998ff74b-3a8c-5d89-a510-fdb6e200b833"
_PROFILE_NAMES = ("Claudex", "Claudex (managed)")
_UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")
_HELPER_NAME = re.compile(r"credential-[0-9]+-[0-9a-f]{64}\.sh")
_CONTEXT_ENV = "CLAUDE_CODE_MAX_CONTEXT_TOKENS"
_COMPACTION_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_MODEL_RECEIPT = f".claudex-models-{_PROFILE_ID}.json"
_PROFILE_CONFLICTS = {
    "bootstrapUrl",
    "deploymentMode",
    "forceLoginOrgUUID",
    "inferenceCredentialHelperArgs",
    "inferenceGatewayApiKey",
    "inferenceGatewayManagedClaudeCode",
}


class DesktopProfileSetupError(GatewaySettingsError):
    pass


def configure_desktop(
    home: Path,
    state_dir: Path,
    port: int,
    gateway_settings: Settings,
    helper_command: tuple[str, ...],
    *,
    export: bool = False,
    effort_levels: Mapping[str, tuple[str, ...]] | None = None,
) -> bool | Path:
    if not home.is_absolute() or not state_dir.is_absolute():
        raise GatewaySettingsError("Claude Desktop configuration requires absolute paths.")
    versioned_port = _port_filename(port)
    url, token, effort = _gateway_values(gateway_settings, port)
    if not export:
        _desktop_window(gateway_settings)
    support = home / "Library" / "Application Support"
    try:
        state_dir = state_dir.resolve()
        helper_dir = state_dir / "desktop"
        script = _helper_script(helper_command, state_dir, port, token)
        helper = helper_dir / f"credential-{port}-{sha256(script).hexdigest()}.sh"
        profile: Settings = {
            "inferenceProvider": "gateway",
            "inferenceCredentialKind": "helper-script",
            "inferenceGatewayAuthScheme": "bearer",
            "inferenceCredentialHelper": str(helper),
            "inferenceGatewayBaseUrl": url,
            "modelDiscoveryEnabled": True,
            "defaultModelEffort": effort,
        }
        if effort_levels is not None:
            profile["modelDiscoveryEnabled"] = False
            profile["inferenceModels"] = _desktop_models(gateway_settings, effort_levels)
        if not export:
            _check_directory(home)
            _check_app()
            _check_desktop_policy(support)
        _prepare_helper_directory(helper_dir)
        with _locked(helper_dir, "desktop.lock", private=True):
            if export:
                path = helper_dir / f"claudex-{versioned_port}"
                _write_helper(helper, script)
                _write_private_json(path, profile)
                return path
            root = support / "Claude-3p"
            with _locked(root, ".claudex-desktop.lock"):
                return _activate_profile(root, helper, script, profile)
    except OSError:
        raise GatewaySettingsError("Claudex could not access the Claude Desktop files.") from None


def launch_desktop(
    home: Path,
    state_dir: Path,
    port: int,
    gateway_settings: Settings,
    *,
    before_open: Callable[[], None] | None = None,
    effort_levels: Mapping[str, tuple[str, ...]] | None = None,
) -> None:
    if not home.is_absolute() or not state_dir.is_absolute():
        raise GatewaySettingsError("Claude Desktop configuration requires absolute paths.")
    _port_filename(port)
    url, token, _ = _gateway_values(gateway_settings, port)
    window = _desktop_window(gateway_settings)
    support = home / "Library" / "Application Support"
    try:
        _check_directory(home)
        _check_desktop_policy(support)
        root = support / "Claude-3p"
        expected = (
            None if effort_levels is None else _desktop_models(gateway_settings, effort_levels)
        )
        with _locked(root, ".claudex-desktop.lock"):
            _check_app(command="claudex desktop")
            _check_active_profile(root, state_dir.resolve(), port, url, token, expected)
            if before_open is not None:
                before_open()
            _open_desktop(window)
    except OSError:
        raise GatewaySettingsError("Claudex could not access the Claude Desktop files.") from None


def _desktop_window(settings: Settings) -> int:
    invalid = "Desktop context settings are incomplete or invalid. Restart the Claudex gateway."
    available = settings.get("availableModels")
    models = settings.get("modelSettings")
    if not isinstance(available, list) or not available or not isinstance(models, dict):
        raise GatewaySettingsError(invalid)
    env = settings.get("env")
    setting = env.get(_CONTEXT_ENV) if isinstance(env, dict) else None
    if not isinstance(setting, str) or not re.fullmatch(r"[1-9][0-9]{0,6}", setting):
        raise GatewaySettingsError(invalid)
    maximum = int(setting)
    if maximum > 2_000_000:
        raise GatewaySettingsError(invalid)
    windows: list[int] = []
    for model in available:
        fields = models.get(model) if isinstance(model, str) else None
        window = fields.get("autoCompactWindow") if isinstance(fields, dict) else None
        if window == "auto":
            window = maximum
        if type(window) is not int or not 1 <= window <= maximum:
            raise GatewaySettingsError(invalid)
        windows.append(window)
    # desktop code sessions share one process-wide context setting
    smallest = min(windows)
    if not 100_000 <= smallest <= 1_000_000:
        raise GatewaySettingsError(
            "Claude Desktop needs a shared context window from 100,000 to 1,000,000 tokens. "
            "Choose a compatible model map and restart the Claudex gateway."
        )
    return smallest


def _desktop_models(
    settings: Settings, effort_levels: Mapping[str, tuple[str, ...]]
) -> list[dict[str, str]]:
    available = settings.get("availableModels")
    picker = settings.get("modelPicker")
    options = picker.get("options") if isinstance(picker, dict) else None
    incomplete = "The gateway model list is incomplete. Restart the Claudex gateway."
    if not isinstance(available, list) or not available or not isinstance(options, list):
        raise GatewaySettingsError(incomplete)
    labels = {
        option["model"]: option["label"]
        for option in options
        if isinstance(option, dict)
        and isinstance(option.get("model"), str)
        and isinstance(option.get("label"), str)
    }
    result: list[dict[str, str]] = []
    for model in available:
        if not isinstance(model, str) or model not in labels or model not in effort_levels:
            raise GatewaySettingsError(incomplete)
        levels = effort_levels[model]
        if (
            not levels
            or len(set(levels)) != len(levels)
            or any(level not in _EFFORTS for level in levels)
        ):
            raise GatewaySettingsError(
                "The gateway effort levels are invalid. Restart the Claudex gateway."
            )
        row = {"name": model, "labelOverride": labels[model]}
        if set(levels) == set(_EFFORTS[: len(levels)]):
            row["behavesAs"] = "claude-sonnet-5"
            if len(levels) < len(_EFFORTS):
                row["maxEffort"] = _EFFORTS[len(levels) - 1]
        result.append(row)
    return result


def _check_active_profile(
    root: Path,
    state_dir: Path,
    port: int,
    url: str,
    token: str,
    expected: list[dict[str, str]] | None,
) -> None:
    unavailable = "The matching Claudex profile is inactive. Run claudex configure desktop."
    library = root / "configLibrary"
    _check_directory(library)
    mode = _read(root / "claude_desktop_config.json") or {}
    if mode.get("enterpriseConfig") is not None:
        raise GatewaySettingsError("A legacy enterprise configuration controls Claude Desktop.")
    meta = _read(library / "_meta.json") or {}
    if meta.get("hybridPointer") is not None:
        raise GatewaySettingsError("An organization connection controls Claude Desktop.")
    if mode.get("deploymentMode") != "3p" or meta.get("appliedId") != _PROFILE_ID:
        raise DesktopProfileSetupError(unavailable)
    _applied_profile(meta, library)
    profile = _read(library / f"{_PROFILE_ID}.json", private=True) or {}
    helper_setting = profile.get("inferenceCredentialHelper")
    if (
        profile.get("inferenceGatewayBaseUrl") != url
        or profile.get("inferenceGatewayAuthScheme") != "bearer"
        or not isinstance(helper_setting, str)
    ):
        raise DesktopProfileSetupError(unavailable)
    _check_model_ownership(root, profile)
    _merge_profile(profile, profile)
    if (expected is None and profile.get("modelDiscoveryEnabled") is not True) or (
        expected is not None
        and (
            profile.get("modelDiscoveryEnabled") is not False
            or profile.get("inferenceModels") != expected
        )
    ):
        raise DesktopProfileSetupError(unavailable)
    helper = Path(helper_setting)
    directory = state_dir / "desktop"
    if helper.parent != directory:
        raise DesktopProfileSetupError(unavailable)
    _check_private_helper_directory(directory)
    _check_helper_script(helper, state_dir, port, token)


def _check_helper_script(path: Path, state_dir: Path, port: int, token: str) -> None:
    stale = "The Desktop credential helper no longer matches. Run claudex configure desktop."
    info = _file_info(path)
    if info is None or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise DesktopProfileSetupError(stale)
    with open(path, "rb", opener=_open_regular) as source:
        script = source.read(16_385)
    if len(script) > 16_384 or path.name != f"credential-{port}-{sha256(script).hexdigest()}.sh":
        raise DesktopProfileSetupError(stale)
    try:
        _, invocation = script.decode().split("\n", 1)
        args = shlex.split(invocation)
    except ValueError:
        raise DesktopProfileSetupError(stale) from None
    prefix = tuple(args[1:-7])
    if not prefix or prefix[1:] not in ((), ("-I", "-m", "claudex")):
        raise DesktopProfileSetupError(stale)
    expected = _helper_script(prefix, state_dir, port, token)
    if not compare_digest(script, expected):
        raise DesktopProfileSetupError(stale)
    executable = Path(prefix[0])
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise DesktopProfileSetupError(stale)


def _open_desktop(window: int) -> None:
    try:
        subprocess.run(  # noqa: S603  # nosec B603
            [
                "/usr/bin/open",
                "-a",
                str(_APP_PATH),
                "--env",
                f"{_CONTEXT_ENV}={window}",
                "--env",
                f"{_COMPACTION_ENV}={window}",
            ],
            env={},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=True,
        )
    except OSError, subprocess.SubprocessError:
        raise GatewaySettingsError(
            "Could not open Claude Desktop. Check the installation and try claudex desktop again."
        ) from None


def _gateway_values(settings: Settings, port: int) -> tuple[str, str, str]:
    env = settings.get("env")
    url = f"http://127.0.0.1:{port}"
    if not isinstance(env, dict) or env.get("ANTHROPIC_BASE_URL") != url:
        raise GatewaySettingsError("The gateway settings do not match the Desktop port.")
    token = env.get("ANTHROPIC_AUTH_TOKEN")
    if not isinstance(token, str) or re.fullmatch(r"[a-zA-Z0-9_-]{40,}", token) is None:
        raise GatewaySettingsError("The gateway credential is invalid.")
    model = settings.get("model")
    if not isinstance(model, str) or not model:
        raise GatewaySettingsError("The gateway has no selected model.")
    # desktop discovers its initial model from the gateway list order
    available = settings.get("availableModels")
    if isinstance(available, list) and available and isinstance(available[0], str):
        model = available[0]
    models = settings.get("modelSettings")
    default_model = models.get(model) if isinstance(models, dict) else None
    effort = env.get("CLAUDE_CODE_EFFORT_LEVEL")
    if effort != "max" and isinstance(default_model, dict):
        # launch settings omit a per-model effort when its configured value is max
        effort = default_model.get("effortLevel", "max")
    if not isinstance(effort, str) or effort not in ("low", "medium", "high", "xhigh", "max"):
        raise GatewaySettingsError("The gateway has no supported Desktop effort.")
    return url, token, effort


def _helper_script(command: tuple[str, ...], state_dir: Path, port: int, token: str) -> bytes:
    if not command or not Path(command[0]).is_absolute():
        raise GatewaySettingsError("The Desktop credential helper requires an absolute executable.")
    args = (
        *command,
        "_desktop-credential",
        "--state-dir",
        str(state_dir),
        "--port",
        str(port),
        "--fingerprint",
        sha256(token.encode("ascii")).hexdigest(),
    )
    return f"#!/bin/sh\nexec {shlex.join(args)}\n".encode()


def _write_helper(path: Path, script: bytes) -> bool:
    if (info := _file_info(path)) is not None:
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise GatewaySettingsError(
                "The Desktop credential helper must be private and executable."
            )
        with open(path, "rb", opener=_open_regular) as source:
            if source.read(len(script) + 1) != script:
                raise GatewaySettingsError("The Desktop credential helper changed.")
        return False
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(script)
            os.fchmod(file.fileno(), 0o700)
            file.flush()
            os.fsync(file.fileno())
        _file_info(path)
        temporary.replace(path)
        return True
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _write_private_json(path: Path, data: Settings) -> bool:
    current = _read(path)
    info = _file_info(path)
    if current == data and info is not None and stat.S_IMODE(info.st_mode) == 0o600:
        return False
    _write(path, data)
    return True


def _app_metadata() -> object:
    path = _APP_PATH / "Contents" / "Info.plist"
    try:
        with path.open("rb") as source:
            return plistlib.load(source)
    except OSError, ValueError, plistlib.InvalidFileException:
        return None


def _desktop_running() -> bool:
    message = "Claudex could not check whether Claude Desktop is open."
    try:
        probe = subprocess.run(  # noqa: S603  # nosec B603
            ["/usr/bin/pgrep", "-x", "Claude"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        raise GatewaySettingsError(message) from None
    if probe.returncode not in (0, 1):
        raise GatewaySettingsError(message)
    return probe.returncode == 0


def _check_app(*, command: str = "claudex configure desktop") -> None:
    app = _app_metadata()
    if (
        not isinstance(app, dict)
        or app.get("CFBundleIdentifier") != _APP_ID
        or app.get("CFBundleVersion") != _SUPPORTED_BUILD
    ):
        raise GatewaySettingsError(
            "This Claude Desktop build has not been verified for direct configuration. "
            "Use claudex configure desktop --export and import the profile in Claude Desktop."
        )
    if _desktop_running():
        raise GatewaySettingsError(f"Quit Claude Desktop, then run {command} again.")


def _managed_preferences() -> tuple[Path, Path]:
    from pwd import getpwuid

    directory = Path("/Library/Managed Preferences")
    try:
        user = getpwuid(os.getuid()).pw_name
    except KeyError:
        raise GatewaySettingsError("Claudex could not check managed preferences.") from None
    return directory / f"{_APP_ID}.plist", directory / user / f"{_APP_ID}.plist"


def _check_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        raise GatewaySettingsError("Claudex needs a regular directory for Claude Desktop settings.")


def _prepare_helper_directory(directory: Path) -> None:
    _check_helper_ancestors(directory.parent)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _check_private_helper_directory(directory)


def _check_private_helper_directory(directory: Path) -> None:
    _check_helper_ancestors(directory)
    info = directory.lstat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise GatewaySettingsError("The Desktop credential directory must be private.")


def _check_helper_ancestors(directory: Path) -> None:
    owners = {0, os.getuid()}
    for path in (*reversed(directory.parents), directory):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        shared = info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        sticky_system_directory = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in owners:
            raise GatewaySettingsError("The Desktop helper needs regular, trusted directories.")
        if shared and not sticky_system_directory:
            raise GatewaySettingsError("The Desktop helper directory must prevent shared writes.")


def _check_desktop_policy(support: Path) -> None:
    _check_directory(support.parent)
    _check_directory(support)
    for name in ("Claude", "Claude-3p"):
        directory = support / name
        _check_directory(directory)
        lock = _file_info(directory / "saved-org-lock.json")
        if lock is not None and lock.st_size:
            raise GatewaySettingsError("An organization lock controls Claude Desktop.")
    for path in _managed_preferences():
        _check_directory(path.parent)
        if _file_info(path) is not None:
            raise GatewaySettingsError("Managed preferences control Claude Desktop.")


def _activate_profile(root: Path, helper: Path, script: bytes, profile: Settings) -> bool:
    library = root / "configLibrary"
    _check_directory(library)
    meta_path = library / "_meta.json"
    mode_path = root / "claude_desktop_config.json"
    profile_path = library / f"{_PROFILE_ID}.json"
    old_mode = _read(mode_path) or {}
    if old_mode.get("enterpriseConfig") is not None:
        raise GatewaySettingsError("A legacy enterprise configuration controls Claude Desktop.")
    if old_mode.get("deploymentMode") not in (None, "1p", "3p"):
        raise GatewaySettingsError("The Claude Desktop deployment mode is unsupported.")
    old_meta = _read(meta_path)
    meta = _applied_profile(old_meta, library)
    current = _read(profile_path)
    _check_model_ownership(root, current)
    profile = _merge_profile(current, profile)
    library.mkdir(mode=0o700, parents=True, exist_ok=True)
    helper_changed = _write_helper(helper, script)
    if "inferenceModels" in profile:
        old_digest = _model_digest(current.get("inferenceModels") if current else None)
        new_digest = _model_digest(profile["inferenceModels"])
        _write_private_json(
            root / _MODEL_RECEIPT, {"digests": list(dict.fromkeys((old_digest, new_digest)))}
        )
    profile_changed = _write_private_json(profile_path, profile)
    if "inferenceModels" in profile:
        _write_private_json(root / _MODEL_RECEIPT, {"digests": [new_digest]})
    elif current and "inferenceModels" in current:
        (root / _MODEL_RECEIPT).unlink(missing_ok=True)
    if old_meta != meta:
        _write(meta_path, meta)
    if old_mode.get("deploymentMode") != "3p":
        _write(mode_path, {**old_mode, "deploymentMode": "3p"})
        return True
    return helper_changed or profile_changed or old_meta != meta


def _model_digest(models: object) -> str:
    return sha256(json.dumps(models, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _check_model_ownership(root: Path, current: Settings | None) -> None:
    if current is None or "inferenceModels" not in current:
        return
    path = root / _MODEL_RECEIPT
    info = _file_info(path, private=True)
    receipt = _read(path, private=True) if info is not None and info.st_uid == os.getuid() else None
    digests = receipt.get("digests") if receipt else None
    if not isinstance(digests, list) or _model_digest(current["inferenceModels"]) not in digests:
        raise GatewaySettingsError(
            "The Claudex Desktop profile has conflicting custom settings. "
            "Keep it and use claudex configure desktop --export to create a separate profile."
        )


def _merge_profile(current: Settings | None, profile: Settings) -> Settings:
    if current is None:
        return profile
    prior_helper = current.get("inferenceCredentialHelper")
    if (
        current.get("inferenceProvider") != "gateway"
        or current.get("inferenceCredentialKind") != "helper-script"
        or not isinstance(prior_helper, str)
        or _HELPER_NAME.fullmatch(Path(prior_helper).name) is None
    ):
        raise GatewaySettingsError("The reserved Claudex profile is already in use.")
    conflicts = any(
        key in _PROFILE_CONFLICTS or key.startswith(("inferenceGatewayOidc", "inferenceIdp"))
        for key in current
    )
    if "inferenceCustomHeaders" in current:
        conflicts = conflicts or any(
            current.get(key) != profile[key]
            for key in ("inferenceGatewayBaseUrl", "inferenceCredentialHelper")
        )
    if conflicts:
        raise GatewaySettingsError(
            "The Claudex Desktop profile has conflicting custom settings. "
            "Keep it and use claudex configure desktop --export to create a separate profile."
        )
    merged = {**current, **profile}
    if "inferenceModels" not in profile:
        merged.pop("inferenceModels", None)
    return merged


def _applied_profile(meta: Settings | None, library: Path) -> Settings:
    meta = {"entries": []} if meta is None else meta
    if meta.get("hybridPointer") is not None:
        raise GatewaySettingsError("An organization connection controls Claude Desktop.")
    entries = meta.get("entries")
    if not isinstance(entries, list):
        raise GatewaySettingsError("The Claude Desktop profile list is invalid.")
    names: set[str] = set()
    identifiers: set[str] = set()
    managed = False
    for entry in entries:
        identifier = entry.get("id") if isinstance(entry, dict) else None
        name = entry.get("name") if isinstance(entry, dict) else None
        if (
            not isinstance(identifier, str)
            or _UUID.fullmatch(identifier) is None
            or not isinstance(name, str)
            or not name.strip()
            or identifier in identifiers
        ):
            raise GatewaySettingsError("The Claude Desktop profile list is invalid.")
        identifiers.add(identifier)
        if identifier == _PROFILE_ID:
            if name not in _PROFILE_NAMES:
                raise GatewaySettingsError("The reserved Claudex profile is already in use.")
            managed = True
        else:
            names.add(name.strip().casefold())
    active = meta.get("appliedId")
    if active not in (None, "") and (
        not isinstance(active, str)
        or active not in identifiers
        or _file_info(library / f"{active}.json") is None
    ):
        raise GatewaySettingsError("The active Claude Desktop profile is invalid.")
    if not managed:
        name = next((name for name in _PROFILE_NAMES if name.casefold() not in names), None)
        if name is None:
            raise GatewaySettingsError("The Claudex Desktop profile names are already in use.")
        entries = [*entries, {"id": _PROFILE_ID, "name": name}]
    return {**meta, "entries": entries, "appliedId": _PROFILE_ID}
