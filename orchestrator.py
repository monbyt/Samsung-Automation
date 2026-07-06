"""
Orchestrator — runs the whole pipeline once: download -> parse into DB.

Run it with:  python orchestrator.py   (or double-click run_now.bat)

This is the single source of truth for 'do one full cycle'. The scheduler
just calls this on a timer.
"""
from datetime import datetime

import config
from download import download_latest
from parse_to_db import ingest_download
from db import init_db


def run_pipeline():
    init_db()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] === Order Extract pipeline ===")

    print("Step 1/2  Downloading latest file...")
    path = download_latest()
    print(f"          Saved: {path}")

    print("Step 2/2  Parsing into database...")
    ingest_download(path, table=config.ORDERS_TABLE, filter_id="order_extract")

    done = datetime.now().strftime("%H:%M:%S")
    print(f"[{done}] Pipeline complete.")
    return path


if __name__ == "__main__":
    run_pipeline()
