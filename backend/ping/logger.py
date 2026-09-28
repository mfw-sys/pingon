"""Centralized logging configuration.

Kept deliberately simple: a single named logger ('ping_monitor') that
callers configure once via `configure_logging()`. Library code never
calls `logging.basicConfig()` itself, so embedding this engine into a
larger backend (e.g. a REST API) won't clobber the host app's logging
setup.
"""

from __future__ import annotations

import logging

LOGGER_NAME = "ping_monitor"


def get_logger() -> logging.Logger:
    """Return the shared library logger.

    If nothing has configured it yet, attach a NullHandler so the
    library stays silent by default (best practice for libraries) and
    never spams stderr with 'no handlers found' warnings.
    """
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def configure_logging(level: int = logging.INFO, verbose: bool = False) -> logging.Logger:
    """Configure console logging for the ping_monitor logger.

    Args:
        level: logging level, defaults to INFO. Use logging.WARNING or
            higher in production to avoid verbose per-packet logs.
        verbose: if True, forces DEBUG level regardless of `level`.
            Intended for local troubleshooting only, not production.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()

    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else level)
    logger.propagate = False
    return logger
