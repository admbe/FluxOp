from pathlib import Path
import sys


application_root = Path("/home/site/wwwroot")
if not (application_root / "api").exists():
    application_root = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(application_root))

from api.jobs import analytics_history_prune, scheduled_sync


sync_result = scheduled_sync(["inventory", "policy"])
prune_result = analytics_history_prune()
raise SystemExit(sync_result or prune_result)
