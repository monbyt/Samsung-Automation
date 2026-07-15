"""
Cron scheduler for mail jobs — runs only when due, never on startup.
"""
import os
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

from datetime import datetime

import config
from db import record_monitor_run, init_db
from mail.jobs_db import get_due_jobs, get_job, job_as_filter, mark_job_finished, seed_from_config
from mail.reader import run_mail_check
from parse_to_db import ingest_download

_lock = threading.Lock()
_running = False


def _ingest_item(item):
    if not item.get("table"):
        print(f"  No SQL table set for {item['filter_id']} — download only, skipping parse")
        return

    from parse_to_db import file_hash, get_last_ingest_hash_for_filter, ingest_download

    h = file_hash(item["path"])
    fid = item["filter_id"]
    if get_last_ingest_hash_for_filter(fid) == h:
        print(f"  Unchanged file for {fid}, skipping SQL load")
        return

    ingest_download(
        item["path"],
        table=item["table"],
        filter_id=fid,
        mail_subject=item["subject"],
        ingest_mode=item.get("ingest_mode", "replace"),
        extract_zip=item.get("extract_zip", False),
    )


def _run_rpas_for_downloads(job_id: str, downloads: list) -> list:
    """Fire linked RPAs for each downloaded file — called AFTER the mail
    Playwright context is closed (nested sync contexts are forbidden).
    """
    from rpa.runner import trigger_for_mail_job

    errors = []
    for item in downloads:
        try:
            trigger_for_mail_job(job_id, upload_file=item["path"])
        except Exception as e:
            msg = f"RPA trigger failed for {item.get('path')}: {e}"
            print(msg)
            errors.append(msg)
    return errors


def run_job(job_id: str) -> dict:
    """Download mail for one job and parse attachments into SQL, then run RPAs."""
    job = get_job(job_id)
    if not job:
        raise ValueError(f"Unknown job: {job_id}")

    print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] Running job: {job_id}")

    with _lock:
        # Ingest happens per-file (no Playwright). RPAs are deferred to
        # after run_mail_check returns so we're not inside its sync
        # Playwright context when we call another sync Playwright script.
        summary = run_mail_check(
            filters=[job_as_filter(job)],
            on_download=_ingest_item,
        )
        summary["job_id"] = job_id

        rpa_errors = _run_rpas_for_downloads(job_id, summary["downloads"])
        summary["errors"].extend(rpa_errors)

        if summary["errors"]:
            mark_job_finished(job_id, "error", "; ".join(summary["errors"]))
        else:
            mark_job_finished(job_id, "ok")

        record_monitor_run(summary, job_id=job_id)

    print(
        f"Job {job_id} done — {len(summary['downloads'])} file(s), "
        f"{len(summary['errors'])} error(s)"
    )
    return summary


def tick():
    """Run all jobs that are due right now."""
    due = get_due_jobs()
    for job in due:
        try:
            run_job(job["job_id"])
        except Exception:
            err = traceback.format_exc()[-500:]
            print(f"Job {job['job_id']} failed:\n{err}")
            mark_job_finished(job["job_id"], "error", err)
            record_monitor_run({
                "checked_at": datetime.now(),
                "downloads": [],
                "errors": [err],
                "job_id": job["job_id"],
            })


def scheduler_loop():
    """Background loop — checks every minute, does NOT run jobs on startup."""
    global _running
    if _running:
        return
    _running = True

    init_db()
    seed_from_config()
    from rpa.jobs_db import seed_from_config as seed_rpa
    seed_rpa()
    tick_seconds = getattr(config, "SCHEDULER_TICK_SECONDS", 60)

    print(
        f"Mail scheduler started — checking every {tick_seconds}s for due jobs.\n"
        "Jobs will NOT run immediately on startup; manage them from the dashboard.\n"
    )

    while True:
        try:
            tick()
        except Exception:
            print("Scheduler tick failed:")
            traceback.print_exc()
        time.sleep(tick_seconds)


def start_background():
    """Start the cron loop in a daemon thread (used by dashboard)."""
    t = threading.Thread(target=scheduler_loop, daemon=True, name="mail-cron")
    t.start()
    return t


if __name__ == "__main__":
    scheduler_loop()
