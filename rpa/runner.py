"""
Run RPA tools — manually or after a mail job finishes.
"""
import json
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
    """Most recently created/modified spreadsheet in one folder.

    Skip SAP *RESULT* workbooks — those are outputs from a previous run and
    must not be re-uploaded (sales org will not match the screen).
    """
    if not os.path.isdir(directory):
        return None
    candidates = []
    for name in os.listdir(directory):
        if not name.lower().endswith(_UPLOAD_EXT):
            continue
        if "result" in name.lower():
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
    """Pick the Excel to upload.

    A file passed from the mail job always wins. Otherwise a previous run's
    RESULT in the upload folder is newer and SAP errors with
    "upload sales org differs from input sales org".
    """
    from rpa.debug_log import debug_log

    mail_job_id = rpa_job.get("trigger_mail_job") or None
    configured = (rpa_job.get("upload_folder") or "").strip()
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

    # 1. Mail-triggered spreadsheet (each unread email's attachment)
    if upload_file:
        if os.path.isfile(upload_file):
            try:
                resolved = _resolve_spreadsheet(upload_file)
                _log(f"Using file from mail trigger: {resolved}")
                debug_log(
                    "H1",
                    "runner.py:_prepare_upload_file",
                    "resolved mail trigger file",
                    {"path": resolved, "exists": os.path.isfile(resolved)},
                )
                return resolved
            except Exception as e:
                _log(f"Mail trigger file unusable ({e}), falling back")
        else:
            print(f"  Warning: upload path missing or unusable: {upload_file!r}")

    # 2. Upload folder on RPA edit page (manual runs / no mail file)
    if configured:
        if os.path.isfile(configured):
            _log(f"Using configured upload file: {configured}")
            resolved = _resolve_spreadsheet(configured)
            debug_log("H1", "runner.py:_prepare_upload_file", "resolved configured file", {"path": resolved, "exists": os.path.isfile(resolved)})
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
                debug_log("H1", "runner.py:_prepare_upload_file", "resolved latest in folder", {
                    "path": latest, "folder": configured,
                    "size": os.path.getsize(latest),
                    "ctime": ctime, "mtime": mtime,
                })
                return latest
            raise FileNotFoundError(f"No spreadsheet in upload folder: {configured}")
        raise FileNotFoundError(f"Upload path not found: {configured}")

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


