from pathlib import Path
import sys

application_root = Path("/home/site/wwwroot")
if not (application_root / "api").exists():
    application_root = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(application_root))

from api.jobs import scheduled_sync


raise SystemExit(scheduled_sync(["cost"]))
