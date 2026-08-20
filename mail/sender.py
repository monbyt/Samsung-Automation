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
import time
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

    last_err = ""
    for attempt in range(1, 4):
        try:
            resp = requests.post(
                cfg["agent_api_url"], headers=headers,
                data=json.dumps(payload), timeout=120,
            )
        except requests.RequestException as e:
            last_err = str(e)
            print(f"[mail] Send attempt {attempt}/3 network error: {e}", flush=True)
            if attempt < 3:
                time.sleep(4 * attempt)
            continue
        if resp.ok:
            try:
                return resp.json()
            except Exception:
                return {"status_code": resp.status_code, "text": resp.text}
        try:
            err = resp.json()
            last_err = err.get("errorMessage") or err.get("errorCode") or resp.text
        except Exception:
            last_err = resp.text
        print(
            f"[mail] Send attempt {attempt}/3 failed: {resp.status_code} {last_err[:200]}",
            flush=True,
        )
        if resp.status_code not in (502, 503, 504) or attempt >= 3:
            raise SendError(f"Agent API {resp.status_code}: {last_err}")
        time.sleep(4 * attempt)
    raise SendError(f"Agent API failed after retries: {last_err}")


_ATTACH_EXTS = (".pdf", ".xlsx", ".xls", ".csv", ".xlsm", ".zip", ".docx", ".doc",
                ".png", ".jpg", ".jpeg", ".txt")


def _latest_files_in(directory: str, count: int) -> list[str]:
    """Newest attachable files in directory.

    count <= 0 means attach ALL matching files (for a dedicated PDF folder).
    """
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
    if count <= 0:
        return candidates
    return candidates[:count]


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
    keep_name_contains: Optional[Iterable[str]] = None,
) -> list[str]:
    """Delete attachable files from a folder (and any explicit paths).

    Returns the list of deleted file paths. Skips protected staging files
    (e.g. data/Book1.xlsx) and anything outside the given directory unless
    listed in ``also``.

    keep_name_contains — if set, skip files whose name contains any of these
    substrings (case-insensitive), e.g. ("layout",) to keep ZLSDF50270LAYOUT.XLSX.
    """
    deleted: list[str] = []
    protected = _protected_paths()
    seen: set[str] = set()
    keep_bits = [s.lower() for s in (keep_name_contains or ()) if s]

    def _safe_remove(path: str) -> None:
        if not path:
            return
        abs_path = os.path.abspath(path)
        if abs_path in seen or abs_path in protected:
            return
        if not os.path.isfile(abs_path):
            return
        name_l = os.path.basename(abs_path).lower()
        if any(bit in name_l for bit in keep_bits):
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


def remove_worker_dir(directory: str) -> bool:
    """Delete an isolated _worker_* folder after the run (files + directory)."""
    import shutil

    directory = _normalize_folder_path(directory or "")
    if not directory:
        return False
    base = os.path.basename(directory.rstrip("\\/"))
    if not base.startswith("_worker_"):
        return False
    try:
        shutil.rmtree(directory, ignore_errors=False)
        print(f"[mail] Removed worker folder: {directory}", flush=True)
        return True
    except OSError as e:
        print(f"[mail] Could not remove worker folder {directory}: {e}", flush=True)
        return False


def wipe_folder_attachables(directory: str, *, keep_name_contains: Optional[Iterable[str]] = None) -> list[str]:
    """Empty a folder of attachable files before a new RPA run starts."""
    return cleanup_folder_files(directory, keep_name_contains=keep_name_contains)


def _pdf_files_in(directory: str) -> list[str]:
    """All PDFs in directory, newest first — never Excel leftovers."""
    if not directory or not os.path.isdir(directory):
        return []
    out = []
    for name in os.listdir(directory):
        if not name.lower().endswith(".pdf"):
            continue
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            out.append(path)
    out.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return out


