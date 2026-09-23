"""
migrate_progress_to_db.py

Run this ONCE, manually, before you run run_upload.py with the new
SQLite-backed ProgressTracker. It imports your existing
customers/progress/processed_orders.json (103 keys as of writing) into
the new customers/progress/processed_orders.db, so nothing you've
already uploaded gets treated as new.

Safe to run more than once by accident -- import is idempotent
(INSERT OR IGNORE), so re-running just imports 0 new keys the second time.

Usage (from the apps/sniper folder, same place you run run_upload.py from):
    python migrate_progress_to_db.py
"""

from pathlib import Path
import sys


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
    
    
from apps.sniper.product_upload.progress_tracker import ProgressTracker
   
    
_SCRIPT_DIR = Path(__file__).resolve().parent
PROGRESS_DIR = _SCRIPT_DIR / "customers" / "progress"

OLD_JSON_PATH = PROGRESS_DIR / "processed_orders.json"
NEW_DB_PATH = PROGRESS_DIR / "processed_orders.db"


def main() -> None:
    print(f"Old JSON file : {OLD_JSON_PATH}")
    print(f"New DB file   : {NEW_DB_PATH}")
    print()

    tracker = ProgressTracker(path=str(NEW_DB_PATH))
    imported = tracker.import_from_json_backup(str(OLD_JSON_PATH))
    print(f"\nDone. {imported} key(s) imported. Total now in DB: {tracker.count}")
    tracker.close()


if __name__ == "__main__":
    main()