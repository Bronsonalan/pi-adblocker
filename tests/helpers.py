"""Put the repo root on sys.path and keep test logs quiet."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_logger = logging.getLogger("piadblock")
_logger.handlers.clear()
_logger.addHandler(logging.NullHandler())
_logger.setLevel(logging.CRITICAL)
_logger.propagate = False
