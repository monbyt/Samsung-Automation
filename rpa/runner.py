"""
Run RPA tools — manually or after a mail job finishes.
"""
import os
import shutil
import traceback
from typing import Optional

import config
from db import record_rpa_run
from rpa.jobs_db import get_rpa_job, list_for_mail_job, mark_rpa_finished


def _prepare_upload_file(upload_file: Optional[str]) -> str:
    if upload_file and os.path.isfile(upload_file):
        return upload_file
    if os.path.isfile(config.NERP_UPLOAD_FILE):
        return config.NERP_UPLOAD_FILE
    raise FileNotFoundError(
        "No upload file — run a mail job first or set NERP_UPLOAD_FILE in config."
    )


def run_rpa(rpa_id: str, upload_file: Optional[str] = None) -> dict:
    """Run one RPA tool by id."""
    job = get_rpa_job(rpa_id)
    if not job:
        raise ValueError(f"Unknown RPA job: {rpa_id}")

    print(f"\n[RPA] Running {job['name']} ({rpa_id})...")
    result = {"rpa_id": rpa_id, "status": "ok", "message": ""}

    try:
        if job["tool"] == "nerp":
            from nerp.rpa import run as nerp_run

            path = _prepare_upload_file(upload_file)
            if path != config.NERP_UPLOAD_FILE:
                os.makedirs(os.path.dirname(config.NERP_UPLOAD_FILE), exist_ok=True)
                shutil.copy2(path, config.NERP_UPLOAD_FILE)
                print(f"  Using mail file: {os.path.basename(path)}")
            nerp_run(upload_file=config.NERP_UPLOAD_FILE)
        else:
            raise ValueError(f"Unsupported RPA tool: {job['tool']}")

        mark_rpa_finished(rpa_id, "ok")
        record_rpa_run(rpa_id, "ok", upload_file=upload_file)
        print(f"[RPA] {job['name']} complete.")
    except Exception as e:
        err = traceback.format_exc()[-500:]
        result["status"] = "error"
        result["message"] = str(e)
        mark_rpa_finished(rpa_id, "error", err)
        record_rpa_run(rpa_id, "error", message=err, upload_file=upload_file)
        print(f"[RPA] {job['name']} failed: {e}")
        raise

    return result


def trigger_for_mail_job(mail_job_id: str, upload_file: Optional[str] = None):
    """Run all enabled RPA tools linked to this mail job."""
    linked = list_for_mail_job(mail_job_id)
    if not linked:
        return []

    results = []
    for rpa in linked:
        try:
            results.append(run_rpa(rpa["rpa_id"], upload_file=upload_file))
        except Exception as e:
            results.append({
                "rpa_id": rpa["rpa_id"],
                "status": "error",
                "message": str(e),
            })
    return results
