"""
Continuous W1 mail monitor.

Keeps checking configured mail filters on an interval, decrypts Excel
attachments, and loads them into the SQL database.

Run on the always-on PC:  python mail/monitor.py
(or double-click run_monitor.bat)
"""
import os
import sys
import time
import traceback
from datetime import datetime

# Allow running as script or module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

import config
from db import record_monitor_run, init_db
from mail.reader import run_mail_check
from parse_to_db import ingest_download


def _handle_download(item):
    ingest_download(
        item["path"],
        table=item["table"],
        filter_id=item["filter_id"],
        mail_subject=item["subject"],
    )


def run_once():
    print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] === Mail check ===")
    summary = run_mail_check(on_download=_handle_download)
    record_monitor_run(summary)
    print(
        f"Done — {len(summary['downloads'])} file(s) downloaded, "
        f"{len(summary['errors'])} error(s)"
    )
    return summary


def run_forever():
    init_db()
    interval = config.MONITOR_INTERVAL_MINUTES * 60
    print(
        f"Mail monitor started — checking every {config.MONITOR_INTERVAL_MINUTES} min.\n"
        f"Filters: {', '.join(f['id'] for f in config.MAIL_FILTERS)}\n"
        "Leave this window open. Press Ctrl+C to stop.\n"
    )
    while True:
        try:
            run_once()
        except Exception:
            print("Mail check failed:")
            traceback.print_exc()
            record_monitor_run({
                "checked_at": datetime.now(),
                "downloads": [],
                "errors": ["fatal: " + traceback.format_exc()[-500:]],
            })

        nxt = datetime.now().strftime("%H:%M:%S")
        print(f"[{nxt}] Sleeping {config.MONITOR_INTERVAL_MINUTES} min...\n")
        time.sleep(interval)


if __name__ == "__main__":
    run_forever()
