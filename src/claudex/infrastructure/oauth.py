import asyncio
import base64
import hashlib
import json
import re
import time
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from hmac import compare_digest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from secrets import token_urlsafe
from threading import Event
from typing import Any, override
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from claudex.domain.accounts import AccountError, ChatGPTTokens, Registration

_ISSUER = "https://auth.openai.com"
_AUTHORIZE_URL = f"{_ISSUER}/api/accounts/authorize"
_TOKEN_URL = f"{_ISSUER}/api/accounts/oauth/token"
_JWKS_URL = f"{_ISSUER}/.well-known/jwks.json"
_RESOURCE = "https://api.openai.com/v1"
_REGISTER_CLIENT = "dynamic_agent_client"
_PLAN_SCOPE = "chatgpt.tokens.use.direct"
_SCOPES = f"openid profile email offline_access resource.invoke {_PLAN_SCOPE}"
_CALLBACK_PATH = "/auth/callback"
_COMPLETE_PATH = "/auth/complete"
_CALLBACK_TIMEOUT = 300
_HTTP_TIMEOUT = 15
_CLOCK_TOLERANCE = 5
_REFRESH_GRANT = "refresh_token"
_ISSUED_CLIENT_ID = re.compile(r"[a-zA-Z0-9_-]{1,200}\Z")
_REFRESH_FAILURES = frozenset(
    {
        "invalid_grant",
        "invalid_refresh_token",
        "token_expired",
        "refresh_token_expired",
        "refresh_token_invalidated",
        "refresh_token_reused",
    }
)


class ExpiredRefreshError(AccountError):
    pass


class InvalidIdentityError(AccountError):
    pass


class InvalidTokenResponseError(AccountError):
    pass


class CodeExchangeExpiredError(AccountError):
    def __init__(self, client_id: str) -> None:
        super().__init__("The sign-in code expired. Start a new browser sign-in.")
        self.client_id = client_id


class _TokenResponse(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    scope: str
    id_token: str | None = Field(default=None, repr=False)
    access_token: str | None = Field(default=None, repr=False)
    refresh_token: str | None = Field(default=None, repr=False)
    token_type: str | None = None
    expires_in: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    earliest_refresh_at: int | str | None = None

    @property
    def scopes(self) -> tuple[str, ...]:
        return tuple(self.scope.split())

    def tokens(self, received_at: float, *, previous: ChatGPTTokens | None = None) -> ChatGPTTokens:
        if (
            not self.access_token
            or not self.refresh_token
            or self.expires_in is None
            or self.token_type is None
            or self.token_type.casefold() != "bearer"
        ):
            raise InvalidTokenResponseError(
                "ChatGPT returned incomplete credentials. Sign in again."
            )
        return ChatGPTTokens(
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            id_token=self.id_token or (previous.id_token if previous else ""),
            expires_at=received_at + self.expires_in,
            refresh_after=_refresh_time(self.earliest_refresh_at),
            scopes=self.scopes,
            identity_pending_since=received_at if previous and self.id_token else None,
        )


@dataclass(frozen=True, slots=True)
class _Attempt:
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    verifier: str = field(repr=False)

    @classmethod
    def create(cls) -> _Attempt:
        return cls(token_urlsafe(32), token_urlsafe(32), token_urlsafe(32))

    def authorization_url(
        self,
        redirect_uri: str,
        host_id: str,
        client_id: str,
        registration: Registration | None,
    ) -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest())
        parameters = {
            "client_id": client_id,
            "ext_agent_host_id": host_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": _SCOPES,
            "resource": _RESOURCE,
            "state": self.state,
            "nonce": self.nonce,
            "code_challenge_method": "S256",
            "code_challenge": challenge.rstrip(b"=").decode(),
        }
        if client_id == _REGISTER_CLIENT:
            parameters["agent_name_hint"] = "Claudex"
        if registration and registration.needs_consent:
            parameters["prompt"] = "consent"
        elif registration and registration.tokens and registration.tokens.id_token:
            parameters["id_token_hint"] = registration.tokens.id_token
        return f"{_AUTHORIZE_URL}?{urlencode(parameters)}"


