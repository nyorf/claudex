import logging
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

_CACHE_LOGGER = "claudex.cache"
_CACHE_LOG_BYTES = 1024 * 1024
_CACHE_LOG_BACKUPS = 2


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


def log_cache_usage(
    *,
    request_id: str,
    model: str,
    cached_input_tokens: int,
    uncached_input_tokens: int,
    cache_write_input_tokens: int,
) -> None:
    structlog.get_logger(_CACHE_LOGGER).info(
        "inference_cache_usage",
        request_id=request_id,
        model=model,
        cache_status="hit" if cached_input_tokens else "miss",
        cached_input_tokens=cached_input_tokens,
        uncached_input_tokens=uncached_input_tokens,
        cache_write_input_tokens=cache_write_input_tokens,
        total_input_tokens=cached_input_tokens + uncached_input_tokens,
    )


@contextmanager
def cache_log_file(path: Path | None) -> Iterator[None]:
    if path is None:
        yield
        return
    logger = logging.getLogger(_CACHE_LOGGER)
    try:
        for index in range(_CACHE_LOG_BACKUPS + 1):
            candidate = path if index == 0 else path.with_name(f"{path.name}.{index}")
            with suppress(FileNotFoundError):
                info = candidate.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise OSError
        handler = RotatingFileHandler(
            path, maxBytes=_CACHE_LOG_BYTES, backupCount=_CACHE_LOG_BACKUPS, encoding="utf-8"
        )
    except OSError:
        logger.warning("Claudex could not open the cache usage log.")
        yield
        return
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        handler.close()
