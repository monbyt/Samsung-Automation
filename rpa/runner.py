"""
Run RPA tools — manually or after a mail job finishes.
"""
import os
import shutil
import traceback
from datetime import datetime
from typing import Optional, Tuple

import config
from db import record_rpa_run
from rpa.jobs_db import get_rpa_job, list_for_mail_job, mark_rpa_finished, get_next_rpa

_UPLOAD_EXT = (".xlsx", ".xls", ".xlsm", ".csv")


def _log(msg: str) -> None:
    print(f"[RPA {datetime.now():%H:%M:%S}] {msg}", flush=True)


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


def _file_recency_key(path: str) -> float:
    """Return the most recent timestamp for a file.

    On Windows, getctime is the actual creation time (when the file landed on disk),
    which is more reliable than mtime for downloaded/copied files that can preserve
    the original modification date from the source server or email attachment.
    """
    try:
        return max(os.path.getmtime(path), os.path.getctime(path))
    except OSError:
        return 0.0


def _find_latest_in_dir(directory: str) -> Optional[str]:
    """Most recently created/modified spreadsheet in one folder."""
    if not os.path.isdir(directory):
        return None
    candidates = []
    for name in os.listdir(directory):
        if not name.lower().endswith(_UPLOAD_EXT):
            continue
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            candidates.append(path)
    if not candidates:
        return None
    best = max(candidates, key=_file_recency_key)
    _log(
        f"Latest file in {directory!r}: {os.path.basename(best)}"
        f" (mtime={os.path.getmtime(best):.0f}"
        f" ctime={os.path.getctime(best):.0f})"
    )
    return best


def _find_latest_spreadsheet(mail_job_id: Optional[str] = None) -> Optional[str]:
    """Most recently created/modified spreadsheet across job folders."""
    candidates = []
    for folder in _dirs_for_mail_job(mail_job_id):
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            if not name.lower().endswith(_UPLOAD_EXT):
                continue
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=_file_recency_key)


def _resolve_spreadsheet(path: str) -> str:
    """Turn a download path (.xlsx, .xls, .zip) into a readable file for upload."""
    if path.lower().endswith(_UPLOAD_EXT):
        return path
    if path.lower().endswith(".zip"):
        from parse_to_db import extract_zip_if_needed
        return extract_zip_if_needed(path, extract_zip=True)
    raise FileNotFoundError(f"Not an Excel or zip file: {path}")


def _normalize_folder_path(path: str) -> str:
    """Folder path for Windows dialogs — if user pasted a file path, use its parent."""
    path = (path or "").strip()
    if not path:
        return ""
    path = os.path.normpath(path)
    if os.path.isfile(path):
        return os.path.dirname(path)
    return path


def _resolve_rpa_folders(rpa_job: dict) -> Tuple[str, str]:
    """Upload/download Windows folders for an RPA job."""
    from mail.jobs_db import get_job, resolve_download_dir

    upload = _normalize_folder_path(rpa_job.get("upload_folder") or "")
    download = _normalize_folder_path(rpa_job.get("download_folder") or "")
    mail_id = rpa_job.get("trigger_mail_job") or ""

    if mail_id:
        mail_job = get_job(mail_id)
        if mail_job:
            mail_dir = mail_job.get("download_dir") or resolve_download_dir(mail_job)
            if not upload:
                upload = mail_dir
            if not download:
                download = mail_dir

    if not upload:
        upload = config.DOWNLOAD_DIR
    if not download:
        download = upload
    return os.path.normpath(upload), os.path.normpath(download)