@dataclass(frozen=True, slots=True)
class _Callback:
    code: str = field(repr=False)
    client_id: str = field(repr=False)
    redirect_uri: str


type BrowserAuthorize = Callable[[_Attempt, str, str, Registration | None], Awaitable[_Callback]]


@dataclass(frozen=True, slots=True)
class _BrowserResult:
    callback: _Callback | None = field(default=None, repr=False)
    error: AccountError | None = None


def _parse_callback(
    query: str,
    state: str,
    client_id: str,
    redirect_uri: str,
) -> _BrowserResult | None:
    parameters = parse_qs(query, keep_blank_values=True)
    states = parameters.get("state", [])
    if len(states) != 1 or not compare_digest(states[0].encode(), state.encode()):
        return None
    if "error" in parameters:
        return _BrowserResult(error=AccountError("ChatGPT sign-in was declined or cancelled."))
    codes = parameters.get("code", [])
    clients = parameters.get("client_id", [])
    if len(codes) != 1 or not codes[0] or len(clients) > 1:
        return _BrowserResult(error=AccountError("ChatGPT returned an invalid sign-in callback."))
    issued_id = clients[0] if clients else client_id
    if client_id != _REGISTER_CLIENT and issued_id != client_id:
        return _BrowserResult(error=AccountError("ChatGPT returned a different registration."))
    if issued_id == _REGISTER_CLIENT or not _ISSUED_CLIENT_ID.fullmatch(issued_id):
        return _BrowserResult(error=AccountError("ChatGPT did not return an issued client ID."))
    return _BrowserResult(callback=_Callback(codes[0], issued_id, redirect_uri))


class _LoopbackServer(HTTPServer):
    allow_reuse_address = False

    def __init__(self, state: str, client_id: str) -> None:
        super().__init__(("127.0.0.1", 0), _CallbackHandler)
        self.timeout = 0.25
        self.host = f"127.0.0.1:{self.server_port}"
        self.redirect_uri = f"http://{self.host}{_CALLBACK_PATH}"
        self.start_path = f"/auth/start/{token_urlsafe(24)}"
        self.authorization_url: str | None = None
        self.state = state
        self.client_id = client_id
        self.result: _BrowserResult | None = None
        self.complete = False

    @override
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(1)
        return connection, address

    @override
    def handle_error(self, request, client_address) -> None:
        pass


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _LoopbackServer

    @override
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        try:
            path = urlsplit(self.path)
        except ValueError:
            self._reply(HTTPStatus.BAD_REQUEST)
            return
        if self.headers.get("Host") != self.server.host or path.netloc or path.scheme:
            self._reply(HTTPStatus.NOT_FOUND)
            return
        if path.path == self.server.start_path and self.server.authorization_url:
            self._reply(HTTPStatus.SEE_OTHER, location=self.server.authorization_url)
            self.server.authorization_url = None
        elif path.path == _CALLBACK_PATH and self.server.result is None:
            self._callback(path.query)
        elif path.path == _COMPLETE_PATH and self.server.result is not None:
            self._reply(HTTPStatus.OK, "Return to Claudex to finish signing in.")
            self.server.complete = True
        else:
            self._reply(HTTPStatus.NOT_FOUND)

    def _callback(self, query: str) -> None:
        result = _parse_callback(
            query,
            self.server.state,
            self.server.client_id,
            self.server.redirect_uri,
        )
        if result is None:
            self._reply(HTTPStatus.BAD_REQUEST, "Return to the browser tab that started sign-in.")
            return
        self.server.result = result
        self._reply(HTTPStatus.SEE_OTHER, location=_COMPLETE_PATH)

    def _reply(self, status: HTTPStatus, message: str = "", *, location: str | None = None) -> None:
        body = message.encode()
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(body)


