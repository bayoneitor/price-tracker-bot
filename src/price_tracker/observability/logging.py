"""structlog configuration emitting JSON to stdout.

Usage:
    from price_tracker.observability.logging import configure_logging
    configure_logging(level="INFO")
    log = structlog.get_logger(__name__)
    log.info("event.name", request_id="...", domain="amazon.com")
"""

from __future__ import annotations

import contextlib
import logging
import sys
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterator


def configure_logging(*, level: str = "INFO") -> None:
    """Configure structlog with JSON renderer to stdout.

    Idempotent — safe to call multiple times.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Routes the *standard library* `logging` module into the same JSON stream.
    # Most of this codebase calls `structlog.get_logger()`, which the config
    # above already covers — but `core.registry`'s plugin discovery, the
    # scrapers, and several third-party libraries use plain
    # `logging.getLogger(__name__)`. Left unconfigured, the stdlib root logger
    # has no handler, so every one of those calls — "Registered drop-in
    # scraper: amazon", the history-provider equivalent, a plugin's own import
    # error — was silently dropped rather than merely uninteresting.
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(log_level)


@contextlib.contextmanager
def bind_request_context(**ctx: Any) -> Iterator[None]:
    """Bind contextvars for the duration of the block.

    All structlog logs inside the block carry the bound key/values.
    """
    tokens = structlog.contextvars.bind_contextvars(**ctx)
    try:
        yield
    finally:
        structlog.contextvars.unbind_contextvars(*ctx.keys())
        del tokens
