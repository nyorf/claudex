class InvalidMessagesRequestError(ValueError):
    pass


class UnknownModelError(InvalidMessagesRequestError):
    pass


class GatewayError(Exception):
    pass


class UpstreamUnavailableError(GatewayError):
    pass


class UpstreamProtocolError(GatewayError):
    pass


class UpstreamRejectedError(GatewayError):
    def __init__(self, status: int, code: str | None, retry_after: str | None = None) -> None:
        super().__init__("OpenAI declined the request.")
        self.status = status
        self.code = code
        self.retry_after = retry_after