def run_rpa(
    rpa_id: str,
    upload_file: Optional[str] = None,
    _visited: Optional[set] = None,
    send_email: bool = True,
    upload_dir: Optional[str] = None,
    download_dir: Optional[str] = None,
    run_label: Optional[str] = None,
) -> dict:
    """Run one RPA tool by id. Chains to next_rpa on success.

    send_email — if False, skip the post-RPA mail send.
    upload_dir / download_dir — optional isolated folders for parallel workers.
    run_label — optional Live-execution card title.
    """
    from pipeline_progress import (
        PipelineCancelled, begin_step, check_cancelled, current_run_id,
        finish_run, finish_step, start_run,
    )

    if _visited is None:
        _visited = set()
    if rpa_id in _visited:
        _log(f"Cycle detected — skipping already-visited job: {rpa_id}")
        return {"rpa_id": rpa_id, "status": "skipped", "message": "cycle detected"}
    _visited.add(rpa_id)

    job = get_rpa_job(rpa_id)
    if not job:
        raise ValueError(f"Unknown RPA job: {rpa_id}")

    owns_run = current_run_id() is None
    if owns_run:
        start_run(
            "rpa",
            rpa_id,
            run_label or f"RPA · {job.get('name') or rpa_id}",
            [
                ("prepare", "Prepare upload file"),
                ("rpa", f"Run {job.get('name') or rpa_id}"),
                ("email", "Send email"),
                ("cleanup", "Clean up files"),
            ],
        )

    print(f"\n[RPA] Running {job['name']} ({rpa_id})...")
    _log(f"Tool type: {job['tool']}")
    if job.get("trigger_mail_job"):
        _log(f"Linked mail job: {job['trigger_mail_job']}")
    if job.get("start_url"):
        _log(f"Start URL: {job['start_url']}")
    result = {"rpa_id": rpa_id, "status": "ok", "message": ""}
    used_path = None
    resolved_upload_dir = ""
    resolved_download_dir = ""

    try:
        check_cancelled()
        begin_step("prepare" if owns_run else "rpa", f"Preparing {rpa_id}")
        if job["tool"] == "nerp":
            from nerp.rpa import run as nerp_run

            path = _prepare_upload_file(upload_file, job)
            used_path = path
            if owns_run:
                finish_step("prepare", "ok", os.path.basename(path))
                begin_step("rpa", f"Running {job['name']}")
            check_cancelled()
            os.makedirs(os.path.dirname(config.NERP_UPLOAD_FILE), exist_ok=True)
            if os.path.abspath(path) != os.path.abspath(config.NERP_UPLOAD_FILE):
                shutil.copy2(path, config.NERP_UPLOAD_FILE)
                print(f"  Copied to {config.NERP_UPLOAD_FILE}")
            nerp_run(upload_file=config.NERP_UPLOAD_FILE)
        elif job["tool"] == "codegen":
            from rpa.codegen import run_recorded_script

            base_upload, base_download = _resolve_rpa_folders(job)
            resolved_upload_dir = _normalize_folder_path(upload_dir) or base_upload
            resolved_download_dir = _normalize_folder_path(download_dir) or base_download
            os.makedirs(resolved_upload_dir, exist_ok=True)
            os.makedirs(resolved_download_dir, exist_ok=True)
            _log(f"Upload folder: {resolved_upload_dir}")
            _log(f"Download folder: {resolved_download_dir}")
            try:
                path = _prepare_upload_file(upload_file, job)
                used_path = path
                _log(f"Resolved upload file: {path}")
                if owns_run:
                    finish_step("prepare", "ok", os.path.basename(path))
            except FileNotFoundError as e:
                path = None
                _log(f"No upload file: {e}")
                if owns_run:
                    finish_step("prepare", "ok", "No upload file (script may not need one)")
            if owns_run:
                begin_step("rpa", f"Running {job['name']}")
            check_cancelled()
            run_recorded_script(
                rpa_id,
                upload_file=path,
                upload_dir=resolved_upload_dir,
                download_dir=resolved_download_dir,
            )
        else:
            raise ValueError(f"Unsupported RPA tool: {job['tool']}")

        check_cancelled()
        mark_rpa_finished(rpa_id, "ok")
        record_rpa_run(rpa_id, "ok", upload_file=used_path)
        print(f"[RPA] {job['name']} complete.")
        finish_step("rpa", "ok", f"{job['name']} finished")

        if send_email:
            _maybe_send_email(
                rpa_id,
                upload_dir=resolved_upload_dir,
                attach_dir=resolved_download_dir,
            )
        else:
            _log("Deferring email send until remaining download files are processed.")

        # Chain to next step if configured
        next_id = job.get("next_rpa") or ""
        if next_id:
            check_cancelled()
            _log(f"Chaining to next step: {next_id}")
            try:
                run_rpa(next_id, upload_file=used_path, _visited=_visited)
            except PipelineCancelled:
                raise
            except Exception as chain_err:
                _log(f"Chained step {next_id!r} failed: {chain_err}")
                result["chain_error"] = str(chain_err)

        if owns_run:
            finish_run("ok" if not result.get("chain_error") else "error",
                       result.get("chain_error") or "")

    except PipelineCancelled as e:
        result["status"] = "cancelled"
        result["message"] = str(e)
        mark_rpa_finished(rpa_id, "error", str(e))
        record_rpa_run(rpa_id, "error", message=str(e), upload_file=used_path or upload_file)
        finish_step("rpa", "error", str(e))
        if owns_run:
            finish_run("cancelled", str(e))
        print(f"[RPA] {job['name']} cancelled: {e}")
        raise

    except Exception as e:
        err = traceback.format_exc()[-500:]
        result["status"] = "error"
        result["message"] = str(e)
        mark_rpa_finished(rpa_id, "error", err)
        record_rpa_run(rpa_id, "error", message=err, upload_file=used_path or upload_file)
        print(f"[RPA] {job['name']} failed: {e}")
        finish_step("rpa", "error", str(e))
        if owns_run:
            finish_run("error", str(e))
        raise

    return result


