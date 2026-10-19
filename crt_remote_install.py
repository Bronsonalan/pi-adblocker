#!/usr/bin/env python3
"""Install the remote CRT client. crt-remote-bootstrap.sh runs this."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from piadblock.crtremote import install_main

if __name__ == "__main__":
    raise SystemExit(install_main())
