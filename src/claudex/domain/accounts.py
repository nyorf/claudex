from dataclasses import dataclass, field
from typing import Protocol


class AccountError(Exception):
    def __init__(self, message: str, *, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


class ReauthenticationRequiredError(AccountError):
    pass


@dataclass(frozen=True, slots=True)
class ChatGPTTokens:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    id_token: str = field(repr=False)
    expires_at: float
    refresh_after: float
    scopes: tuple[str, ...]
    identity_pending_since: float | None = None


@dataclass(frozen=True, slots=True)
class Registration:
    client_id: str = field(repr=False)
    subject: str = field(repr=False)
    tokens: ChatGPTTokens | None = field(default=None, repr=False)
    needs_consent: bool = False


class TokenProvider(Protocol):
    async def access_token(self, *, refresh_if: str | None = None) -> str: ...