def _wait_for_browser(
    attempt: _Attempt,
    host_id: str,
    client_id: str,
    registration: Registration | None,
    stop: Event,
) -> _Callback:
    try:
        server = _LoopbackServer(attempt.state, client_id)
    except OSError:
        raise AccountError("Could not open the local sign-in callback.", status_code=503) from None
    with server:
        server.authorization_url = attempt.authorization_url(
            server.redirect_uri,
            host_id,
            client_id,
            registration,
        )
        # open only a local start URL so browser command arguments carry no OAuth parameters
        try:
            opened = webbrowser.open(f"http://{server.host}{server.start_path}")
        except OSError, webbrowser.Error:
            opened = False
        if not opened:
            raise AccountError("Open a default browser on this machine and try signing in again.")
        deadline = time.monotonic() + _CALLBACK_TIMEOUT
        while not stop.is_set() and not server.complete and time.monotonic() < deadline:
            server.handle_request()
            if server.result is not None:
                deadline = min(deadline, time.monotonic() + 1)
        if server.result and server.result.error:
            raise server.result.error
        if server.result and server.result.callback:
            return server.result.callback
        raise AccountError("ChatGPT sign-in timed out. Start sign-in again.")


async def _browser(
    attempt: _Attempt,
    host_id: str,
    client_id: str,
    registration: Registration | None,
) -> _Callback:
    stop = Event()
    try:
        return await asyncio.to_thread(
            _wait_for_browser,
            attempt,
            host_id,
            client_id,
            registration,
            stop,
        )
    finally:
        stop.set()


def _refresh_time(value: int | str | None) -> float:
    if isinstance(value, int):
        return float(value)
    if isinstance(value, str):
        try:
            return float(int(value))
        except ValueError:
            try:
                time_value = datetime.fromisoformat(value)
                if time_value.tzinfo is not None:
                    return time_value.timestamp()
            except ValueError:
                pass
    return 0


