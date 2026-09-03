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
import re
import time
from datetime import datetime
from html import escape as html_escape
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


_TEMPLATE_TOKEN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _basename(path: str) -> str:
    return os.path.basename(path or "").strip()


def _stem(path: str) -> str:
    return os.path.splitext(_basename(path))[0]


def _looks_like_html(text: str) -> bool:
    return bool(text) and "<" in text and ">" in text


def render_email_template(template: str, values: dict, *, html: bool = False) -> str:
    """Replace {tokens} in subject/body. Unknown tokens are left unchanged."""
    if not template:
        return ""

    def repl(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)
        raw = values[key]
        val = "" if raw is None else str(raw)
        if html:
            val = html_escape(val).replace("\n", "<br>\n")
        return val

    return _TEMPLATE_TOKEN.sub(repl, template)


def _last_rpa_upload_name(rpa_id: str) -> str:
    if not rpa_id:
        return ""
    try:
        from sqlalchemy import select

        from db import engine, init_db, rpa_runs

        init_db()
        with engine.connect() as conn:
            row = conn.execute(
                select(rpa_runs.c.upload_file)
                .where(rpa_runs.c.rpa_id == rpa_id)
                .where(rpa_runs.c.upload_file.isnot(None))
                .where(rpa_runs.c.upload_file != "")
                .order_by(rpa_runs.c.id.desc())
                .limit(1)
            ).first()
        return (row[0] if row else "") or ""
    except Exception:
        return ""


def _lookup_mail_subject(source_file: str, filter_id: str = "") -> str:
    """Inbound mail subject from the last ingest of this Excel (or this mail job)."""
    try:
        from sqlalchemy import select

        from db import engine, ingestion_log, init_db

        init_db()
        name = _basename(source_file)
        if not name and not filter_id:
            return ""
        with engine.connect() as conn:
            stmt = select(ingestion_log.c.mail_subject).where(
                ingestion_log.c.status == "success"
            )
            if name:
                stmt = stmt.where(ingestion_log.c.source_file == name)
            else:
                stmt = stmt.where(ingestion_log.c.filter_id == filter_id)
            row = conn.execute(
                stmt.order_by(ingestion_log.c.id.desc()).limit(1)
            ).first()
        return ((row[0] if row else "") or "").strip()
    except Exception:
        return ""


def _rpa_context(rpa_id: str) -> dict:
    ctx = {"rpa_id": rpa_id or "", "rpa_name": "", "mail_job": "", "mail_job_name": ""}
    if not rpa_id:
        return ctx
    try:
        from rpa.jobs_db import get_rpa_job

        job = get_rpa_job(rpa_id) or {}
    except Exception:
        job = {}
    ctx["rpa_name"] = (job.get("name") or "") if job else ""
    mail_id = (job.get("trigger_mail_job") or "") if job else ""
    ctx["mail_job"] = mail_id
    if mail_id:
        try:
            from mail.jobs_db import get_job

            mj = get_job(mail_id) or {}
            ctx["mail_job_name"] = mj.get("name") or mail_id
        except Exception:
            ctx["mail_job_name"] = mail_id
    return ctx


