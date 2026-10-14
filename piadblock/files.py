"""Small file helpers shared by the updater and the stats writer."""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write(path: str | Path, data: str | bytes) -> None:
    """Replace ``path`` with ``data``. Readers never see a half-written file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    mode = "w" if isinstance(data, str) else "wb"
    with temporary.open(mode, encoding=None if "b" in mode else "utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
