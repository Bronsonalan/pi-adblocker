"""Logging setup shared by the server and the command line."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_file: str, level: str) -> logging.Logger:
    """Configure the ``piadblock`` logger.

    Logs always go to stderr (so ``journalctl`` sees them). When ``log_file``
    is set, the same records are also written to a small rotating file.
    """
    logger = logging.getLogger("piadblock")
    logger.handlers.clear()
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    if log_file:
        try:
            path = Path(log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            rotating = RotatingFileHandler(
                path, maxBytes=1_048_576, backupCount=3, encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("could not open log file %s: %s", log_file, exc)
        else:
            rotating.setFormatter(formatter)
            logger.addHandler(rotating)
    return logger