class OAuthClient:
    def __init__(
        self,
        client: httpx2.AsyncClient,
        *,
        authorize: BrowserAuthorize = _browser,
    ) -> None:
        self._client = client
        self._authorize = authorize

    async def register(self, host_id: str, registration: Registration | None) -> Registration:
        client_id = registration.client_id if registration else _REGISTER_CLIENT
        try:
            return await self._sign_in(host_id, client_id, registration)
        except CodeExchangeExpiredError as error:
            return await self._sign_in(host_id, error.client_id, registration)

    async def _sign_in(
        self,
        host_id: str,
        client_id: str,
        registration: Registration | None,
    ) -> Registration:
        attempt = _Attempt.create()
        callback = await self._authorize(attempt, host_id, client_id, registration)
        data = await self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": callback.client_id,
                "code": callback.code,
                "code_verifier": attempt.verifier,
                "redirect_uri": callback.redirect_uri,
                "resource": _RESOURCE,
            }
        )
        received_at = time.time()
        subject = await self._verify_identity(
            data.id_token,
            callback.client_id,
            received_at,
            nonce=attempt.nonce,
        )
        if registration is not None and registration.subject != subject:
            raise AccountError("The browser selected a different ChatGPT account.")
        if _PLAN_SCOPE not in data.scopes:
            return Registration(callback.client_id, subject, needs_consent=True)
        return Registration(callback.client_id, subject, data.tokens(received_at))

    async def refresh(self, registration: Registration, tokens: ChatGPTTokens) -> ChatGPTTokens:
        data = await self._token_request(
            {
                "grant_type": _REFRESH_GRANT,
                "client_id": registration.client_id,
                "refresh_token": tokens.refresh_token,
                "resource": _RESOURCE,
            },
            previous_scopes=tokens.scopes,
        )
        if _PLAN_SCOPE not in data.scopes:
            raise ExpiredRefreshError("ChatGPT plan access ended. Sign in again.")
        if data.id_token is not None and not data.id_token:
            raise InvalidIdentityError("ChatGPT returned an empty refreshed identity token.")
        return data.tokens(time.time(), previous=tokens)

    async def confirm_refreshed_identity(
        self,
        registration: Registration,
        tokens: ChatGPTTokens,
    ) -> ChatGPTTokens:
        if tokens.identity_pending_since is None:
            return tokens
        subject = await self._verify_identity(
            tokens.id_token,
            registration.client_id,
            tokens.identity_pending_since,
        )
        if registration.subject != subject:
            raise InvalidIdentityError("The refreshed ChatGPT identity did not match the profile.")
        return replace(tokens, identity_pending_since=None)

    async def revoke(self, registration: Registration, tokens: ChatGPTTokens) -> bool:
        for attempt in range(2):
            try:
                response = await self._send_revocation(registration.client_id, tokens.refresh_token)
                if response.status_code == HTTPStatus.OK:
                    return True
                if response.status_code < HTTPStatus.INTERNAL_SERVER_ERROR:
                    return False
            except AccountError:
                pass
            if attempt == 0:
                await asyncio.sleep(0.5)
        return False

    async def _send_revocation(self, client_id: str, refresh_token: str) -> httpx2.Response:
        discovery = await self._json_get(f"{_ISSUER}/.well-known/openid-configuration")
        endpoint = discovery.get("revocation_endpoint")
        if discovery.get("issuer") != _ISSUER or not _same_issuer(endpoint):
            raise AccountError("ChatGPT sign-out configuration could not be verified.")
        assert isinstance(endpoint, str)
        return await self._request(
            "POST",
            endpoint,
            data={
                "client_id": client_id,
                "token": refresh_token,
                "token_type_hint": _REFRESH_GRANT,
            },
        )

    async def _token_request(
        self,
        form: dict[str, str],
        *,
        previous_scopes: tuple[str, ...] | None = None,
    ) -> _TokenResponse:
        response = await self._request("POST", _TOKEN_URL, data=form)
        if response.is_success:
            try:
                payload = _object(response)
                if previous_scopes is not None and "scope" not in payload:
                    payload["scope"] = " ".join(previous_scopes)
                return _TokenResponse.model_validate(payload)
            except AccountError, ValidationError:
                raise InvalidTokenResponseError(
                    "ChatGPT returned an invalid token response."
                ) from None
        if response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR or response.status_code == 429:
            raise AccountError(
                "ChatGPT sign-in is unavailable. Try again shortly.", status_code=503
            )
        payload = _object(response)
        error = payload.get("error")
        code = error.get("code") if isinstance(error, dict) else error
        if (
            form["grant_type"] == _REFRESH_GRANT
            and isinstance(code, str)
            and code in _REFRESH_FAILURES
        ):
            raise ExpiredRefreshError("The ChatGPT session expired. Sign in again.")
        if code == "invalid_grant" and form["grant_type"] == "authorization_code":
            raise CodeExchangeExpiredError(form["client_id"])
        if code == "invalid_client":
            raise AccountError(
                "ChatGPT rejected this registration. Remove the profile and sign in again."
            )
        raise AccountError("ChatGPT declined the request. Check the profile and try again.")

    async def _verify_identity(
        self,
        id_token: str | None,
        client_id: str,
        received_at: float,
        *,
        nonce: str | None = None,
    ) -> str:
        if not id_token:
            raise InvalidIdentityError("ChatGPT did not return an identity token.")
        header, claims, signature, signed = _decode_identity_token(id_token)
        keys = await self._json_get(_JWKS_URL)
        _verify_signature(header, signature, signed, keys)
        return _verify_claims(claims, client_id, received_at, nonce)

    async def _json_get(self, url: str) -> dict[str, Any]:
        response = await self._request("GET", url)
        if not response.is_success:
            raise AccountError("ChatGPT identity services are unavailable.", status_code=503)
        return _object(response)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
    ) -> httpx2.Response:
        try:
            async with asyncio.timeout(_HTTP_TIMEOUT):
                return await self._client.request(
                    method,
                    url,
                    data=data,
                    headers={"Accept": "application/json"},
                    timeout=_HTTP_TIMEOUT,
                    follow_redirects=False,
                )
        except TimeoutError, httpx2.RequestError:
            raise AccountError(
                "Could not reach ChatGPT sign-in services.", status_code=503
            ) from None


