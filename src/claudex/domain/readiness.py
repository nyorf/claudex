import hmac


def readiness_proof(token: bytes, challenge: str) -> str:
    message = b"claudex-desktop-ready-v1:" + challenge.encode("ascii")
    return hmac.digest(token, message, "sha256").hex()
