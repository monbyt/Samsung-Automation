"""
Orchestrator — runs the whole pipeline once: download -> parse into DB.

Run it with:  python orchestrator.py   (or double-click run_now.bat)

This is the single source of truth for 'do one full cycle'. The scheduler
just calls this on a timer.
"""
from datetime import datetime

from download import download_latest
from parse_to_db import parse_file


def run_pipeline():
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] === Order Extract pipeline ===")

    print("Step 1/2  Downloading latest file...")
    path = download_latest()
    print(f"          Saved: {path}")

    print("Step 2/2  Parsing into database...")
    parse_file(path)

    done = datetime.now().strftime("%H:%M:%S")
    print(f"[{done}] Pipeline complete.")
    return path


if __name__ == "__main__":
    run_pipeline()