def build_email_template_values(
    rpa_id: str,
    attach_files: Iterable[str],
    source_upload_file: str = "",
) -> dict:
    """Values for {placeholders} in the email-job subject and body."""
    names = [_basename(p) for p in (attach_files or []) if _basename(p)]
    original = _basename(source_upload_file) or _last_rpa_upload_name(rpa_id)
    now = datetime.now()
    ctx = _rpa_context(rpa_id)
    mail_subject = _lookup_mail_subject(original, ctx.get("mail_job") or "")
    first = names[0] if names else ""
    files_joined = ", ".join(names)
    file_list = "\n".join(names)
    return {
        "file": first,
        "filename": first,
        "file_name": first,
        "file_stem": _stem(first),
        "files": files_joined,
        "file_names": files_joined,
        "file_list": file_list,
        "file_count": str(len(names)),
        "original_file": original,
        "original_stem": _stem(original),
        "source_file": original,
        "upload_file": original,
        "rpa_id": ctx["rpa_id"],
        "rpa_name": ctx["rpa_name"],
        "mail_job": ctx["mail_job"],
        "mail_job_name": ctx["mail_job_name"],
        "mail_subject": mail_subject,
        "mail_from": "",
        "mail_cc": "",
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "datetime": now.strftime("%Y-%m-%d %H:%M"),
    }


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
    source_upload_file: Optional[str] = None,
) -> dict:
    """Look up the email job for an RPA and send its latest downloaded file(s).

    override_file — if provided, use this exact path instead of scanning folders.
    cleanup — after a successful send, delete files from the attach/download
              subfolder so processed attachments do not pile up.
    upload_dir — explicit RPA upload folder to clean (RESULT xlsx). Prefer this
                 over DB lookup so we clean the same path the RPA just wrote to.
    attach_dir — explicit PDF/attach folder (parallel workers use an isolated subdir).
    source_upload_file — original mail Excel in the parent upload folder (not the
                 _worker_ copy). Deleted after a successful send so order-creation
                 does not keep growing.
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
        # P/I emails are PDF-only. Never fall back to .xlsx (RESULT / mail Excel)
        # when PDF download failed — that is how "why is it sending Excel?" happens.
        pdfs = _pdf_files_in(watch_dir)
        raw_count = job.get("attach_count")
        try:
            attach_count = int(raw_count)
        except (TypeError, ValueError):
            attach_count = 0
        if not pdfs:
            raise SendError(
                f"No PDF files in {watch_dir!r} for RPA '{rpa_id}'. "
                "Not attaching Excel. Re-run after P/I download succeeds."
            )
        files = pdfs if attach_count <= 0 else pdfs[:attach_count]
        if attach_count > 0 and len(pdfs) > attach_count:
            print(
                f"[mail] attach_count={attach_count} but folder has {len(pdfs)} PDFs. "
                "Set 'How many latest files' to 0 to attach all.",
                flush=True,
            )
        before = len(files)
        files = _dedupe_files_by_content(files)
        if len(files) < before:
            print(
                f"[mail] Removed {before - len(files)} duplicate PDF(s) before send",
                flush=True,
            )
        print(
            f"[mail] Attach folder {watch_dir!r} count={attach_count} "
            f"→ {len(files)} PDF(s): {[os.path.basename(p) for p in files]}",
            flush=True,
        )

    values = build_email_template_values(
        rpa_id, files, source_upload_file=source_upload_file or "",
    )
    sender = ""
    captured_cc: list[str] = []
    try:
        from mail.mail_meta import read_mail_meta
        meta = read_mail_meta(source_upload_file or "") or {}
        if not meta.get("from") and not meta.get("cc"):
            meta = read_mail_meta(os.environ.get("RPA_UPLOAD_FILE") or "") or meta
        sender = (meta.get("from") or "").strip()
        raw_cc = meta.get("cc") or []
        if isinstance(raw_cc, str):
            raw_cc = re.split(r"[\s,;]+", raw_cc)
        captured_cc = [e.strip() for e in raw_cc if (e or "").strip()]
    except Exception:
        sender = ""
        captured_cc = []
    values["mail_from"] = sender
    values["mail_cc"] = ", ".join(captured_cc)
    raw_subject = job.get("subject", "") or ""
    raw_body = job.get("body", "") or ""
    subject = render_email_template(raw_subject, values)
    body = render_email_template(raw_body, values, html=_looks_like_html(raw_body))
    print(
        f"[mail] Template original_file={values.get('original_file')!r} "
        f"files={values.get('files')!r} subject={subject!r}",
        flush=True,
    )

    to_addr = sender or (job.get("to_emails") or "")
    if sender:
        print(f"[mail] To original sender {sender} (Email Job To is fallback only)", flush=True)
    else:
        print("[mail] No captured sender — using Email Job To", flush=True)

    # Cc = W1 Cc people + Email Job Cc, minus anyone already on To.
    to_set = {
        p.strip().lower()
        for p in re.split(r"[\s,;]+", to_addr or "")
        if p.strip()
    }
    cc_parts: list[str] = []
    seen_cc = set(to_set)
    for part in (captured_cc, re.split(r"[\s,;]+", job.get("cc_emails", "") or "")):
        for addr in part:
            a = (addr or "").strip()
            if not a or a.lower() in seen_cc:
                continue
            seen_cc.add(a.lower())
            cc_parts.append(a)
    cc_addr = ", ".join(cc_parts)
    if captured_cc:
        print(f"[mail] Cc from W1 mail: {captured_cc}", flush=True)
    if job.get("cc_emails"):
        print(f"[mail] Cc from Email Job: {job.get('cc_emails')}", flush=True)
    print(f"[mail] Final Cc: {cc_addr or '(none)'}", flush=True)

    result = send_email(
        to=to_addr,
        subject=subject,
        body=body,
        files=files,
        cc=cc_addr,
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

        # Parent order-creation mail Excel (W1 download) — not inside _worker_*.
        if source_upload_file:
            src = os.path.abspath(source_upload_file)
            name_l = os.path.basename(src).lower()
            if any(bit in name_l for bit in ("layout", "template")):
                print(f"[mail] Keeping protected upload: {src}", flush=True)
            elif os.path.isfile(src):
                extra = cleanup_folder_files("", also=[src])
                deleted += extra
                if extra:
                    print(
                        f"[mail] Removed source mail Excel from parent folder: "
                        f"{os.path.basename(src)}",
                        flush=True,
                    )
            result["cleaned_files"] = [os.path.basename(p) for p in deleted]

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
