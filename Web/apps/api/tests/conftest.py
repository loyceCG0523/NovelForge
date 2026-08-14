"""Keep API and Worker imports stable regardless of the repository parent folder."""

from __future__ import annotations

import sys
from pathlib import Path


API_DIR = Path(__file__).resolve().parents[1]
WORKER_DIR = API_DIR.parent / "worker"

for path in (API_DIR, WORKER_DIR):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)

