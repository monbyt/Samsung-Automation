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

_SPREADSHEET_EXT = (".xlsx", ".xls")


def _dirs_for_mail_job(mail_job_id: Optional[str]):
    from mail.jobs_db import get_job, list_jobs, resolve_download_dir

    dirs = []
    if mail_job_id:
        job = get_job(mail_job_id)
        if job:
            dirs.append(job.get("download_dir") or resolve_download_dir(job))
    else:
        for j in list_jobs():
            d = j.get("download_dir") or resolve_download_dir(j)
            if d:
                dirs.append(d)
    dirs.append(config.DOWNLOAD_DIR)
    # unique, preserve order
    seen = set()
    out = []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _find_latest_spreadsheet(mail_job_id: Optional[str] = None) -> Optional[str]:
    """Newest Excel on Desktop job folders (optionally scoped to one mail job)."""
    candidates = []
    for folder in _dirs_for_mail_job(mail_job_id):
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            if not name.lower().endswith(_SPREADSHEET_EXT):
                continue
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def _resolve_spreadsheet(path: str) -> str:
    """Turn a download path (.xlsx or .zip) into a readable Excel file."""
    if path.lower().endswith(_SPREADSHEET_EXT):
        return path
    if path.lower().endswith(".zip"):
        from parse_to_db import extract_zip_if_needed
        return extract_zip_if_needed(path, extract_zip=True)
    raise FileNotFoundError(f"Not an Excel or zip file: {path}")


def _prepare_upload_file(upload_file: Optional[str], rpa_job: dict) -> str:
    """Pick the Excel file for NERP — explicit path, linked mail folder, or data/Book1.xlsx."""
    mail_job_id = rpa_job.get("trigger_mail_job") or None

    if upload_file:
        if os.path.isfile(upload_file):
            try:
                return _resolve_spreadsheet(upload_file)
            except Exception:
                pass
        print(f"  Warning: upload path missing or unusable: {upload_file!r}")

    latest = _find_latest_spreadsheet(mail_job_id)
    if latest:
        print(f"  Using latest file from mail folder: {os.path.basename(latest)}")
        return latest

    if not mail_job_id:
        latest = _find_latest_spreadsheet()
        if latest:
            print(f"  Using latest file from any mail folder: {os.path.basename(latest)}")
            return latest

    if os.path.isfile(config.NERP_UPLOAD_FILE):
        return config.NERP_UPLOAD_FILE

    folders = ", ".join(_dirs_for_mail_job(mail_job_id))
    raise FileNotFoundError(
        f"No Excel file found for NERP. Run the linked mail job first "
        f"(checked: {folders}) or place a file at {config.NERP_UPLOAD_FILE}"
    )


def run_rpa(rpa_id: str, upload_file: Optional[str] = None) -> dict:
    """Run one RPA tool by id."""
    job = get_rpa_job(rpa_id)
    if not job:
        raise ValueError(f"Unknown RPA job: {rpa_id}")

    print(f"\n[RPA] Running {job['name']} ({rpa_id})...")
    result = {"rpa_id": rpa_id, "status": "ok", "message": ""}
    used_path = None

    try:
        if job["tool"] == "nerp":
            from nerp.rpa import run as nerp_run

            path = _prepare_upload_file(upload_file, job)
            used_path = path
            os.makedirs(os.path.dirname(config.NERP_UPLOAD_FILE), exist_ok=True)
            if os.path.abspath(path) != os.path.abspath(config.NERP_UPLOAD_FILE):
                shutil.copy2(path, config.NERP_UPLOAD_FILE)
                print(f"  Copied to {config.NERP_UPLOAD_FILE}")
            nerp_run(upload_file=config.NERP_UPLOAD_FILE)
        elif job["tool"] == "codegen":
            from rpa.codegen import run_recorded_script

            try:
                path = _prepare_upload_file(upload_file, job)
                used_path = path
            except FileNotFoundError:
                path = None
                print("  No mail/download file found — running script without auto-upload")
            run_recorded_script(rpa_id, upload_file=path)
        else:
            raise ValueError(f"Unsupported RPA tool: {job['tool']}")

        mark_rpa_finished(rpa_id, "ok")
        record_rpa_run(rpa_id, "ok", upload_file=used_path)
        print(f"[RPA] {job['name']} complete.")
    except Exception as e:
        err = traceback.format_exc()[-500:]
        result["status"] = "error"
        result["message"] = str(e)
        mark_rpa_finished(rpa_id, "error", err)
        record_rpa_run(rpa_id, "error", message=err, upload_file=used_path or upload_file)
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
