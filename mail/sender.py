"""
Send email via Samsung Knox Mail API (multipart form: JSON `mail` + `attachments`).

Two entry points:
- send_email(...)      — send with explicit params
- send_for_rpa(rpa_id) — look up email job for an RPA, grab latest file, send
"""
import mimetypes
import os
from typing import Iterable, Optional

import requests

from mail.settings_db import get_knox_config, is_knox_configured


class SendError(RuntimeError):
    pass


def _is_html(text: str) -> bool:
    if not text:
        return False
    return "<" in text and ">" in text


def _split_emails(raw: str) -> list[str]:
    if not raw:
        return []
    import re
    return [e for e in re.split(r"[\s,;]+", raw.strip()) if e]


def send_email(
    to: str,
    subject: str,
    body: str,
    files: Optional[Iterable[str]] = None,
    cc: str = "",
) -> dict:
    """Send one email through the Knox Mail API. Returns response JSON.

    `to` / `cc` accept comma/semicolon/space-separated addresses.
    `files` is an iterable of local paths to attach.
    """
    if not is_knox_configured():
        raise SendError(
            "Knox Mail API not configured. Open Settings → Email API and fill in "
            "bearer token, system id, sender email, sender user id."
        )

    cfg = get_knox_config()
    to_list = _split_emails(to)
    cc_list = _split_emails(cc)
    if not to_list:
        raise SendError("At least one recipient address is required.")

    mail_body = {
        "subject": subject or "",
        "contents": body or "",
        "contentType": "HTML" if _is_html(body) else "TEXT",
        "docSecuType": "PERSONAL",
        "sender": {"emailAddress": cfg["knox_mail_sender_email"]},
        "recipients": (
            [{"emailAddress": e, "recipientType": "TO"} for e in to_list]
            + [{"emailAddress": e, "recipientType": "CC"} for e in cc_list]
        ),
    }

    import json
    data_parts = {"mail": json.dumps(mail_body)}
    file_parts = []
    open_handles = []
    try:
        for path in (files or []):
            if not path:
                continue
            if not os.path.isfile(path):
                raise SendError(f"Attachment not found: {path}")
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            handle = open(path, "rb")
            open_handles.append(handle)
            file_parts.append(("attachments", (os.path.basename(path), handle, ctype)))

        url = f"{cfg['knox_mail_api_base'].rstrip('/')}/mails/send"
        headers = {
            "Authorization": f"Bearer {cfg['knox_mail_bearer_token']}",
            "System-ID": cfg["knox_mail_system_id"],
        }
        params = {"userId": cfg["knox_mail_sender_user_id"]}

        resp = requests.post(
            url, params=params, headers=headers,
            data=data_parts, files=file_parts, timeout=60,
        )
    finally:
        for h in open_handles:
            try:
                h.close()
            except Exception:
                pass

    if not resp.ok:
        try:
            err = resp.json()
            msg = err.get("errorMessage") or err.get("errorCode") or resp.text
        except Exception:
            msg = resp.text
        raise SendError(f"Knox Mail API {resp.status_code}: {msg}")

    try:
        return resp.json()
    except Exception:
        return {"status_code": resp.status_code, "text": resp.text}


_ATTACH_EXTS = (".pdf", ".xlsx", ".xls", ".csv", ".xlsm", ".zip", ".docx", ".doc",
                ".png", ".jpg", ".jpeg", ".txt")


def _latest_files_in(directory: str, count: int) -> list[str]:
    if not directory or not os.path.isdir(directory):
        return []
    candidates = []
    for name in os.listdir(directory):
        if not name.lower().endswith(_ATTACH_EXTS):
            continue
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            candidates.append(path)
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[:max(1, count)]


def send_for_rpa(rpa_id: str, override_file: Optional[str] = None) -> dict:
    """Look up the email job for an RPA and send its latest downloaded file(s).

    override_file — if provided, use this exact path instead of scanning folders.
    """
    from mail.email_jobs_db import get_email_job_for_rpa

    job = get_email_job_for_rpa(rpa_id)
    if not job:
        raise SendError(f"No email job configured for RPA '{rpa_id}'.")
    if not job.get("enabled"):
        raise SendError(f"Email job for RPA '{rpa_id}' is disabled.")

    files: list[str] = []
    if override_file and os.path.isfile(override_file):
        files.append(override_file)
    else:
        watch_dir = job.get("attach_folder") or _rpa_download_folder(rpa_id)
        files = _latest_files_in(watch_dir, job.get("attach_count") or 1)
        if not files:
            raise SendError(f"No attachable files found in {watch_dir!r} for RPA '{rpa_id}'.")

    return send_email(
        to=job["to_emails"],
        subject=job.get("subject", "") or "",
        body=job.get("body", "") or "",
        files=files,
        cc=job.get("cc_emails", "") or "",
    )


def _rpa_download_folder(rpa_id: str) -> str:
    """Best-guess download folder for an RPA — for auto-attach lookup only."""
    try:
        from rpa.jobs_db import get_rpa_job
        job = get_rpa_job(rpa_id)
    except Exception:
        job = None
    if not job:
        return ""
    folder = (job.get("download_folder") or "").strip()
    if folder and os.path.isdir(folder):
        return folder
    mail_id = job.get("trigger_mail_job") or ""
    if mail_id:
        try:
            from mail.jobs_db import get_job, resolve_download_dir
            mj = get_job(mail_id)
            if mj:
                return mj.get("download_dir") or resolve_download_dir(mj)
        except Exception:
            pass
    return ""
