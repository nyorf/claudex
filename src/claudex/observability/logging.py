import logging

import structlog


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for name in ("httpx2", "httpcore2", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
