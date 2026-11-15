import asyncio
import hashlib
import json
import os
import re
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol, cast
from uuid import uuid4

from filelock import AsyncFileLock, Timeout
from pydantic import TypeAdapter, ValidationError

from claudex.domain.accounts import AccountError, Registration

SERVICE = "org.claudex.chatgpt"
_REGISTRATION = TypeAdapter(Registration)
_CHUNK_PREFIX = "claudex-chunks-v1:"
_CHUNK_MANIFEST = re.compile(r"claudex-chunks-v1:([0-9a-f]{32}):([1-9][0-9]{0,3}):([0-9a-f]{64})\Z")
_CHUNK_SIZE = 600


class CredentialBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


class InvalidStoredCredentialsError(AccountError):
    pass


class _WindowsCredentialBackend:
    def __init__(self, backend: CredentialBackend) -> None:
        self._backend = backend

    @staticmethod
    def _manifest(value: str | None) -> tuple[str, int, str] | None:
        if value is None or not value.startswith(_CHUNK_PREFIX):
            return None
        match = _CHUNK_MANIFEST.fullmatch(value)
        if match is None:
            raise InvalidStoredCredentialsError("Stored credentials are invalid. Sign in again.")
        return match[1], int(match[2]), match[3]

    @staticmethod
    def _chunk_service(service: str, generation: str, index: int) -> str:
        return f"{service}.{generation}.{index}"

    @staticmethod
    def _manifest_service(service: str, username: str) -> str:
        digest = hashlib.sha256(username.encode("utf-8")).hexdigest()
        return f"{service}.manifest.{digest}"

    def get_password(self, service: str, username: str) -> str | None:
        value = self._backend.get_password(self._manifest_service(service, username), username)
        manifest = self._manifest(value)
        if manifest is None:
            return value
        generation, count, digest = manifest
        parts = []
        for index in range(count):
            part = self._backend.get_password(
                self._chunk_service(service, generation, index), username
            )
            if part is None:
                raise InvalidStoredCredentialsError(
                    "Stored credentials are incomplete. Sign in again."
                )
            parts.append(part)
        result = "".join(parts)
        if hashlib.sha256(result.encode("utf-8")).hexdigest() != digest:
            raise InvalidStoredCredentialsError("Stored credentials are invalid. Sign in again.")
        return result

    def set_password(self, service: str, username: str, password: str) -> None:
        manifest_service = self._manifest_service(service, username)
        previous = self._manifest(self._backend.get_password(manifest_service, username))
        parts = [password[i : i + _CHUNK_SIZE] for i in range(0, len(password), _CHUNK_SIZE)] or [
            ""
        ]
        if len(parts) > 9999:
            raise InvalidStoredCredentialsError("Credentials exceed the supported size.")
        generation = uuid4().hex
        written = []
        try:
            for index, part in enumerate(parts):
                chunk_service = self._chunk_service(service, generation, index)
                self._backend.set_password(chunk_service, username, part)
                written.append(chunk_service)
            digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
            self._backend.set_password(
                manifest_service, username, f"{_CHUNK_PREFIX}{generation}:{len(parts)}:{digest}"
            )
        except Exception:
            for chunk_service in written:
                with suppress(Exception):
                    self._backend.delete_password(chunk_service, username)
            raise
        if previous is not None:
            # the new manifest is already committed; cleanup cannot roll it back
            with suppress(Exception):
                self._delete_chunks(service, username, previous)

    def _delete_chunks(self, service: str, username: str, manifest: tuple[str, int, str]) -> None:
        generation, count, _ = manifest
        for index in range(count):
            chunk_service = self._chunk_service(service, generation, index)
            if self._backend.get_password(chunk_service, username) is not None:
                self._backend.delete_password(chunk_service, username)

    def delete_password(self, service: str, username: str) -> None:
        manifest_service = self._manifest_service(service, username)
        manifest = self._manifest(self._backend.get_password(manifest_service, username))
        if manifest is not None:
            self._delete_chunks(service, username, manifest)
        self._backend.delete_password(manifest_service, username)


