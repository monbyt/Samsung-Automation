"""
Send email via the Samsung Agent API (agent.sec.samsung.net Langflow style).

Two-step:
  1. Upload each attachment to {origin}/api/v2/files/ (multipart, field=file,
     x-api-key header). Response JSON contains a "path" reference.
  2. POST to {agent_api_url} with the mail component's `attachments` set to the
     list of returned paths.

Entry points:
- send_email(...)      — send with explicit params
- send_for_rpa(rpa_id) — look up email job for an RPA, grab latest file, send
"""
import json
import mimetypes
import os
from typing import Iterable, Optional, Set
from urllib.parse import urlparse

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


def _files_upload_url(agent_url: str) -> str:
    parsed = urlparse(agent_url)
    if not parsed.scheme or not parsed.netloc:
        raise SendError(f"Invalid Agent API URL: {agent_url!r}")
    return f"{parsed.scheme}://{parsed.netloc}/api/v2/files/"


def _upload_attachment(path: str, api_key: str, upload_url: str) -> str:
    """Upload one file to Langflow /api/v2/files/, return the server path ref."""
    if not os.path.isfile(path):
        raise SendError(f"Attachment not found: {path}")
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as fh:
        files = {"file": (os.path.basename(path), fh, ctype)}
        resp = requests.post(
            upload_url,
            headers={"x-api-key": api_key},
            files=files,
            timeout=120,
        )
    if not resp.ok:
        raise SendError(
            f"Attachment upload failed for {os.path.basename(path)}: "
            f"{resp.status_code} {resp.text[:300]}"
        )
    try:
        data = resp.json()
    except Exception as e:
        raise SendError(f"Attachment upload returned non-JSON: {e}: {resp.text[:300]}")
    ref = data.get("path") or data.get("file_path") or data.get("filePath")
    if not ref:
        raise SendError(f"Attachment upload response missing 'path': {data!r}")
    return ref


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

    upload_url = _files_upload_url(cfg["agent_api_url"])
    attachment_refs: list[str] = []
    for p in (files or []):
        if not p:
            continue
        ref = _upload_attachment(p, cfg["agent_api_key"], upload_url)
        attachment_refs.append(ref)

    mail_inputs = {
        "attachments": attachment_refs,
        "content": body or "",
        "target_emails": to_norm,
        "title": subject or "",
    }
    # Always send the CC field — Agent mail component expects it present.
    mail_inputs["cc_target_emails"] = cc_norm

    payload = {
        "input_type": "text",
        "output_type": "text",
        "input_value": (
            f"Mail to {to_norm}"
            + (f" (cc {cc_norm})" if cc_norm else "")
        ),
        "component_inputs": {
            cfg["agent_mail_component_id"]: mail_inputs,
        },
    }

    print(
        f"[mail] Sending to={to_norm!r}"
        + (f" cc={cc_norm!r}" if cc_norm else " cc=(none)")
        + f" subject={subject!r} attachments={len(attachment_refs)}",
        flush=True,
    )

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


def _protected_paths() -> Set[str]:
    """Paths that must never be deleted during post-send cleanup."""
    protected: Set[str] = set()
    try:
        import config
        if getattr(config, "NERP_UPLOAD_FILE", None):
            protected.add(os.path.abspath(config.NERP_UPLOAD_FILE))
    except Exception:
        pass
    return protected


def cleanup_folder_files(
    directory: str,
    *,
    also: Optional[Iterable[str]] = None,
) -> list[str]:
    """Delete attachable files from a folder (and any explicit paths).

    Returns the list of deleted file paths. Skips protected staging files
    (e.g. data/Book1.xlsx) and anything outside the given directory unless
    listed in ``also``.
    """
    deleted: list[str] = []
    protected = _protected_paths()
    seen: set[str] = set()

    def _safe_remove(path: str) -> None:
        if not path:
            return
        abs_path = os.path.abspath(path)
        if abs_path in seen or abs_path in protected:
            return
        if not os.path.isfile(abs_path):
            return
        try:
            os.remove(abs_path)
            deleted.append(abs_path)
            seen.add(abs_path)
            print(f"[mail] Cleaned up: {abs_path}", flush=True)
        except OSError as e:
            print(f"[mail] Cleanup skip {abs_path}: {e}", flush=True)

    for p in also or []:
        _safe_remove(p)

    if directory and os.path.isdir(directory):
        for name in os.listdir(directory):
            if not name.lower().endswith(_ATTACH_EXTS):
                continue
            _safe_remove(os.path.join(directory, name))

    return deleted


def send_for_rpa(
    rpa_id: str,
    override_file: Optional[str] = None,
    *,
    cleanup: bool = True,
) -> dict:
    """Look up the email job for an RPA and send its latest downloaded file(s).

    override_file — if provided, use this exact path instead of scanning folders.
    cleanup — after a successful send, delete files from the attach/download
              subfolder so processed attachments do not pile up.
    """
    from mail.email_jobs_db import get_email_job_for_rpa

    job = get_email_job_for_rpa(rpa_id)
    if not job:
        raise SendError(f"No email job configured for RPA '{rpa_id}'.")
    if not job.get("enabled"):
        raise SendError(f"Email job for RPA '{rpa_id}' is disabled.")

    files: list[str] = []
    watch_dir = ""
    if override_file and os.path.isfile(override_file):
        files.append(override_file)
        watch_dir = os.path.dirname(os.path.abspath(override_file))
    else:
        watch_dir = job.get("attach_folder") or _rpa_download_folder(rpa_id)
        files = _latest_files_in(watch_dir, job.get("attach_count") or 1)
        if not files:
            raise SendError(f"No attachable files found in {watch_dir!r} for RPA '{rpa_id}'.")

    result = send_email(
        to=job["to_emails"],
        subject=job.get("subject", "") or "",
        body=job.get("body", "") or "",
        files=files,
        cc=job.get("cc_emails", "") or "",
    )

    if cleanup:
        deleted = cleanup_folder_files(watch_dir, also=files)
        result = dict(result) if isinstance(result, dict) else {"response": result}
        result["cleaned_files"] = [os.path.basename(p) for p in deleted]
        result["cleaned_dir"] = watch_dir

    return result


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
