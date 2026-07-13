"""
Send email via the Samsung Agent API (agent.sec.samsung.net Langflow style).

POST {agent_api_url}
Headers: Content-Type: application/json, x-api-key: {agent_api_key}
Body:
{
  "input_type": "text",
  "output_type": "text",
  "input_value": "...",
  "component_inputs": {
    "<mail_component_id>": {
      "attachments": [...],
      "cc_target_emails": "...",
      "content": "...",
      "target_emails": "...",
      "title": "..."
    }
  }
}

Entry points:
- send_email(...)      — send with explicit params
- send_for_rpa(rpa_id) — look up email job for an RPA, grab latest file, send
"""
import base64
import json
import mimetypes
import os
from typing import Iterable, Optional

import requests

from mail.settings_db import get_agent_config, is_agent_configured


class SendError(RuntimeError):
    pass


def _normalize_emails(raw: str) -> str:
    """Return comma-separated, whitespace-trimmed address list."""
    if not raw:
        return ""
    import re
    parts = [e.strip() for e in re.split(r"[\s,;]+", raw.strip()) if e.strip()]
    return ", ".join(parts)


def _encode_attachment(path: str) -> dict:
    if not os.path.isfile(path):
        raise SendError(f"Attachment not found: {path}")
    with open(path, "rb") as f:
        data = f.read()
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return {
        "filename": os.path.basename(path),
        "contentType": ctype,
        "content": base64.b64encode(data).decode("ascii"),
    }


def send_email(
    to: str,
    subject: str,
    body: str,
    files: Optional[Iterable[str]] = None,
    cc: str = "",
) -> dict:
    """Send one email through the Samsung Agent mail API. Returns response JSON."""
    if not is_agent_configured():
        raise SendError(
            "Agent API not configured. Open Settings and fill in the API URL, "
            "API key, and mail component ID."
        )

    cfg = get_agent_config()
    to_norm = _normalize_emails(to)
    cc_norm = _normalize_emails(cc)
    if not to_norm:
        raise SendError("At least one recipient address is required.")

    attachments = [_encode_attachment(p) for p in (files or []) if p]

    payload = {
        "input_type": "text",
        "output_type": "text",
        "input_value": f"Mail to {to_norm}",
        "component_inputs": {
            cfg["agent_mail_component_id"]: {
                "attachments": attachments,
                "cc_target_emails": cc_norm,
                "content": body or "",
                "target_emails": to_norm,
                "title": subject or "",
            }
        },
    }

    headers = {
        "Content-Type": "application/json",
        "x-api-key": cfg["agent_api_key"],
    }

    resp = requests.post(
        cfg["agent_api_url"], headers=headers,
        data=json.dumps(payload), timeout=120,
    )

    if not resp.ok:
        try:
            err = resp.json()
            msg = err.get("errorMessage") or err.get("errorCode") or resp.text
        except Exception:
            msg = resp.text
        raise SendError(f"Agent API {resp.status_code}: {msg}")

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