def _prepare_upload_file(upload_file: Optional[str], rpa_job: dict) -> str:
    """Pick upload file — configured upload folder/path beats linked mail download."""
    from rpa.debug_log import debug_log

    mail_job_id = rpa_job.get("trigger_mail_job") or None
    configured = (rpa_job.get("upload_folder") or "").strip()
    # region agent log
    debug_log(
        "H1",
        "runner.py:_prepare_upload_file:entry",
        "upload resolution start",
        {
            "rpa_id": rpa_job.get("rpa_id"),
            "configured_upload_folder": configured,
            "trigger_upload_file": upload_file,
        },
    )
    # endregion

    # 1. Upload folder on RPA edit page wins (folder = newest file there, or full file path)
    if configured:
        if os.path.isfile(configured):
            _log(f"Using configured upload file: {configured}")
            resolved = _resolve_spreadsheet(configured)
            # region agent log
            debug_log("H1", "runner.py:_prepare_upload_file", "resolved configured file", {"path": resolved, "exists": os.path.isfile(resolved)})
            # endregion
            return resolved
        if os.path.isdir(configured):
            latest = _find_latest_in_dir(configured)
            if latest:
                import datetime as _dt
                ctime = os.path.getctime(latest)
                mtime = os.path.getmtime(latest)
                _log(
                    f"Selected file: {os.path.basename(latest)}"
                    f" | size={os.path.getsize(latest)} bytes"
                    f" | created={_dt.datetime.fromtimestamp(ctime):%Y-%m-%d %H:%M:%S}"
                    f" | modified={_dt.datetime.fromtimestamp(mtime):%Y-%m-%d %H:%M:%S}"
                )
                # region agent log
                debug_log("H1", "runner.py:_prepare_upload_file", "resolved latest in folder", {
                    "path": latest, "folder": configured,
                    "size": os.path.getsize(latest),
                    "ctime": ctime, "mtime": mtime,
                })
                # endregion
                return latest
            raise FileNotFoundError(f"No spreadsheet in upload folder: {configured}")
        raise FileNotFoundError(f"Upload path not found: {configured}")

    # 2. File passed when mail job triggered this RPA
    if upload_file:
        if os.path.isfile(upload_file):
            try:
                _log(f"Using file from mail trigger: {upload_file}")
                return _resolve_spreadsheet(upload_file)
            except Exception:
                pass
        print(f"  Warning: upload path missing or unusable: {upload_file!r}")

    # 3. Latest from linked mail job folder
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
    # region agent log
    debug_log(
        "H1",
        "runner.py:_prepare_upload_file:fail",
        "no upload file resolved",
        {"mail_job_id": mail_job_id, "checked_folders": folders},
    )
    # endregion
    raise FileNotFoundError(
        f"No Excel file found for NERP. Run the linked mail job first "
        f"(checked: {folders}) or place a file at {config.NERP_UPLOAD_FILE}"
    )


def run_rpa(rpa_id: str, upload_file: Optional[str] = None, _visited: Optional[set] = None) -> dict:
    """Run one RPA tool by id. Chains to next_rpa on success."""
    if _visited is None:
        _visited = set()
    if rpa_id in _visited:
        _log(f"Cycle detected — skipping already-visited job: {rpa_id}")
        return {"rpa_id": rpa_id, "status": "skipped", "message": "cycle detected"}
    _visited.add(rpa_id)

    job = get_rpa_job(rpa_id)
    if not job:
        raise ValueError(f"Unknown RPA job: {rpa_id}")

    print(f"\n[RPA] Running {job['name']} ({rpa_id})...")
    _log(f"Tool type: {job['tool']}")
    if job.get("trigger_mail_job"):
        _log(f"Linked mail job: {job['trigger_mail_job']}")
    if job.get("start_url"):
        _log(f"Start URL: {job['start_url']}")
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

            upload_dir, download_dir = _resolve_rpa_folders(job)
            _log(f"Upload folder: {upload_dir}")
            _log(f"Download folder: {download_dir}")
            try:
                path = _prepare_upload_file(upload_file, job)
                used_path = path
                _log(f"Resolved upload file: {path}")
            except FileNotFoundError as e:
                path = None
                _log(f"No upload file: {e}")
            run_recorded_script(
                rpa_id,
                upload_file=path,
                upload_dir=upload_dir,
                download_dir=download_dir,
            )
        else:
            raise ValueError(f"Unsupported RPA tool: {job['tool']}")

        mark_rpa_finished(rpa_id, "ok")
        record_rpa_run(rpa_id, "ok", upload_file=used_path)
        print(f"[RPA] {job['name']} complete.")

        # Chain to next step if configured
        next_id = job.get("next_rpa") or ""
        if next_id:
            _log(f"Chaining to next step: {next_id}")
            try:
                run_rpa(next_id, upload_file=used_path, _visited=_visited)
            except Exception as chain_err:
                _log(f"Chained step {next_id!r} failed: {chain_err}")
                result["chain_error"] = str(chain_err)

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
