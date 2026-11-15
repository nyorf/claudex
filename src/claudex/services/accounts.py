import asyncio
import re
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx2
from anyio import CancelScope, to_thread

from claudex.domain.accounts import (
    AccountError,
    ChatGPTTokens,
    ReauthenticationRequiredError,
    Registration,
)
from claudex.infrastructure.credentials import (
    CredentialStore,
    InvalidStoredCredentialsError,
    ProfileIndex,
    ProfileStore,
)
from claudex.infrastructure.oauth import (
    ExpiredRefreshError,
    InvalidIdentityError,
    InvalidTokenResponseError,
    OAuthClient,
)

_REFRESH_MARGIN = 60
_PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}\Z")


def _validate_profile(profile: str) -> None:
    if not _PROFILE_NAME.fullmatch(profile):
        raise AccountError(
            "Use up to 48 lowercase letters, digits, hyphens, or underscores for the profile name. "
            "Start with a letter or digit.",
            status_code=400,
        )


class AccountManager:
    def __init__(
        self,
        state_dir: Path,
        *,
        client: httpx2.AsyncClient,
        credentials: CredentialStore | None = None,
        oauth: OAuthClient | None = None,
    ) -> None:
        self._profiles = ProfileStore(state_dir)
        self._credentials = credentials if credentials is not None else CredentialStore()
        self._oauth = oauth if oauth is not None else OAuthClient(client)

    async def login(self, profile: str) -> None:
        _validate_profile(profile)
        async with self._profiles.lock(f"profile-{profile}"):
            index = await self._profiles.initialize()
            registration_id = index.profiles.get(profile)
            previous = (
                await to_thread.run_sync(self._credentials.read, registration_id)
                if registration_id is not None
                else None
            )
            registration = await self._oauth.register(index.host_id, previous)
            with CancelScope(shield=True):
                if registration_id is None or previous is None:
                    await self._save_new(profile, registration)
                elif registration.tokens is not None or previous.tokens is None:
                    await to_thread.run_sync(self._credentials.write, registration_id, registration)
            if registration.tokens is None:
                raise ReauthenticationRequiredError(
                    "ChatGPT plan usage was not granted. Sign in again and allow plan usage."
                )

    async def _save_new(self, profile: str, registration: Registration) -> None:
        registration_id = uuid4().hex
        await to_thread.run_sync(self._credentials.write, registration_id, registration)
        try:
            async with self._profiles.lock("index"):
                index = await self._read_index()
                profiles = {**index.profiles, profile: registration_id}
                selected = index.selected if index.selected != profile else None
                await to_thread.run_sync(
                    self._profiles.write,
                    replace(index, profiles=profiles, selected=selected),
                )
        except AccountError:
            await to_thread.run_sync(self._credentials.delete, registration_id)
            raise

    async def logout(self, profile: str) -> bool:
        return await self._sign_out(profile, remove=False)

    async def remove(self, profile: str) -> bool:
        return await self._sign_out(profile, remove=True)

    async def _sign_out(self, profile: str, *, remove: bool) -> bool:
        _validate_profile(profile)
        async with self._profiles.lock(f"profile-{profile}"):
            index = await self._read_index()
            registration_id = _profile_id(index, profile)
            with CancelScope(shield=True):
                try:
                    registration = await to_thread.run_sync(self._credentials.read, registration_id)
                except InvalidStoredCredentialsError:
                    if not remove:
                        raise
                    await to_thread.run_sync(self._credentials.delete, registration_id)
                    registration = None
                tokens = registration.tokens if registration else None
                if registration is not None and tokens is not None:
                    revoked = await self._oauth.revoke(registration, tokens)
                else:
                    revoked = registration is not None
                await self._clear_profile(profile, registration_id, registration, remove=remove)
            return revoked

    async def _clear_profile(
        self,
        profile: str,
        registration_id: str,
        registration: Registration | None,
        *,
        remove: bool,
    ) -> None:
        # clear tokens first so another launch cannot keep using the signed-out registration
        if registration is not None and registration.tokens is not None:
            await to_thread.run_sync(
                self._credentials.write,
                registration_id,
                replace(registration, tokens=None),
            )
        async with self._profiles.lock("index"):
            index = await self._read_index()
            profiles = dict(index.profiles)
            if remove:
                del profiles[profile]
            selected = index.selected if index.selected != profile else None
            await to_thread.run_sync(
                self._profiles.write,
                replace(index, profiles=profiles, selected=selected),
            )
        if remove:
            await to_thread.run_sync(self._credentials.delete, registration_id)

    async def profiles(self) -> list[str]:
        index = await to_thread.run_sync(self._profiles.read)
        return sorted(index.profiles) if index else []

    async def selected(self) -> str | None:
        index = await to_thread.run_sync(self._profiles.read)
        return index.selected if index else None

    async def select(self, profile: str) -> None:
        _validate_profile(profile)
        async with self._profiles.lock(f"profile-{profile}"), self._profiles.lock("index"):
            index = await self._read_index()
            registration = await self._registration(_profile_id(index, profile))
            _connected_tokens(registration)
            await to_thread.run_sync(self._profiles.write, replace(index, selected=profile))

    def provider(self, profile: str) -> ProfileTokenProvider:
        _validate_profile(profile)
        registration_id = _profile_id(self._profiles.read(), profile)
        return ProfileTokenProvider(self, profile, registration_id)

    async def _read_index(self) -> ProfileIndex:
        index = await to_thread.run_sync(self._profiles.read)
        if index is None:
            raise ReauthenticationRequiredError("Sign in to a ChatGPT profile first.")
        return index

    async def _registration(self, registration_id: str) -> Registration:
        registration = await to_thread.run_sync(self._credentials.read, registration_id)
        if registration is None:
            raise ReauthenticationRequiredError("This profile has no saved sign-in. Sign in again.")
        return registration

    async def _token(self, profile: str, registration_id: str, refresh_if: str | None) -> str:
        async with self._profiles.lock(f"profile-{profile}"):
            index = await self._read_index()
            if index.profiles.get(profile) != registration_id:
                raise ReauthenticationRequiredError(
                    "The profile for this launch was removed. Start a new launch."
                )
            registration = await self._registration(registration_id)
            tokens = _connected_tokens(registration)
            with CancelScope(shield=True):
                tokens = await self._confirm_identity(registration_id, registration, tokens)
                if _should_refresh(tokens, refresh_if):
                    tokens = await self._finish_rotation(registration_id, registration, tokens)
                    tokens = await self._confirm_identity(registration_id, registration, tokens)
            now = time.time()
            unusable = tokens.expires_at <= now or refresh_if == tokens.access_token
            if unusable and now < tokens.refresh_after:
                raise AccountError(
                    "ChatGPT has not opened its token refresh window yet. Try again shortly.",
                    status_code=503,
                )
            if unusable:
                raise ReauthenticationRequiredError(
                    "ChatGPT rejected the access token. Sign in again."
                )
            return tokens.access_token

    async def _finish_rotation(
        self,
        registration_id: str,
        registration: Registration,
        tokens: ChatGPTTokens,
    ) -> ChatGPTTokens:
        # uvicorn can cancel a task directly while the profile lock still guards a rotation
        task = asyncio.create_task(self._rotate(registration_id, registration, tokens))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except AccountError:
                    break
            if not task.cancelled():
                task.exception()
            raise

    async def _rotate(
        self,
        registration_id: str,
        registration: Registration,
        tokens: ChatGPTTokens,
    ) -> ChatGPTTokens:
        try:
            updated = await self._oauth.refresh(registration, tokens)
        except (ExpiredRefreshError, InvalidIdentityError, InvalidTokenResponseError) as error:
            await to_thread.run_sync(
                self._credentials.write,
                registration_id,
                replace(registration, tokens=None),
            )
            raise ReauthenticationRequiredError(str(error)) from None
        # store the rotated refresh token before checking the new ID token against remote keys
        await to_thread.run_sync(
            self._credentials.write,
            registration_id,
            replace(registration, tokens=updated),
        )
        return updated

    async def _confirm_identity(
        self,
        registration_id: str,
        registration: Registration,
        tokens: ChatGPTTokens,
    ) -> ChatGPTTokens:
        if tokens.identity_pending_since is None:
            return tokens
        try:
            verified = await self._oauth.confirm_refreshed_identity(registration, tokens)
        except InvalidIdentityError as error:
            await to_thread.run_sync(
                self._credentials.write,
                registration_id,
                replace(registration, tokens=None),
            )
            raise ReauthenticationRequiredError(str(error)) from None
        await to_thread.run_sync(
            self._credentials.write,
            registration_id,
            replace(registration, tokens=verified),
        )
        return verified


class ProfileTokenProvider:
    def __init__(self, manager: AccountManager, profile: str, registration_id: str) -> None:
        self._manager = manager
        self._profile = profile
        self._registration_id = registration_id

    async def access_token(self, *, refresh_if: str | None = None) -> str:
        return await self._manager._token(self._profile, self._registration_id, refresh_if)

    @property
    def registration_id(self) -> str:
        return self._registration_id


def _profile_id(index: ProfileIndex | None, profile: str) -> str:
    if index is None or profile not in index.profiles:
        raise ReauthenticationRequiredError("This ChatGPT profile is unknown. Sign in first.")
    return index.profiles[profile]


def _connected_tokens(registration: Registration) -> ChatGPTTokens:
    if registration.tokens is None:
        raise ReauthenticationRequiredError("This ChatGPT profile needs a new sign-in.")
    return registration.tokens


def _should_refresh(tokens: ChatGPTTokens, refresh_if: str | None) -> bool:
    now = time.time()
    return now >= tokens.refresh_after and (
        tokens.expires_at <= now + _REFRESH_MARGIN or refresh_if == tokens.access_token
    )
