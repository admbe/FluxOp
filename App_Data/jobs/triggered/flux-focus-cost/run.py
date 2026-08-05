from __future__ import annotations

import sys
from pathlib import Path


root = Path(__file__).resolve().parents[4]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from api.jobs import focus_cost_sync


raise SystemExit(focus_cost_sync())
