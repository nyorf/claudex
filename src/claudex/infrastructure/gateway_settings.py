import json
import os
import re
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass
from hmac import compare_digest
from pathlib import Path
from secrets import token_urlsafe
from tempfile import NamedTemporaryFile
from typing import Any, NamedTuple

from filelock import FileLock, Timeout

type Settings = dict[str, Any]
_PATH_SEGMENT = re.compile(r"[a-zA-Z0-9_-]{1,100}\Z")


class GatewaySettingsError(Exception):
    pass


class GatewayNotPublishedError(GatewaySettingsError):
    pass


@dataclass(frozen=True)
class GatewayLaunch:
    profile: str
    model: str
    effort: str
    model_map: Path
    require_model_map: bool

    def record(self) -> Settings:
        return {
            "profile": self.profile,
            "model": self.model,
            "effort": self.effort,
            "model_map": str(self.model_map),
            "require_model_map": self.require_model_map,
        }

    @classmethod
    def parse(cls, value: object) -> GatewayLaunch:
        if isinstance(value, dict):
            profile, model, effort, model_map, required = (
                value.get(name)
                for name in ("profile", "model", "effort", "model_map", "require_model_map")
            )
            if (
                isinstance(profile, str)
                and profile
                and isinstance(model, str)
                and model
                and isinstance(effort, str)
                and effort
                and isinstance(model_map, str)
                and Path(model_map).is_absolute()
                and isinstance(required, bool)
            ):
                return cls(profile, model, effort, Path(model_map), required)
        raise GatewaySettingsError("Saved launch options are invalid. Run claudex serve again.")


class GatewaySnapshot(NamedTuple):
    settings: Settings
    binding: str
    launch: GatewayLaunch | None


@contextmanager
def _locked(directory: Path, filename: str, *, private: bool = False) -> Iterator[None]:
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or _is_reparse_point(info)
            or (private and sys.platform != "win32" and stat.S_IMODE(info.st_mode) != 0o700)
        ):
            raise GatewaySettingsError("Claudex needs a regular directory with private access.")
        if private and sys.platform == "win32":
            _protect_windows_path(directory)
        lock_path = directory / filename
        # filelock opens and truncates the lock file, so reject unsafe existing entries first
        _file_info(lock_path, private=True)
        with FileLock(lock_path, timeout=10, mode=0o600):
            yield
    except Timeout:
        raise GatewaySettingsError("Gateway settings are busy. Try again shortly.") from None
    except OSError:
        raise GatewaySettingsError("Claudex could not access the settings files.") from None


def _file_info(path: Path, *, private: bool = False) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or _is_reparse_point(info)
        or (private and sys.platform != "win32" and stat.S_IMODE(info.st_mode) != 0o600)
    ):
        raise GatewaySettingsError("Claudex needs a regular settings file with safe permissions.")
    if private and sys.platform == "win32":
        _protect_windows_path(path)
    return info


def _is_reparse_point(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _protect_windows_path(path: Path) -> None:
    from claudex.infrastructure.windows_security import make_private

    make_private(path)


def _read(path: Path, *, private: bool = False) -> Settings | None:
    if _file_info(path, private=private) is None:
        return None
    try:
        with open(path, "rb", opener=_open_regular) as source:
            data = json.load(
                source, object_pairs_hook=_unique_keys, parse_constant=_reject_constant
            )
    except ValueError:
        raise GatewaySettingsError("The settings file must contain a valid JSON object.") from None
    if not isinstance(data, dict):
        raise GatewaySettingsError("The settings file must contain a valid JSON object.")
    return data


def _open_regular(path: str, flags: int) -> int:
    before = os.lstat(path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags | nofollow | nonblock)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or _is_reparse_point(before)
        or _is_reparse_point(info)
        or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)
    ):
        os.close(fd)
        raise GatewaySettingsError("Claudex needs a regular settings file.")
    return fd


def _unique_keys(pairs: list[tuple[str, Any]]) -> Settings:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError


def _write(path: Path, data: Settings) -> None:
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8") as file:
            temporary_path = Path(file.name)
            if sys.platform == "win32":
                _protect_windows_path(temporary_path)
            else:
                os.fchmod(file.fileno(), 0o600)
            json.dump(data, file, indent=2, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        _file_info(path)
        temporary_path.replace(path)
    except TypeError, ValueError:
        raise GatewaySettingsError("Claudex could not encode the settings as JSON.") from None
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)


def _port_filename(port: int) -> str:
    if type(port) is not int or not 1 <= port <= 65535:
        raise GatewaySettingsError("The port must be between 1 and 65535.")
    return f"{port}.json"


class GatewaySettingsStore:
    def __init__(self, state_dir: Path) -> None:
        self._directory = state_dir / "gateways"

    def ports(self) -> list[int]:
        return sorted(
            int(path.stem)
            for path in self._directory.glob("*.json")
            if re.fullmatch(r"[1-9][0-9]{0,4}", path.stem) and int(path.stem) <= 65535
        )

    @contextmanager
    def running_instance(self, *, timeout: float = 0) -> Iterator[None]:
        with _locked(self._directory, "gateway.lock", private=True):
            path = self._directory / "running.lock"
            _file_info(path, private=True)
        try:
            with FileLock(path, timeout=timeout, mode=0o600):
                yield
        except Timeout:
            raise GatewaySettingsError("A Claudex proxy is already starting or running.") from None

    def load(self, port: int, binding: str, *, reuse_only: bool = False) -> str:
        path = self._directory / _port_filename(port)
        if not isinstance(binding, str) or not binding:
            raise GatewaySettingsError("A saved account is required for a persistent gateway.")
        with _locked(self._directory, "gateway.lock", private=True):
            record = _read(path, private=True)
            if record is not None and record.get("binding") == binding:
                return _record_token(record)
            if reuse_only:
                raise GatewaySettingsError("The saved gateway changed. Run claudex serve again.")
            token = token_urlsafe(32)
            _write(path, {"binding": binding, "token": token})
            return token

    def publish(
        self,
        port: int,
        binding: str,
        token: str,
        settings: Settings,
        launch: GatewayLaunch | None = None,
    ) -> None:
        path = self._directory / _port_filename(port)
        with _locked(self._directory, "gateway.lock", private=True):
            record = _read(path, private=True)
            if (
                record is None
                or record.get("binding") != binding
                or not _tokens_equal(token, _record_token(record))
            ):
                raise GatewaySettingsError("The gateway credential no longer matches this port.")
            if not _matches_gateway(settings, port, token):
                raise GatewaySettingsError("The Claude settings do not match the gateway.")
            update = {**record, "settings": settings}
            if launch is not None:
                update["launch"] = launch.record()
            _write(path, update)

    def settings(self, port: int) -> Settings:
        return self.snapshot(port).settings

    def snapshot(self, port: int) -> GatewaySnapshot:
        path = self._directory / _port_filename(port)
        with _locked(self._directory, "gateway.lock", private=True):
            record = _read(path, private=True)
            missing = "Start Claudex on this port before configuring Claude."
            if record is None:
                raise GatewayNotPublishedError(missing)
            binding = record.get("binding")
            token = _record_token(record)
            if isinstance(binding, str) and binding and "settings" not in record:
                raise GatewayNotPublishedError(missing)
            settings = record.get("settings")
            if (
                not isinstance(binding, str)
                or not binding
                or not isinstance(settings, dict)
                or not _matches_gateway(settings, port, token)
            ):
                raise GatewaySettingsError("Saved gateway settings are invalid.")
            value = record.get("launch")
            launch = GatewayLaunch.parse(value) if value is not None else None
            return GatewaySnapshot(settings, binding, launch)


def _record_token(record: Settings) -> str:
    token = record.get("token")
    if not isinstance(token, str) or len(token) < 40 or not token.isascii():
        raise GatewaySettingsError("The stored gateway credential is invalid.")
    return token


def _tokens_equal(first: object, second: str) -> bool:
    return isinstance(first, str) and first.isascii() and compare_digest(first, second)