def _maybe_send_email(rpa_id: str, upload_dir: str = "", attach_dir: str = "") -> None:
    """If an enabled email job is configured for this RPA, send it."""
    from pipeline_progress import begin_step, check_cancelled, finish_step, skip_step

    try:
        from mail.email_jobs_db import get_email_job_for_rpa, mark_send_finished
    except Exception as e:
        _log(f"Email module unavailable, skipping send: {e}")
        skip_step("email", f"Unavailable: {e}")
        skip_step("cleanup", "No email send")
        return

    job = get_email_job_for_rpa(rpa_id)
    if not job:
        skip_step("email", "No email job linked")
        skip_step("cleanup", "No email send")
        return
    if not job.get("enabled"):
        _log(f"Email job for {rpa_id} is disabled, skipping.")
        skip_step("email", "Email job disabled")
        skip_step("cleanup", "No email send")
        return

    check_cancelled()
    _log(f"Sending email for {rpa_id} to {job.get('to_emails')}"
         + (f" cc={job.get('cc_emails')}" if job.get("cc_emails") else ""))
    cc_note = f" · cc {job.get('cc_emails')}" if job.get("cc_emails") else ""
    begin_step("email", f"To {job.get('to_emails')}{cc_note}")
    try:
        from mail.sender import send_for_rpa
        result = send_for_rpa(
            rpa_id,
            upload_dir=upload_dir or None,
            attach_dir=attach_dir or None,
        )
        mark_send_finished(rpa_id, "ok")
        cleaned = (result or {}).get("cleaned_files") or []
        finish_step("email", "ok", f"Sent to {job.get('to_emails')}{cc_note}")
        begin_step("cleanup", result.get("cleaned_upload_dir") or result.get("cleaned_dir") or "")
        finish_step(
            "cleanup", "ok",
            f"Removed {len(cleaned)} file(s)" if cleaned else "Folder already empty",
        )
        _log(f"Email sent for {rpa_id}. Cleaned: {cleaned}")
    except Exception as e:
        from pipeline_progress import PipelineCancelled
        if isinstance(e, PipelineCancelled):
            raise
        err = traceback.format_exc()[-500:]
        _log(f"Email send failed for {rpa_id}: {e}")
        finish_step("email", "error", str(e))
        skip_step("cleanup", "Skipped — send failed")
        try:
            mark_send_finished(rpa_id, "error", err)
        except Exception:
            pass


def _worker_slot_dirs(base_upload: str, base_download: str, index: int, token: str) -> Tuple[str, str]:
    """Isolated upload/PDF folders so parallel workers cannot clobber each other."""
    name = f"_worker_{index:02d}_{token}"
    upload = os.path.join(base_upload, name)
    download = os.path.join(base_download, name)
    os.makedirs(upload, exist_ok=True)
    os.makedirs(download, exist_ok=True)
    return upload, download


def _parallel_worker(payload: dict) -> dict:
    """Child-process entry: one inbox Excel → RPA → email (isolated folders)."""
    os.environ.setdefault("NO_PROXY", "*")
    os.environ.setdefault("no_proxy", "*")
    # Detach from parent mail-run context so this worker owns its Live card
    # (needed when we run in-process for a single file).
    from pipeline_progress import set_current_run
    set_current_run(None)

    rpa_id = payload["rpa_id"]
    path = payload.get("upload_file")
    label = payload.get("label") or f"RPA · {rpa_id}"
    try:
        return run_rpa(
            rpa_id,
            upload_file=path,
            send_email=True,
            upload_dir=payload.get("upload_dir"),
            download_dir=payload.get("download_dir"),
            run_label=label,
        )
    except Exception as e:
        return {
            "rpa_id": rpa_id,
            "status": "error",
            "message": str(e),
            "upload_file": path,
        }


def _chrome_profile_dir(index: int, token: str) -> str:
    root = os.path.join(config.BASE_DIR, "chrome-profile-rpa")
    path = os.path.join(root, f"w{index:02d}_{token}")
    os.makedirs(path, exist_ok=True)
    return path


