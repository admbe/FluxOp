from __future__ import annotations

import os
import sys
from pathlib import Path


os.environ.setdefault("FLUX_DUCKDB_MEMORY_LIMIT", "1GB")
os.environ.setdefault("FLUX_DUCKDB_MAX_TEMP_DIRECTORY_SIZE", "8GB")

root = Path(__file__).resolve().parents[4]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from api.jobs import cost_history_sync


raise SystemExit(cost_history_sync())