def _matches_gateway(settings: Any, port: int, token: str) -> bool:
    env = settings.get("env") if isinstance(settings, dict) else None
    return (
        isinstance(env, dict)
        and env.get("ANTHROPIC_BASE_URL") == f"http://127.0.0.1:{port}"
        and _tokens_equal(env.get("ANTHROPIC_AUTH_TOKEN"), token)
    )


class _Missing:
    pass


_MISSING = _Missing()


class _Change(NamedTuple):
    path: tuple[str, ...]
    original: Any
    applied: Any


@dataclass(frozen=True)
class _Configuration:
    original_exists: bool
    original: Settings
    applied: Settings
    prior: Settings | None = None

    @classmethod
    def parse(cls, data: Settings) -> _Configuration:
        try:
            original_exists = data["original_exists"]
            original = data["original"]
            applied = data["applied"]
            prior = data.get("prior")
        except KeyError:
            raise GatewaySettingsError("The Claude settings backup is invalid.") from None
        if (
            type(original_exists) is not bool
            or not isinstance(original, dict)
            or not isinstance(applied, dict)
            or (prior is not None and not isinstance(prior, dict))
        ):
            raise GatewaySettingsError("The Claude settings backup is invalid.")
        return cls(original_exists, original, applied, prior)

    def record(self, *, prior: Settings | None = None) -> Settings:
        return {
            "original_exists": self.original_exists,
            "original": self.original,
            "applied": self.applied,
            "prior": prior,
        }


def _managed_values(settings: Settings) -> Iterator[tuple[tuple[str, ...], Any]]:
    for name, value in settings.items():
        if name == "env":
            for key, setting in _object(value).items():
                yield (name, key), setting
        elif name == "modelSettings":
            for model, fields in _object(value).items():
                for field, setting in _object(fields).items():
                    yield (name, model, field), setting
        else:
            yield (name,), value


def _object(value: Any) -> Settings:
    if not isinstance(value, dict):
        raise GatewaySettingsError("Claude env and per-model settings must be JSON objects.")
    return value


def _get(settings: Settings, path: tuple[str, ...]) -> Any:
    value = settings
    for name in path:
        if value is _MISSING:
            return value
        value = _object(value).get(name, _MISSING)
    return value


def _set(settings: Settings, path: tuple[str, ...], value: Any) -> None:
    for name in path[:-1]:
        settings = _object(settings.setdefault(name, {}))
    if value is _MISSING:
        settings.pop(path[-1], None)
    else:
        settings[path[-1]] = deepcopy(value)


def _changes(original: Settings, applied: Settings) -> list[_Change]:
    return [_Change(path, _get(original, path), value) for path, value in _managed_values(applied)]


def _without_changes(
    current: Settings,
    configuration: _Configuration,
    *,
    restoring: bool = False,
) -> Settings:
    result = deepcopy(current)
    changes = _changes(configuration.original, configuration.applied)
    for path, original, applied in changes:
        value = _get(result, path)
        if (
            value != applied
            and not _model_preference(path)
            and (not restoring or value != original)
        ):
            _raise_conflict(path)
        if value != original:
            _set(result, path, original)
    _prune_added_objects(result, configuration.original, changes)
    return result


def _model_preference(path: tuple[str, ...]) -> bool:
    if path in {("model",), ("autoCompactEnabled",)}:
        return True
    return (
        len(path) == 3
        and path[0] == "modelSettings"
        and path[1].startswith("claudex-")
        and path[2] in {"effortLevel", "autoCompactWindow"}
    )


def _prune_added_objects(result: Settings, original: Settings, changes: list[_Change]) -> None:
    parents = {path[:size] for path, _, _ in changes for size in range(1, len(path))}
    for parent in sorted(parents, key=len, reverse=True):
        if _get(original, parent) is _MISSING and _get(result, parent) == {}:
            _set(result, parent, _MISSING)


def _raise_conflict(path: tuple[str, ...]) -> None:
    label = ".".join(path) if all(_PATH_SEGMENT.fullmatch(name) for name in path) else "field"
    raise GatewaySettingsError(
        f"Claude setting {label} changed. Restore its configured value and retry."
    )