def _same_issuer(url: object) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.netloc == "auth.openai.com" and not parsed.fragment


def _object(response: httpx2.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise AccountError("ChatGPT returned an invalid sign-in response.", status_code=503)
    return payload


def _decode_base64(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _decode_identity_token(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    try:
        raw_header, raw_claims, raw_signature = token.split(".")
        header = json.loads(_decode_base64(raw_header))
        claims = json.loads(_decode_base64(raw_claims))
        signature = _decode_base64(raw_signature)
        signed = f"{raw_header}.{raw_claims}".encode()
    except ValueError:
        raise InvalidIdentityError("ChatGPT returned a malformed identity token.") from None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise InvalidIdentityError("ChatGPT returned a malformed identity token.")
    return header, claims, signature, signed


def _verify_signature(
    header: dict[str, Any],
    signature: bytes,
    signed: bytes,
    jwks: dict[str, Any],
) -> None:
    kid = header.get("kid")
    if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid or header.get("crit"):
        raise InvalidIdentityError("ChatGPT returned an unsupported identity token.")
    keys = jwks.get("keys")
    if not isinstance(keys, list):
        raise AccountError("ChatGPT identity keys are temporarily unavailable.", status_code=503)
    matching = [key for key in keys if isinstance(key, dict) and key.get("kid") == kid]
    if len(matching) != 1:
        raise AccountError("ChatGPT identity keys are temporarily unavailable.", status_code=503)
    key = matching[0]
    if (
        key.get("kty") != "RSA"
        or key.get("alg", "RS256") != "RS256"
        or key.get("use", "sig") != "sig"
    ):
        raise InvalidIdentityError("ChatGPT returned an unsupported identity signing key.")
    try:
        exponent = int.from_bytes(_decode_base64(key["e"]), "big")
        modulus = int.from_bytes(_decode_base64(key["n"]), "big")
        public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
        public_key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature, KeyError, TypeError, ValueError:
        raise InvalidIdentityError(
            "The ChatGPT identity signature could not be verified."
        ) from None


def _verify_claims(claims: dict[str, Any], client_id: str, now: float, nonce: str | None) -> str:
    audience = claims.get("aud")
    if isinstance(audience, str):
        audience = [audience]
    authorized_party = claims.get("azp", client_id if audience == [client_id] else None)
    if (
        claims.get("iss") != _ISSUER
        or not isinstance(audience, list)
        or client_id not in audience
        or authorized_party != client_id
    ):
        raise InvalidIdentityError("The ChatGPT identity was issued for a different client.")
    expires, issued, valid_from = claims.get("exp"), claims.get("iat"), claims.get("nbf", 0)
    if (
        type(expires) is not int
        or type(issued) is not int
        or type(valid_from) is not int
        or expires <= now - _CLOCK_TOLERANCE
        or issued > now + _CLOCK_TOLERANCE
        or valid_from > now + _CLOCK_TOLERANCE
    ):
        raise InvalidIdentityError("The ChatGPT identity token is outside its validity period.")
    if nonce is not None and claims.get("nonce") != nonce:
        raise InvalidIdentityError("The ChatGPT identity did not match this sign-in attempt.")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise InvalidIdentityError("ChatGPT did not identify the account.")
    return subject