def _os_keyring() -> CredentialBackend:
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring

        return cast(CredentialBackend, Keyring())
    if sys.platform == "linux":
        from keyring.backends.SecretService import Keyring as SecretService

        return cast(CredentialBackend, SecretService())
    if sys.platform == "win32":
        from keyring.backends import Windows
        from keyring.backends.Windows import WinVaultKeyring

        vault = WinVaultKeyring()
        vault.persist = Windows.win32cred.CRED_PERSIST_LOCAL_MACHINE
        return _WindowsCredentialBackend(cast(CredentialBackend, vault))
    raise AccountError("This platform has no supported credential store.", status_code=503)


class CredentialStore:
    def __init__(self, backend: CredentialBackend | None = None) -> None:
        self._backend = backend

    @property
    def _keyring(self) -> CredentialBackend:
        if self._backend is None:
            self._backend = _os_keyring()
        return self._backend

    def read(self, registration_id: str) -> Registration | None:
        with _keyring_errors():
            stored = self._keyring.get_password(SERVICE, registration_id)
        if stored is None:
            return None
        try:
            return _REGISTRATION.validate_json(stored)
        except ValidationError:
            raise InvalidStoredCredentialsError(
                "Stored credentials are invalid. Remove the profile and sign in again."
            ) from None

    def write(self, registration_id: str, registration: Registration) -> None:
        with _keyring_errors():
            self._keyring.set_password(SERVICE, registration_id, json.dumps(asdict(registration)))

    def delete(self, registration_id: str) -> None:
        with _keyring_errors():
            try:
                present = self._keyring.get_password(SERVICE, registration_id) is not None
            except InvalidStoredCredentialsError:
                present = True
            if present:
                self._keyring.delete_password(SERVICE, registration_id)


@dataclass(frozen=True, slots=True)
class ProfileIndex:
    host_id: str
    profiles: dict[str, str]
    selected: str | None = None


_INDEX = TypeAdapter(ProfileIndex)


class ProfileStore:
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._index = directory / "profiles.json"

    @asynccontextmanager
    async def lock(self, name: str) -> AsyncIterator[None]:
        try:
            await asyncio.to_thread(self._directory.mkdir, mode=0o700, parents=True, exist_ok=True)
            async with AsyncFileLock(self._directory / f"{name}.lock", timeout=30, mode=0o600):
                yield
        except Timeout:
            raise AccountError(
                "This profile is busy. Try again shortly.", status_code=503
            ) from None
        except OSError:
            raise AccountError("Claudex could not access profile files.", status_code=503) from None

    def read(self) -> ProfileIndex | None:
        try:
            return _INDEX.validate_json(self._index.read_bytes())
        except FileNotFoundError:
            return None
        except OSError, ValidationError:
            raise AccountError(
                "Claudex could not read the profile index.", status_code=503
            ) from None

    def write(self, index: ProfileIndex) -> None:
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(dir=self._directory, delete=False, mode="wb") as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(_INDEX.dump_json(index))
                temporary.flush()
                os.fsync(temporary.fileno())
            temporary_path.replace(self._index)
        except OSError:
            raise AccountError(
                "Claudex could not save the profile index.", status_code=503
            ) from None
        finally:
            if temporary_path is not None:
                with suppress(OSError):
                    temporary_path.unlink(missing_ok=True)

    async def initialize(self) -> ProfileIndex:
        async with self.lock("index"):
            index = await asyncio.to_thread(self.read)
            if index is None:
                index = ProfileIndex(host_id=uuid4().urn, profiles={})
                await asyncio.to_thread(self.write, index)
            return index


@contextmanager
def _keyring_errors() -> Iterator[None]:
    try:
        yield
    except AccountError:
        raise
    except Exception:
        # backend errors may include sensitive arguments or platform details
        raise AccountError(
            "Claudex could not access the OS credential store. Unlock it and try again.",
            status_code=503,
        ) from None
