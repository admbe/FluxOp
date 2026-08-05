from __future__ import annotations

import sys
from pathlib import Path


root = Path(__file__).resolve().parents[4]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from api.jobs import rightsizing_proposal_sync


raise SystemExit(rightsizing_proposal_sync())
