from __future__ import annotations

import sys
from pathlib import Path


root = Path(__file__).resolve().parents[4]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from api.jobs import _with_publication, price_sheet_sync


raise SystemExit(_with_publication(price_sheet_sync()))