def _apply_settings(current: Settings, settings: Settings) -> Settings:
    result = deepcopy(current)
    for path, value in _managed_values(settings):
        _set(result, path, value)
    return result


def _preserve_no_proxy(original: Settings, settings: Settings) -> Settings:
    if "env" not in settings:
        return settings
    configured = _object(settings["env"])
    keys = ("NO_PROXY", "no_proxy")
    if not any(key in configured for key in keys):
        return settings
    original_env = _object(original.get("env", {}))
    hosts = dict.fromkeys(
        host
        for source in (original_env, configured)
        for key in keys
        if isinstance(value := source.get(key), str)
        for entry in value.split(",")
        if (host := entry.strip())
    )
    value = "*" if "*" in hosts else ",".join(hosts)
    return {**settings, "env": {**configured, **dict.fromkeys(keys, value)}}


def _recover(
    settings_path: Path, journal_path: Path
) -> tuple[Settings | None, _Configuration | None]:
    current = _read(settings_path)
    backup = _read(journal_path, private=True)
    configuration = _Configuration.parse(backup) if backup is not None else None
    if configuration is None or configuration.prior is None:
        return current, configuration
    # write-ahead records allow retry after either the backup or settings write succeeds
    pending = configuration.prior
    if current == pending.get("after"):
        _write(journal_path, configuration.record())
        return current, configuration
    previous = pending.get("configuration")
    if current == pending.get("before") and (previous is None or isinstance(previous, dict)):
        restored = _Configuration.parse(previous) if previous is not None else None
        _restore_journal(journal_path, previous)
        return current, restored
    raise GatewaySettingsError(
        "A Claude settings update was interrupted. Review settings and retry."
    )


def _restore_journal(path: Path, backup: Settings | None) -> None:
    if backup is None:
        path.unlink(missing_ok=True)
    else:
        _write(path, backup)


def configure_claude(claude_config_dir: Path, settings: Settings) -> bool:
    settings_path = claude_config_dir / "settings.json"
    journal_path = claude_config_dir / ".claudex-settings.json"
    with _locked(claude_config_dir, ".claudex-settings.lock"):
        saved, configuration = _recover(settings_path, journal_path)
        current = saved if saved is not None else {}
        if configuration is None:
            exists = saved is not None
            original = current
        else:
            exists = configuration.original_exists
            original = _without_changes(current, configuration)
        settings = _preserve_no_proxy(original, settings)
        updated = _apply_settings(original, settings)
        new = _Configuration(exists, original, settings)
        private_mode = _needs_private_mode(settings_path)
        if current == updated and configuration is not None and settings == configuration.applied:
            if private_mode:
                _write(settings_path, current)
                return True
            return False
        if current == updated and not private_mode:
            _write(journal_path, new.record())
            return True
        prior = {
            "before": saved,
            "after": updated,
            "configuration": configuration.record() if configuration else None,
        }
        _write(journal_path, new.record(prior=prior))
        _write(settings_path, updated)
        _write(journal_path, new.record())
    return True


def unconfigure_claude(claude_config_dir: Path) -> bool:
    if not claude_config_dir.exists() and not claude_config_dir.is_symlink():
        return False
    settings_path = claude_config_dir / "settings.json"
    journal_path = claude_config_dir / ".claudex-settings.json"
    with _locked(claude_config_dir, ".claudex-settings.lock"):
        current, configuration = _recover(settings_path, journal_path)
        if configuration is None:
            return False
        restored = _without_changes(current or {}, configuration, restoring=True)
        if restored or configuration.original_exists:
            if restored != current or _needs_private_mode(settings_path):
                _write(settings_path, restored)
        else:
            settings_path.unlink(missing_ok=True)
        journal_path.unlink()
    return True


def _needs_private_mode(path: Path) -> bool:
    info = _file_info(path)
    if info is not None and sys.platform == "win32":
        _protect_windows_path(path)
        return False
    return info is not None and stat.S_IMODE(info.st_mode) != 0o600
