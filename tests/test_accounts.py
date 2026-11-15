import asyncio
import base64
import hashlib
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from claudex.domain.accounts import AccountError, ReauthenticationRequiredError, Registration
from claudex.infrastructure.credentials import CredentialStore
from claudex.infrastructure.oauth import (
    InvalidIdentityError,
    OAuthClient,
    _Attempt,
    _Callback,
    _parse_callback,
)
from claudex.services.accounts import AccountManager

_REDIRECT = "http://127.0.0.1:1455/auth/callback"


def _base64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class MemoryCredentialBackend:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.records.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.records[service, username] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.records[service, username]


class FakeOpenAI:
    def __init__(self) -> None:
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.authorization_queries: list[dict[str, list[str]]] = []
        self.refreshes = 0
        self.refresh_started = asyncio.Event()
        self.release_refresh: asyncio.Event | None = None
        self.revoke_status = 200
        self.jwks_status = 200
        self.expire_code = False
        self.refresh_error: str | None = None
        self.omit_refresh_scope = False
        self.plan_allowed = True
        self.subject = "synthetic-subject"
        self.forged_claims: dict[str, str] = {}

    async def browser(
        self,
        attempt: _Attempt,
        host_id: str,
        client_id: str,
        registration: Registration | None,
    ) -> _Callback:
        query = parse_qs(
            urlsplit(
                attempt.authorization_url(
                    _REDIRECT,
                    host_id,
                    client_id,
                    registration,
                )
            ).query
        )
        self.authorization_queries.append(query)
        assert query["code_challenge"] == [
            _base64(hashlib.sha256(attempt.verifier.encode()).digest())
        ]
        if client_id == "dynamic_agent_client":
            client_id = f"oaiapp_synthetic{len(self.authorization_queries)}"
        return _Callback("synthetic-code", client_id, _REDIRECT)

    async def respond(self, request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "auth.openai.com"
        match request.url.path:
            case "/.well-known/jwks.json":
                if self.jwks_status != 200:
                    return httpx2.Response(self.jwks_status, json={})
                key = self.private_key.public_key().public_numbers()
                return httpx2.Response(
                    200,
                    json={
                        "keys": [
                            {
                                "kid": "synthetic",
                                "kty": "RSA",
                                "n": _base64(key.n.to_bytes((key.n.bit_length() + 7) // 8)),
                                "e": _base64(key.e.to_bytes((key.e.bit_length() + 7) // 8)),
                            }
                        ]
                    },
                )
            case "/.well-known/openid-configuration":
                return httpx2.Response(
                    200,
                    json={
                        "issuer": "https://auth.openai.com",
                        "revocation_endpoint": "https://auth.openai.com/api/accounts/oauth/revoke",
                    },
                )
            case "/api/accounts/oauth/revoke":
                form = parse_qs(request.content.decode())
                assert form["token_type_hint"] == ["refresh_token"]
                assert form["token"][0].startswith("synthetic-renew-")
                return httpx2.Response(self.revoke_status)
            case "/api/accounts/oauth/token":
                return await self._token_response(parse_qs(request.content.decode()))
            case _:
                raise AssertionError("unexpected synthetic upstream request")

    async def _token_response(self, form: dict[str, list[str]]) -> httpx2.Response:
        assert form["resource"] == ["https://api.openai.com/v1"]
        client_id = form["client_id"][0]
        assert client_id.startswith("oaiapp_")
        if form["grant_type"] == ["refresh_token"]:
            self.refreshes += 1
            self.refresh_started.set()
            if self.release_refresh is not None:
                await self.release_refresh.wait()
            else:
                await asyncio.sleep(0.01)
            assert "scope" not in form
            if self.refresh_error:
                return httpx2.Response(400, json={"error": self.refresh_error})
            nonce = None
        else:
            assert form["redirect_uri"] == [_REDIRECT]
            assert len(form["code_verifier"][0]) >= 43
            if self.expire_code:
                self.expire_code = False
                return httpx2.Response(400, json={"error": "invalid_grant"})
            nonce = self.authorization_queries[-1]["nonce"][0]
        suffix = f"{client_id}-{self.refreshes}"
        scope = "openid profile email offline_access resource.invoke"
        if self.plan_allowed:
            scope += " chatgpt.tokens.use.direct"
        body = {
            "access_token": f"synthetic-access-{suffix}",
            "refresh_token": f"synthetic-renew-{suffix}",
            "id_token": self._identity(client_id, nonce),
            "scope": scope,
            "token_type": "Bearer",
            "expires_in": 3600,
        }
        if nonce is None and self.omit_refresh_scope:
            del body["scope"]
        return httpx2.Response(200, json=body)

    def _identity(self, client_id: str, nonce: str | None) -> str:
        now = int(time.time())
        claims = {
            "iss": "https://auth.openai.com",
            "sub": self.subject,
            "aud": [client_id],
            "iat": now,
            "exp": now + 600,
            "nonce": nonce,
            **self.forged_claims,
        }
        header = _base64(json.dumps({"alg": "RS256", "kid": "synthetic"}).encode())
        signed = f"{header}.{_base64(json.dumps(claims).encode())}"
        signature = self.private_key.sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{signed}.{_base64(signature)}"


@pytest.fixture
def fake_openai() -> FakeOpenAI:
    return FakeOpenAI()


@pytest.fixture
def store() -> CredentialStore:
    return CredentialStore(MemoryCredentialBackend())


@pytest.fixture
async def manager(tmp_path: Path, fake_openai: FakeOpenAI, store: CredentialStore):
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(fake_openai.respond)) as client:
        oauth = OAuthClient(client, authorize=fake_openai.browser)
        yield AccountManager(tmp_path, client=client, credentials=store, oauth=oauth)


async def test_switching_profiles_preserves_the_launch_binding(
    manager: AccountManager,
    fake_openai: FakeOpenAI,
    tmp_path: Path,
) -> None:
    await manager.login("personal")
    first = manager.provider("personal")
    first_token = await first.access_token()
    assert await manager.selected() is None
    await manager.select("personal")
    await manager.login("work")
    await manager.select("work")
    assert await manager.profiles() == ["personal", "work"]
    assert await manager.selected() == "work"
    assert await manager.provider("work").access_token() != first_token
    assert await first.access_token() == first_token
    initial, later = fake_openai.authorization_queries
    assert initial["ext_agent_host_id"] == later["ext_agent_host_id"]
    assert initial["client_id"] == later["client_id"] == ["dynamic_agent_client"]
    assert initial["agent_name_hint"] == ["Claudex"]
    state = (tmp_path / "profiles.json").read_text()
    assert "synthetic" not in state
    if sys.platform != "win32":
        assert (tmp_path / "profiles.json").stat().st_mode & 0o777 == 0o600


async def test_concurrent_launches_only_rotate_the_rejected_token_once(
    tmp_path: Path,
    store: CredentialStore,
    fake_openai: FakeOpenAI,
) -> None:
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(fake_openai.respond)) as client:
        oauth = OAuthClient(client, authorize=fake_openai.browser)
        first = AccountManager(tmp_path, client=client, credentials=store, oauth=oauth)
        second = AccountManager(tmp_path, client=client, credentials=store, oauth=oauth)
        await first.login("work")
        first_provider, second_provider = first.provider("work"), second.provider("work")
        stale = await first_provider.access_token()
        a, b = await asyncio.gather(
            first_provider.access_token(refresh_if=stale),
            second_provider.access_token(refresh_if=stale),
        )
        assert a == b and a != stale
        assert fake_openai.refreshes == 1


async def test_pending_identity_verification_does_not_reuse_the_consumed_refresh_token(
    manager: AccountManager,
    fake_openai: FakeOpenAI,
) -> None:
    await manager.login("work")
    provider = manager.provider("work")
    stale = await provider.access_token()
    fake_openai.jwks_status = 503
    with pytest.raises(AccountError, match="identity services"):
        await provider.access_token(refresh_if=stale)
    fake_openai.jwks_status = 200
    assert await provider.access_token(refresh_if=stale) != stale
    assert fake_openai.refreshes == 1


async def test_cancelled_request_persists_refresh_rotation_before_unlocking(
    manager: AccountManager,
    fake_openai: FakeOpenAI,
) -> None:
    await manager.login("work")
    provider = manager.provider("work")
    stale = await provider.access_token()
    fake_openai.release_refresh = asyncio.Event()
    request = asyncio.create_task(provider.access_token(refresh_if=stale))
    async with asyncio.timeout(2):
        await fake_openai.refresh_started.wait()
        request.cancel()
        await asyncio.sleep(0)
        request.cancel()
        fake_openai.release_refresh.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert await provider.access_token(refresh_if=stale) != stale
    assert fake_openai.refreshes == 1


async def test_terminal_refresh_error_clears_tokens_but_reuses_the_registration(
    manager: AccountManager,
    fake_openai: FakeOpenAI,
) -> None:
    await manager.login("work")
    provider = manager.provider("work")
    fake_openai.refresh_error = "refresh_token_reused"
    stale = await provider.access_token()
    with pytest.raises(ReauthenticationRequiredError, match="expired"):
        await provider.access_token(refresh_if=stale)
    with pytest.raises(ReauthenticationRequiredError, match="new sign-in"):
        await provider.access_token()
    await manager.login("work")
    assert fake_openai.authorization_queries[-1]["client_id"] == ["oaiapp_synthetic1"]


async def test_refresh_reuses_granted_scopes_when_oauth_omits_them(
    manager: AccountManager,
    fake_openai: FakeOpenAI,
) -> None:
    await manager.login("work")
    provider = manager.provider("work")
    stale = await provider.access_token()
    fake_openai.omit_refresh_scope = True
    assert await provider.access_token(refresh_if=stale) != stale


async def test_missing_keyring_record_requires_a_new_launch_after_login(
    manager: AccountManager,
    store: CredentialStore,
    tmp_path: Path,
) -> None:
    await manager.login("work")
    await manager.select("work")
    old_provider = manager.provider("work")
    index = json.loads((tmp_path / "profiles.json").read_text())
    store.delete(index["profiles"]["work"])
    await manager.login("work")
    assert await manager.selected() is None
    with pytest.raises(ReauthenticationRequiredError, match="removed"):
        await old_provider.access_token()


async def test_removed_profile_name_cannot_rebind_an_open_launch(manager: AccountManager) -> None:
    await manager.login("work")
    old_provider = manager.provider("work")
    await manager.select("work")
    assert await manager.remove("work") is True
    assert await manager.selected() is None
    await manager.login("work")
    with pytest.raises(ReauthenticationRequiredError, match="removed"):
        await old_provider.access_token()


async def test_logout_clears_local_credentials_when_remote_revocation_fails(
    manager: AccountManager,
    fake_openai: FakeOpenAI,
) -> None:
    await manager.login("work")
    fake_openai.revoke_status = 403
    assert await manager.logout("work") is False
    assert await manager.profiles() == ["work"]
    with pytest.raises(ReauthenticationRequiredError):
        await manager.provider("work").access_token()


async def test_denied_plan_scope_keeps_the_registration_for_consent(
    manager: AccountManager,
    fake_openai: FakeOpenAI,
) -> None:
    fake_openai.plan_allowed = False
    with pytest.raises(ReauthenticationRequiredError, match="allow plan usage"):
        await manager.login("work")
    assert await manager.profiles() == ["work"]
    with pytest.raises(ReauthenticationRequiredError):
        await manager.select("work")
    fake_openai.plan_allowed = True
    await manager.login("work")
    query = fake_openai.authorization_queries[-1]
    assert query["prompt"] == ["consent"]
    assert query["client_id"] == ["oaiapp_synthetic1"]


async def test_invalid_code_starts_fresh_browser_attempt_with_issued_client_id(
    manager: AccountManager,
    fake_openai: FakeOpenAI,
) -> None:
    fake_openai.expire_code = True
    await manager.login("work")
    first, second = fake_openai.authorization_queries
    assert first["client_id"] == ["dynamic_agent_client"]
    assert second["client_id"] == ["oaiapp_synthetic1"]
    assert first["state"] != second["state"]


async def test_returning_sign_in_rejects_different_account_and_preserves_active_tokens(
    manager: AccountManager,
    fake_openai: FakeOpenAI,
) -> None:
    await manager.login("work")
    await manager.select("work")
    active = await manager.provider("work").access_token()
    fake_openai.subject = "different-synthetic-subject"
    with pytest.raises(AccountError, match="different ChatGPT account"):
        await manager.login("work")
    assert await manager.selected() == "work"
    assert await manager.provider("work").access_token() == active


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("iss", "https://example.invalid"),
        ("nonce", "unexpected"),
        ("aud", "oaiapp_other"),
    ],
)
async def test_login_rejects_signed_identity_claims_for_another_attempt(
    manager: AccountManager,
    fake_openai: FakeOpenAI,
    claim: str,
    value: str,
) -> None:
    fake_openai.forged_claims[claim] = value
    with pytest.raises(InvalidIdentityError):
        await manager.login("work")
    assert await manager.profiles() == []


def test_callback_ignores_unrelated_requests_and_rejects_swapped_client_id() -> None:
    attempt = _Attempt.create()
    assert _parse_callback("state=other", attempt.state, "oaiapp_saved", _REDIRECT) is None
    query = urlencode({"state": attempt.state, "code": "synthetic", "client_id": "oaiapp_other"})
    result = _parse_callback(query, attempt.state, "oaiapp_saved", _REDIRECT)
    assert result is not None and result.error is not None and result.callback is None
    query = urlencode({"state": attempt.state, "code": "synthetic"})
    result = _parse_callback(query, attempt.state, "oaiapp_saved", _REDIRECT)
    assert result is not None and result.callback is not None
    assert result.callback.client_id == "oaiapp_saved"


async def test_refresh_waits_for_the_server_window(
    manager: AccountManager,
    fake_openai: FakeOpenAI,
    store: CredentialStore,
    tmp_path: Path,
) -> None:
    await manager.login("work")
    index = json.loads((tmp_path / "profiles.json").read_text())
    key = index["profiles"]["work"]
    registration = store.read(key)
    assert registration is not None and registration.tokens is not None
    current = time.time()
    tokens = replace(registration.tokens, refresh_after=current + 5, expires_at=current + 30)
    store.write(key, replace(registration, tokens=tokens))
    provider = manager.provider("work")
    assert await provider.access_token() == tokens.access_token
    with pytest.raises(AccountError, match="refresh window") as failure:
        await provider.access_token(refresh_if=tokens.access_token)
    assert failure.value.status_code == 503
    assert fake_openai.refreshes == 0
