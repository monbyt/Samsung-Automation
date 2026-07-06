"""
The 'cron job'. Keep this running and it runs the full pipeline
(download + parse) every INTERVAL_HOURS (set in config.py).

Run it with:  python scheduler.py     (or double-click run_scheduler.bat)
Stop it with: Ctrl + C
"""
import time
import traceback
from datetime import datetime

import config
from orchestrator import run_pipeline


if __name__ == "__main__":
    print(
        f"Scheduler started — running every {config.INTERVAL_HOURS} hour(s).\n"
        "Leave this window open. Press Ctrl+C to stop.\n"
    )
    while True:
        try:
            run_pipeline()
        except Exception:
            print("Pipeline run failed:")
            traceback.print_exc()

        nxt = datetime.now().strftime("%H:%M:%S")
        print(f"[{nxt}] Sleeping {config.INTERVAL_HOURS}h until next run...\n")
        time.sleep(config.INTERVAL_HOURS * 3600)