def _file_sha256(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _dedupe_files_by_content(paths: list[str]) -> list[str]:
    """Keep one file per identical PDF bytes (stops same invoice attached 2x)."""
    unique: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if not path or not os.path.isfile(path):
            continue
        try:
            digest = _file_sha256(path)
        except OSError:
            unique.append(path)
            continue
        if digest in seen:
            print(
                f"[mail] Skipping duplicate PDF (same bytes as an earlier attach): "
                f"{os.path.basename(path)}",
                flush=True,
            )
            continue
        seen.add(digest)
        unique.append(path)
    return unique


def _normalize_folder_path(raw: str) -> str:
    """Expand and normalize a configured folder path (file → parent dir)."""
    folder = (raw or "").strip().strip('"')
    if not folder:
        return ""
    folder = os.path.expandvars(os.path.expanduser(folder))
    folder = os.path.normpath(folder)
    if os.path.isfile(folder):
        folder = os.path.dirname(folder)
    return folder if folder and os.path.isdir(folder) else ""


def send_for_rpa(
    rpa_id: str,
    override_file: Optional[str] = None,
    *,
    cleanup: bool = True,
    upload_dir: Optional[str] = None,
    attach_dir: Optional[str] = None,
) -> dict:
    """Look up the email job for an RPA and send its latest downloaded file(s).

    override_file — if provided, use this exact path instead of scanning folders.
    cleanup — after a successful send, delete files from the attach/download
              subfolder so processed attachments do not pile up.
    upload_dir — explicit RPA upload folder to clean (RESULT xlsx). Prefer this
                 over DB lookup so we clean the same path the RPA just wrote to.
    attach_dir — explicit PDF/attach folder (parallel workers use an isolated subdir).
    """
    from mail.email_jobs_db import get_email_job_for_rpa

    job = get_email_job_for_rpa(rpa_id)
    if not job:
        raise SendError(f"No email job configured for RPA '{rpa_id}'.")
    if not job.get("enabled"):
        raise SendError(f"Email job for RPA '{rpa_id}' is disabled.")

    files: list[str] = []
    watch_dir = ""
    explicit_attach = (attach_dir or "").strip()
    if override_file and os.path.isfile(override_file):
        files.append(override_file)
        watch_dir = os.path.dirname(os.path.abspath(override_file))
    else:
        if explicit_attach:
            # Parallel workers pass an isolated _worker_* folder. Never fall back
            # to the Email Job's parent attach_folder — that piles up old PDFs
            # and is exactly how the wrong sales-order print gets emailed.
            watch_dir = _normalize_folder_path(explicit_attach)
            if not watch_dir:
                raise SendError(
                    f"Worker PDF folder missing or not a directory: {explicit_attach!r}"
                )
        else:
            watch_dir = (
                _normalize_folder_path(job.get("attach_folder") or "")
                or _rpa_download_folder(rpa_id)
            )
        # Prefer PDFs only (P/I prints). Excel RESULT leftovers must not ride along.
        pdfs = _pdf_files_in(watch_dir)
        raw_count = job.get("attach_count")
        try:
            attach_count = int(raw_count)
        except (TypeError, ValueError):
            attach_count = 0
        if pdfs:
            files = pdfs if attach_count <= 0 else pdfs[:attach_count]
        else:
            files = _latest_files_in(watch_dir, attach_count)
        available = pdfs or _latest_files_in(watch_dir, 0)
        if attach_count > 0 and len(available) > attach_count:
            print(
                f"[mail] attach_count={attach_count} but folder has {len(available)} files. "
                "Set 'How many latest files' to 0 to attach all.",
                flush=True,
            )
        if not files:
            raise SendError(f"No attachable files found in {watch_dir!r} for RPA '{rpa_id}'.")
        before = len(files)
        files = _dedupe_files_by_content(files)
        if len(files) < before:
            print(
                f"[mail] Removed {before - len(files)} duplicate PDF(s) before send",
                flush=True,
            )
        print(
            f"[mail] Attach folder {watch_dir!r} count={attach_count} "
            f"→ {len(files)} file(s): {[os.path.basename(p) for p in files]}",
            flush=True,
        )

    result = send_email(
        to=job["to_emails"],
        subject=job.get("subject", "") or "",
        body=job.get("body", "") or "",
        files=files,
        cc=job.get("cc_emails", "") or "",
    )

    if cleanup:
        deleted: list[str] = []
        resolved_upload = _normalize_folder_path(upload_dir or "") or _rpa_upload_folder(rpa_id)
        watch_norm = os.path.normpath(watch_dir) if watch_dir else ""
        upload_norm = os.path.normpath(resolved_upload) if resolved_upload else ""
        print(
            f"[mail] Cleanup attach_dir={watch_norm!r} upload_dir={upload_norm!r}",
            flush=True,
        )

        if watch_norm and upload_norm and watch_norm == upload_norm:
            # Same folder holds PDFs + Excel — wipe attachables but keep LAYOUT/templates.
            deleted += cleanup_folder_files(
                watch_norm,
                also=files,
                keep_name_contains=("layout", "template"),
            )
        else:
            deleted += cleanup_folder_files(watch_norm, also=files)
            if upload_norm:
                deleted += cleanup_folder_files(
                    upload_norm,
                    keep_name_contains=("layout", "template"),
                )
            else:
                print(
                    f"[mail] Upload folder not resolved for {rpa_id!r} — "
                    "RESULT Excel may remain. Set RPA Upload folder in the dashboard.",
                    flush=True,
                )

        result = dict(result) if isinstance(result, dict) else {"response": result}
        result["cleaned_files"] = [os.path.basename(p) for p in deleted]
        result["cleaned_dir"] = watch_norm
        if upload_norm:
            result["cleaned_upload_dir"] = upload_norm

        # Drop the whole isolated worker slot so the next run cannot see leftovers.
        if watch_norm and os.path.basename(watch_norm).startswith("_worker_"):
            remove_worker_dir(watch_norm)
        if upload_norm and os.path.basename(upload_norm).startswith("_worker_"):
            remove_worker_dir(upload_norm)

    return result


def _rpa_upload_folder(rpa_id: str) -> str:
    """RPA upload folder — where Create Sales Order .xlsx results are saved."""
    try:
        from rpa.jobs_db import get_rpa_job
        job = get_rpa_job(rpa_id)
    except Exception:
        job = None
    if not job:
        return ""
    folder = _normalize_folder_path(job.get("upload_folder") or "")
    if folder:
        return folder
    mail_id = job.get("trigger_mail_job") or ""
    if mail_id:
        try:
            from mail.jobs_db import get_job, resolve_download_dir
            mj = get_job(mail_id)
            if mj:
                d = mj.get("download_dir") or resolve_download_dir(mj)
                return _normalize_folder_path(d)
        except Exception:
            pass
    try:
        import config
        return _normalize_folder_path(getattr(config, "DOWNLOAD_DIR", "") or "")
    except Exception:
        pass
    return ""


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