def _read_worker_result(result_path: str, payload: dict, exit_code: int) -> dict:
    if result_path and os.path.isfile(result_path):
        try:
            with open(result_path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {
        "rpa_id": payload.get("rpa_id"),
        "status": "error",
        "message": f"Worker exited {exit_code} with no result file",
        "upload_file": payload.get("upload_file"),
    }


def _start_worker_process(payload: dict):
    import subprocess
    import sys

    payload = dict(payload)
    upload_dir = payload.get("upload_dir") or config.BASE_DIR
    os.makedirs(upload_dir, exist_ok=True)
    result_path = os.path.join(upload_dir, "_worker_result.json")
    payload_path = os.path.join(upload_dir, "_worker_payload.json")
    payload["result_path"] = result_path
    with open(payload_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    env = os.environ.copy()
    env.setdefault("NO_PROXY", "*")
    env.setdefault("no_proxy", "*")
    env["PYTHONPATH"] = config.BASE_DIR + os.pathsep + env.get("PYTHONPATH", "")
    if payload.get("chrome_profile"):
        env["RPA_CHROME_PROFILE"] = payload["chrome_profile"]
    if payload.get("chrome_port"):
        env["RPA_CHROME_DEBUG_PORT"] = str(payload["chrome_port"])
    worker_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.py")
    _log(f"Opening Chrome · {payload.get('label')}")
    popen_kwargs = {
        "cwd": config.BASE_DIR,
        "env": env,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NEW_CONSOLE
        )
    proc = subprocess.Popen(
        [sys.executable, worker_py, "--payload-file", payload_path],
        **popen_kwargs,
    )
    _log(
        f"Spawned worker PID {proc.pid} · chrome_port={payload.get('chrome_port')} · "
        f"profile={os.path.basename(payload.get('chrome_profile') or '-')}"
    )
    return proc, payload, result_path


def trigger_for_mail_job(
    mail_job_id: str,
    upload_file: Optional[str] = None,
    upload_files: Optional[list] = None,
):
    """Run linked RPA tools for each downloaded mail Excel.

    Multiple inbox Excels run as separate Chrome processes (capped by
    config.RPA_PARALLEL_WORKERS, default 4). Each worker has its own Chrome
    profile + upload/PDF folders and Live card.

    One Excel with several SOs still becomes multiple PDFs on that worker's
    outgoing email.
    """
    import time

    from pipeline_progress import PipelineCancelled, check_cancelled, request_stop

    linked = list_for_mail_job(mail_job_id)
    if not linked:
        return []

    files = [p for p in (upload_files or []) if p]
    if not files and upload_file:
        files = [upload_file]
    if not files:
        files = [None]

    workers = max(1, int(getattr(config, "RPA_PARALLEL_WORKERS", 4) or 4))
    results = []

    for rpa in linked:
        rpa_id = rpa["rpa_id"]
        base_upload, base_download = _resolve_rpa_folders(rpa)
        token = datetime.now().strftime("%H%M%S")
        payloads = []
        for i, path in enumerate(files):
            chrome_profile = _chrome_profile_dir(i + 1, token)
            if path:
                w_upload, w_download = _worker_slot_dirs(base_upload, base_download, i + 1, token)
                label = (
                    f"RPA · {rpa.get('name') or rpa_id} · "
                    f"mail {i + 1}/{len(files)} · {os.path.basename(path)}"
                )
                _log(f"Queue mail file {i + 1}/{len(files)}: {os.path.basename(path)}")
            else:
                w_upload, w_download = base_upload, base_download
                label = f"RPA · {rpa.get('name') or rpa_id}"
            payloads.append({
                "rpa_id": rpa_id,
                "upload_file": path,
                "upload_dir": w_upload,
                "download_dir": w_download,
                "chrome_profile": chrome_profile,
                "chrome_port": 9330 + i,
                "label": label,
            })

        _log(
            f"===== RPA BATCH: {len(payloads)} Excel file(s) → "
            f"up to {workers} Chrome windows ====="
        )
        if len(payloads) == 1:
            _log(
                "Only 1 file was downloaded, so only 1 SAP Chrome will open. "
                "Need 2+ unread mail Excels for parallel windows."
            )

        if workers <= 1:
            for payload in payloads:
                check_cancelled()
                if payload.get("chrome_profile"):
                    os.environ["RPA_CHROME_PROFILE"] = payload["chrome_profile"]
                if payload.get("chrome_port"):
                    os.environ["RPA_CHROME_DEBUG_PORT"] = str(payload["chrome_port"])
                try:
                    results.append(_parallel_worker(payload))
                except PipelineCancelled as e:
                    results.append({
                        "rpa_id": rpa_id,
                        "status": "error",
                        "message": str(e),
                    })
                    raise
                except Exception as e:
                    results.append({
                        "rpa_id": rpa_id,
                        "status": "error",
                        "message": str(e),
                    })
            continue
        queue = list(payloads)
        active = []
        last_heartbeat = 0.0
        try:
            while queue or active:
                check_cancelled()
                while queue and len(active) < workers:
                    proc, payload, result_path = _start_worker_process(queue.pop(0))
                    active.append((proc, payload, result_path))
                    _log(f"Live workers now: {len(active)} / cap {workers}")
                still = []
                for proc, payload, result_path in active:
                    rc = proc.poll()
                    if rc is None:
                        still.append((proc, payload, result_path))
                        continue
                    results.append(_read_worker_result(result_path, payload, rc))
                    _log(
                        f"Worker PID {proc.pid} finished ({rc}) · "
                        f"{os.path.basename(payload.get('upload_file') or '')}"
                    )
                active = still
                now = time.time()
                if active and now - last_heartbeat >= 8:
                    last_heartbeat = now
                    _log(
                        "Still running in parallel: "
                        + ", ".join(
                            f"PID {p.pid}/{os.path.basename(pl.get('upload_file') or '')}"
                            for p, pl, _r in active
                        )
                    )
                if queue or active:
                    time.sleep(0.4)
        except PipelineCancelled:
            _log("Stop requested — closing remaining Chrome workers")
            request_stop("Stopped by user")
            for proc, payload, _result_path in active:
                try:
                    proc.terminate()
                except Exception:
                    pass
            raise
    return results
