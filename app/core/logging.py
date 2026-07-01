"""Application logging configuration."""

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger with structured output.

    Args:
        level: One of ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``, ``CRITICAL``.
    """
    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Avoid duplicate handlers when reloading.
    if not root.handlers:
        root.addHandler(handler)
    else:
        root.handlers = [handler]

    # Keep third-party loggers at a reasonable level.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("botasaurus").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the *scraper-api* namespace."""
    return logging.getLogger(f"scraper-api.{name}")
