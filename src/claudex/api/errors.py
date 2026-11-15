from dataclasses import dataclass

from claudex.domain.accounts import AccountError
from claudex.domain.gateway import (
    GatewayError,
    InvalidMessagesRequestError,
    UnknownModelError,
    UpstreamRejectedError,
    UpstreamUnavailableError,
)
from claudex.domain.translation import Payload


@dataclass(frozen=True, slots=True)
class ErrorResponse:
    status: int
    type: str
    message: str
    retry_after: str | None = None
    retryable: bool | None = None

    def payload(self) -> Payload:
        return {"type": "error", "error": {"type": self.type, "message": self.message}}

    def headers(self) -> dict[str, str]:
        headers = {}
        if self.retry_after is not None:
            headers["Retry-After"] = self.retry_after
        if self.retryable is not None:
            headers["X-Should-Retry"] = str(self.retryable).lower()
        return headers


def gateway_error(exc: GatewayError | InvalidMessagesRequestError | AccountError) -> ErrorResponse:
    if isinstance(exc, UnknownModelError):
        return ErrorResponse(404, "not_found_error", str(exc))
    if isinstance(exc, InvalidMessagesRequestError):
        return ErrorResponse(400, "invalid_request_error", str(exc))
    if isinstance(exc, AccountError):
        error_type = "authentication_error" if exc.status_code == 401 else "api_error"
        return ErrorResponse(exc.status_code, error_type, str(exc), retryable=False)
    if isinstance(exc, UpstreamRejectedError):
        return _upstream_error(exc)
    if isinstance(exc, UpstreamUnavailableError):
        return ErrorResponse(503, "api_error", str(exc))
    return ErrorResponse(502, "api_error", str(exc))


def _upstream_error(exc: UpstreamRejectedError) -> ErrorResponse:
    match exc.code:
        case "context_length_exceeded":
            return ErrorResponse(
                400, "invalid_request_error", "capability_rejected: prompt_too_long"
            )
        case "subscription_sharing_usage_limit_exceeded":
            return ErrorResponse(
                429,
                "rate_limit_error",
                "ChatGPT plan usage reached a limit. See https://chatgpt.com/settings/usage",
                exc.retry_after,
                False,
            )
        case "subscription_sharing_user_not_eligible":
            return ErrorResponse(
                403, "permission_error", "This ChatGPT profile is ineligible for plan usage."
            )
        case "subscription_sharing_unsupported_capability":
            return ErrorResponse(
                400,
                "invalid_request_error",
                "The selected ChatGPT profile rejected this capability.",
            )
        case _:
            return _upstream_status(exc)


def _upstream_status(exc: UpstreamRejectedError) -> ErrorResponse:
    match exc.status:
        case 400 | 422:
            return ErrorResponse(
                400, "invalid_request_error", "OpenAI rejected the request options."
            )
        case 401:
            return ErrorResponse(
                401, "authentication_error", "Sign in to ChatGPT again.", retryable=False
            )
        case 403:
            return ErrorResponse(403, "permission_error", "OpenAI denied access for this profile.")
        case 404:
            return ErrorResponse(404, "not_found_error", "The mapped GPT model is unavailable.")
        case 429:
            return ErrorResponse(
                429, "rate_limit_error", "OpenAI rate limited the request.", exc.retry_after
            )
        case 503:
            return ErrorResponse(
                503, "api_error", "OpenAI Responses is unavailable.", exc.retry_after
            )
        case _:
            return ErrorResponse(502, "api_error", "OpenAI Responses did not accept the request.")
