"""
Daily Order Creation recap for the OC team.

After each create-SO / P/I run, snapshot the business facts (sales orders,
sold-to, sales org, whether a P/I went out) before files are deleted. Once a
day those facts are emailed as a checklist of what was created — and what
still needs a human look.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from html import escape as html_escape
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, select

from db import engine, init_db

_OC_RPA_IDS = frozenset({"final_oc", "final_oc_multi"})
_CAPTURED_SOS_RE = re.compile(
    r"Captured\s+(\d+)\s+SO\(s\):\s*(\[[^\]]*\])",
    re.I,
)
_FINISHED_SO_RE = re.compile(r"Finished SO\s+([A-Za-z0-9]+)", re.I)
_SALES_ORG_RE = re.compile(r"Sales Org from Excel:\s*(710[14])", re.I)
_MIXED_ORG_RE = re.compile(
    r"Mixed Sales Org values in Excel \([^)]+\): (\{[^}]+\})[^\n]*majority (710[14])",
    re.I,
)
_SO_IN_NAME_RE = re.compile(r"\b(\d{10})\b")

_COL_PATTERNS = {
    "so": re.compile(
        r"sales[\s._-]*order|sales[\s._-]*document|\bvbeln\b",
        re.I,
    ),
    "sold_to": re.compile(r"sold[\s._-]*to|soldto|\bkunag\b", re.I),
    "ship_to": re.compile(r"ship[\s._-]*to|shipto|\bkunwe\b", re.I),
    "org": re.compile(r"sales[\s._-]*org|\bvkorg\b", re.I),
    "message": re.compile(r"message|status|error|type", re.I),
    "name": re.compile(r"customer\s*name|sold-to\s*name|\bname1\b", re.I),
}

metadata = MetaData()

oc_digest_events = Table(
    "oc_digest_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime),
    Column("rpa_id", String(64)),
    Column("kind", String(32)),
    Column("status", String(20)),
    Column("upload_file", String(500)),
    Column("mail_from", String(200)),
    Column("mail_subject", String(500)),
    Column("sales_org", String(16)),
    Column("so_numbers", Text),
    Column("pdf_names", Text),
    Column("warnings", Text),
    Column("email_to", String(500)),
    Column("message", Text),
    Column("payload", Text),
)


def _ensure_table():
    init_db()
    metadata.create_all(engine)


def _log(msg: str) -> None:
    print(f"[digest {datetime.now():%H:%M:%S}] {msg}", flush=True)


def is_oc_rpa(rpa_id: str = "", name: str = "") -> bool:
    rid = (rpa_id or "").strip().lower()
    nm = (name or "").strip().lower()
    if rid in _OC_RPA_IDS:
        return True
    blob = f"{rid} {nm}"
    return "order" in blob and "creat" in blob


def is_oc_mail_job(mail_job_id: str) -> bool:
    if not mail_job_id:
        return False
    mid = mail_job_id.lower()
    if "order" in mid and "creat" in mid:
        return True
    try:
        from rpa.jobs_db import list_for_mail_job
        for job in list_for_mail_job(mail_job_id) or []:
            if is_oc_rpa(job.get("rpa_id") or "", job.get("name") or ""):
                return True
    except Exception:
        pass
    return False


def _json_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    if not value:
        return []
    try:
        data = json.loads(value)
        if isinstance(data, list):
            return [str(v) for v in data if str(v).strip()]
    except Exception:
        pass
    return []


def _unique(items: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in items:
        item = str(raw or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _cell_text(raw) -> str:
    if raw is None:
        return ""
    try:
        import pandas as pd
        if pd.isna(raw):
            return ""
    except Exception:
        pass
    text = str(raw).strip()
    if not text or text.lower() in ("nan", "none"):
        return ""
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]
    return text


def _sap_num(raw) -> str:
    compact = re.sub(r"[^\d]", "", _cell_text(raw))
    return compact


def _read_text_tail(path: str, max_chars: int = 80_000) -> str:
    if not path or not os.path.isfile(path):
        return ""
    try:
        size = os.path.getsize(path)
        with open(path, encoding="utf-8", errors="replace") as fh:
            if size > max_chars:
                fh.seek(max(0, size - max_chars))
                fh.readline()
            return fh.read()
    except OSError:
        return ""


def _current_log_path() -> str:
    try:
        import sys
        path = getattr(sys.stdout, "path", "") or ""
        if path and os.path.isfile(path):
            return path
    except Exception:
        pass
    return ""


def _mail_meta(upload_file: str) -> dict:
    try:
        from mail.mail_meta import read_mail_meta
        return read_mail_meta(upload_file or "") or {}
    except Exception:
        return {}


def _latest_result_excel(directory: str) -> str:
    if not directory or not os.path.isdir(directory):
        return ""
    candidates = []
    for name in os.listdir(directory):
        lower = name.lower()
        if not lower.endswith((".xlsx", ".xls", ".xlsm")):
            continue
        if "result" not in lower:
            continue
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            candidates.append(path)
    if not candidates:
        return ""
    return max(candidates, key=os.path.getmtime)


def _header_rows(path: str) -> List[dict]:
    """Read the first tabular sheet as a list of {so, sold_to, org, ...}."""
    if not path or not os.path.isfile(path):
        return []
    try:
        import pandas as pd
        sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=object)
    except Exception as e:
        _log(f"Could not read Excel {os.path.basename(path)}: {e}")
        return []
    for df in (sheets or {}).values():
        if df is None or df.empty:
            continue
        header_row = None
        colmap: Dict[str, int] = {}
        for r in range(min(30, len(df))):
            vals = [_cell_text(c) for c in df.iloc[r].tolist()]
            found: Dict[str, int] = {}
            for i, v in enumerate(vals):
                if not v:
                    continue
                for key, pat in _COL_PATTERNS.items():
                    if key in found:
                        continue
                    if pat.search(v):
                        found[key] = i
            if "so" in found or "sold_to" in found or "org" in found:
                header_row = r
                colmap = found
                break
        if header_row is None:
            continue
        rows: List[dict] = []
        for r in range(header_row + 1, len(df)):
            item: Dict[str, str] = {}
            empty = True
            for key, col in colmap.items():
                if col >= len(df.columns):
                    continue
                val = _cell_text(df.iloc[r, col])
                if key in ("so", "sold_to", "ship_to", "org"):
                    num = _sap_num(val)
                    if key == "org" and num in ("7101", "7104"):
                        val = num
                    elif key != "org" and 5 <= len(num) <= 12:
                        val = num
                    else:
                        val = val if key == "message" or key == "name" else num
                if val:
                    empty = False
                    item[key] = val
            if not empty:
                rows.append(item)
        if rows:
            return rows
    return []


def _pdf_facts(directory: str) -> Dict[str, Any]:
    names: List[str] = []
    sos: List[str] = []
    dupes = 0
    if not directory or not os.path.isdir(directory):
        return {"names": names, "sos": sos, "duplicate_hashes": 0}
    try:
        from mail.sender import _file_sha256, _pdf_files_in
        paths = _pdf_files_in(directory)
    except Exception:
        paths = []
        try:
            for name in os.listdir(directory):
                if name.lower().endswith(".pdf"):
                    paths.append(os.path.join(directory, name))
        except OSError:
            paths = []
    seen_hash = set()
    for path in paths:
        names.append(os.path.basename(path))
        sos.extend(_SO_IN_NAME_RE.findall(os.path.basename(path)))
        try:
            digest = _file_sha256(path)
        except Exception:
            continue
        if digest in seen_hash:
            dupes += 1
        else:
            seen_hash.add(digest)
    return {"names": names, "sos": _unique(sos), "duplicate_hashes": dupes}


def _facts_from_log(text: str) -> Dict[str, Any]:
    sos: List[str] = []
    sales_org = ""
    mixed: Dict[str, Any] = {}
    for match in _CAPTURED_SOS_RE.finditer(text or ""):
        try:
            parsed = json.loads(match.group(2).replace("'", '"'))
            if isinstance(parsed, list):
                sos.extend(str(x) for x in parsed)
        except Exception:
            sos.extend(_SO_IN_NAME_RE.findall(match.group(2)))
    sos.extend(_FINISHED_SO_RE.findall(text or ""))
    org_match = _SALES_ORG_RE.search(text or "")
    if org_match:
        sales_org = org_match.group(1)
    mixed_match = _MIXED_ORG_RE.search(text or "")
    if mixed_match:
        mixed = {"raw": mixed_match.group(1), "used": mixed_match.group(2)}
    return {
        "so_numbers": _unique(sos),
        "sales_org": sales_org,
        "mixed": mixed,
        "duplicate_print": bool(re.search(r"identical|duplicate PDF|same bytes", text or "", re.I)),
        "sap_no_documents": "no documents" in (text or "").lower(),
        "sap_check_so": "check input s/o" in (text or "").lower(),
        "sap_no_line": "no line selected" in (text or "").lower(),
        "sap_org_mismatch": "upload sales org differs" in (text or "").lower(),
    }


def _human_failure(message: str) -> str:
    first = (message or "").strip().splitlines()[0] if message else ""
    first = re.sub(r'File ".*?"', "", first).strip(" :,-")
    low = first.lower()
    if "check input" in low:
        return "SAP did not accept the sales order number. The order or P/I may be incomplete."
    if "no documents" in low:
        return "SAP had no document to print a P/I for this sales order."
    if "no line selected" in low:
        return "Could not select the correct P/I line for this sold-to. No P/I printed."
    if "upload sales org differs" in low:
        return "The Excel sales org does not match the sales org on the create-order screen."
    if "no sales order numbers" in low:
        return "No sales order numbers came back from Create Sales Order. Nothing was created."
    if "identical" in low or "same bytes" in low:
        return "Two sales orders received the same P/I print. Please re-print the correct one."
    return first[:240] if first else "Order creation did not finish."


def collect_oc_facts(
    *,
    rpa_id: str,
    job: Optional[dict] = None,
    upload_file: str = "",
    upload_dir: str = "",
    download_dir: str = "",
    log_path: str = "",
) -> Dict[str, Any]:
    """Read order facts while the Excel / P/I files still exist."""
    job = job or {}
    log_path = log_path or _current_log_path()
    log_text = _read_text_tail(log_path)
    from_log = _facts_from_log(log_text)
    pdfs = _pdf_facts(download_dir)
    upload_rows = _header_rows(upload_file) if upload_file else []
    result_rows = _header_rows(_latest_result_excel(upload_dir))
    meta = _mail_meta(upload_file)

    so_numbers = _unique(
        [r.get("so") or "" for r in result_rows]
        + list(from_log.get("so_numbers") or [])
        + list(pdfs.get("sos") or [])
    )
    sold_tos = _unique(
        [r.get("sold_to") or "" for r in upload_rows]
        + [r.get("sold_to") or "" for r in result_rows]
    )
    orgs = _unique(
        [r.get("org") or "" for r in upload_rows if (r.get("org") or "") in ("7101", "7104")]
    )
    sales_org = from_log.get("sales_org") or ""
    if not sales_org and len(orgs) == 1:
        sales_org = orgs[0]
    if not sales_org:
        try:
            from rpa.excel_meta import detect_sales_org
            sales_org = detect_sales_org(upload_file) if upload_file else ""
        except Exception:
            sales_org = ""

    pdf_sos = set(pdfs.get("sos") or [])
    missing_pi = [so for so in so_numbers if so not in pdf_sos] if pdf_sos or pdfs.get("names") else []
    if so_numbers and pdfs.get("names") and not pdf_sos:
        if len(so_numbers) > len(pdfs["names"]):
            missing_pi = so_numbers[len(pdfs["names"]):]

    checks: List[str] = []
    mixed = from_log.get("mixed") or {}
    if mixed or len(orgs) > 1:
        used = (mixed.get("used") if mixed else None) or sales_org or (orgs[0] if orgs else "")
        other = ", ".join(o for o in orgs if o != used) or mixed.get("raw") or ""
        checks.append(
            f"This Excel mixed sales orgs {', '.join(orgs) or mixed.get('raw') or ''}. "
            f"Orders were created under {used}"
            + (f" (file also had {other})" if other and other != used else "")
            + ". Please confirm that is the right org."
        )
    if missing_pi:
        checks.append(
            "Sales order(s) created with no P/I: " + ", ".join(missing_pi)
        )
    if pdfs.get("duplicate_hashes") or from_log.get("duplicate_print"):
        checks.append(
            "Two sales orders got the same P/I print. Please re-print the correct P/I."
        )
    if from_log.get("sap_org_mismatch"):
        checks.append(
            "Excel sales org does not match the org on the create-order screen. "
            "Orders may have been created under the wrong org."
        )
    if from_log.get("sap_check_so"):
        checks.append("SAP rejected a sales order number (CHECK input s/o).")
    if from_log.get("sap_no_documents"):
        checks.append("SAP had no document to print a P/I.")
    if from_log.get("sap_no_line"):
        checks.append("Could not pick the P/I line for this sold-to. P/I may be missing.")

    return {
        "rpa_id": rpa_id,
        "rpa_name": (job.get("name") or rpa_id or ""),
        "upload_file": os.path.basename(upload_file or "") or "",
        "mail_from": (meta.get("from") or "").strip(),
        "mail_subject": (meta.get("subject") or "").strip(),
        "sales_org": sales_org,
        "sold_tos": sold_tos,
        "so_numbers": so_numbers,
        "pdf_names": pdfs.get("names") or [],
        "missing_pi": missing_pi,
        "warnings": _unique(checks),
        "log_path": os.path.basename(log_path) if log_path else "",
    }


def record_event(kind: str, status: str, facts: Optional[dict] = None, **extra) -> None:
    _ensure_table()
    facts = dict(facts or {})
    facts.update({k: v for k, v in extra.items() if v is not None})
    payload = json.dumps(facts, default=str)
    with engine.begin() as conn:
        conn.execute(
            oc_digest_events.insert().values(
                created_at=datetime.now(),
                rpa_id=(facts.get("rpa_id") or "")[:64],
                kind=(kind or "run")[:32],
                status=(status or "")[:20],
                upload_file=(facts.get("upload_file") or "")[:500],
                mail_from=(facts.get("mail_from") or "")[:200],
                mail_subject=(facts.get("mail_subject") or "")[:500],
                sales_org=(facts.get("sales_org") or "")[:16],
                so_numbers=json.dumps(facts.get("so_numbers") or []),
                pdf_names=json.dumps(facts.get("pdf_names") or []),
                warnings=json.dumps(facts.get("warnings") or []),
                email_to=(facts.get("email_to") or "")[:500],
                message=(facts.get("message") or "")[:2000],
                payload=payload[:20000],
            )
        )


def record_rpa_run_event(
    *,
    rpa_id: str,
    job: Optional[dict] = None,
    status: str,
    message: str = "",
    upload_file: str = "",
    upload_dir: str = "",
    download_dir: str = "",
    email_info: Optional[dict] = None,
    facts: Optional[dict] = None,
) -> None:
    job = job or {}
    if not is_oc_rpa(rpa_id, job.get("name") or ""):
        return
    if facts is None:
        facts = collect_oc_facts(
            rpa_id=rpa_id,
            job=job,
            upload_file=upload_file or "",
            upload_dir=upload_dir or "",
            download_dir=download_dir or "",
        )
    else:
        facts = dict(facts)
    facts["message"] = _human_failure(message) if (status or "") != "ok" else (message or "")[:2000]
    email_info = email_info or {}
    if email_info.get("to"):
        facts["email_to"] = email_info.get("to") or ""
    if email_info.get("cc"):
        facts["email_cc"] = email_info.get("cc") or ""
    emailed = email_info.get("files") or []
    if emailed and not facts.get("pdf_names"):
        facts["pdf_names"] = [os.path.basename(p) for p in emailed]
    skipped = (email_info.get("skipped") or "").lower()
    if skipped:
        if "disabled" in skipped or "no email job" in skipped or "send failed" in skipped:
            facts.setdefault("warnings", []).append("P/I was not emailed for this request.")
        elif "deferred" not in skipped:
            facts.setdefault("warnings", []).append("P/I was not emailed for this request.")
    elif status == "ok" and not facts.get("email_to") and facts.get("so_numbers"):
        facts.setdefault("warnings", []).append("P/I was not emailed for this request.")
    if facts.get("mail_from") and facts.get("email_to"):
        if facts["mail_from"].lower() not in (facts["email_to"] or "").lower():
            facts.setdefault("warnings", []).append(
                f"P/I was emailed to {facts['email_to']}, not the person who sent the request "
                f"({facts['mail_from']})."
            )
    if (status or "") != "ok" and facts.get("message"):
        facts.setdefault("warnings", []).append(facts["message"])
    facts["warnings"] = _unique(facts.get("warnings") or [])
    record_event("run", status, facts)
    _log(
        f"Captured OC run {rpa_id} status={status} "
        f"SOs={len(facts.get('so_numbers') or [])} "
        f"sold-to={len(facts.get('sold_tos') or [])} "
        f"checks={len(facts.get('warnings') or [])}"
    )


def record_idle_check(mail_job_id: str, downloads: int = 0) -> None:
    if not is_oc_mail_job(mail_job_id):
        return
    record_event(
        "idle",
        "ok",
        {
            "rpa_id": mail_job_id,
            "message": (
                f"{downloads} file(s) downloaded"
                if downloads
                else "Mail job ran — no new Order Creation mail"
            ),
            "downloads": downloads,
        },
    )


def digest_enabled() -> bool:
    try:
        from mail.settings_db import get_setting
        raw = (get_setting("digest_enabled", "1") or "1").strip().lower()
        return raw not in ("0", "false", "no", "off")
    except Exception:
        return True


def digest_time_str() -> str:
    try:
        from mail.settings_db import get_setting
        raw = (get_setting("digest_time", "18:00") or "18:00").strip()
    except Exception:
        raw = "18:00"
    try:
        datetime.strptime(raw, "%H:%M")
        return raw
    except ValueError:
        return "18:00"


def digest_recipients() -> str:
    try:
        from mail.settings_db import get_setting
        dedicated = (get_setting("digest_emails", "") or "").strip()
        if dedicated:
            return dedicated
        return (get_setting("alert_emails", "") or "").strip()
    except Exception:
        return ""


def skip_empty() -> bool:
    try:
        from mail.settings_db import get_setting
        raw = (get_setting("digest_skip_empty", "1") or "1").strip().lower()
        return raw not in ("0", "false", "no", "off")
    except Exception:
        return True


def last_sent_at() -> Optional[datetime]:
    try:
        from mail.settings_db import get_setting
        raw = (get_setting("digest_last_sent_at", "") or "").strip()
        if not raw:
            return None
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def _mark_sent(when: Optional[datetime] = None) -> None:
    when = when or datetime.now()
    from mail.settings_db import set_setting
    set_setting("digest_last_sent_at", when.isoformat(timespec="seconds"))


def _event_payload(event: dict) -> dict:
    try:
        data = json.loads(event.get("payload") or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _events_since(since: datetime) -> List[dict]:
    _ensure_table()
    with engine.connect() as conn:
        rows = conn.execute(
            select(oc_digest_events)
            .where(oc_digest_events.c.created_at >= since)
            .order_by(oc_digest_events.c.created_at.asc())
        ).fetchall()
    out = []
    for row in rows:
        mapping = dict(row._mapping)
        mapping["so_numbers"] = _json_list(mapping.get("so_numbers"))
        mapping["pdf_names"] = _json_list(mapping.get("pdf_names"))
        mapping["warnings"] = _json_list(mapping.get("warnings"))
        extra = _event_payload(mapping)
        mapping["sold_tos"] = _json_list(extra.get("sold_tos"))
        mapping["missing_pi"] = _json_list(extra.get("missing_pi"))
        out.append(mapping)
    return out


def _td(value: str) -> str:
    return f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;vertical-align:top'>{html_escape(value or '—')}</td>"


def _th(value: str) -> str:
    return (
        f"<th style='padding:6px 10px;border-bottom:2px solid #333;text-align:left;"
        f"font-size:13px'>{html_escape(value)}</th>"
    )


def build_digest_text(since: Optional[datetime] = None, until: Optional[datetime] = None) -> str:
    until = until or datetime.now()
    since = since or last_sent_at() or (until - timedelta(hours=24))
    events = _events_since(since)
    runs = [e for e in events if e.get("kind") == "run"]
    failed = [e for e in runs if (e.get("status") or "") not in ("ok", "skipped")]
    all_sos = _unique(so for e in runs for so in (e.get("so_numbers") or []))
    all_sold = _unique(s for e in runs for s in (e.get("sold_tos") or []))
    checks = _unique(w for e in runs for w in (e.get("warnings") or []))
    for e in failed:
        if e.get("message"):
            who = e.get("upload_file") or e.get("mail_from") or "a request"
            checks.append(f"{who}: {e['message']}")
    checks = _unique(checks)

    n = len(all_sos)
    headline = (
        f"{n} sales order{'s' if n != 1 else ''} created today"
        if n
        else "No sales orders were created today"
    )
    if failed:
        headline += f" — {len(failed)} request{'s' if len(failed) != 1 else ''} did not finish"

    parts = [
        "<div style='font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222;line-height:1.45'>",
        f"<p style='font-size:18px;margin:0 0 4px'><b>Order Creation — {until:%d %b %Y}</b></p>",
        f"<p style='color:#555;margin:0 0 16px'>{html_escape(headline)}.</p>",
    ]

    parts.append("<p style='margin:0 0 8px'><b>Please check</b></p>")
    if checks:
        parts.append("<ul style='margin:0 0 18px;padding-left:20px'>")
        for item in checks:
            parts.append(f"<li>{html_escape(item)}</li>")
        parts.append("</ul>")
    else:
        parts.append(
            "<p style='margin:0 0 18px;color:#555'>Nothing outstanding — "
            "sales org, P/I, and sold-to look consistent.</p>"
        )

    parts.append("<p style='margin:0 0 8px'><b>Sales orders</b></p>")
    if not all_sos and not failed:
        parts.append("<p style='color:#555'>None.</p>")
    else:
        parts.append(
            "<table style='border-collapse:collapse;width:100%;max-width:820px;"
            "font-size:13px;margin-bottom:18px'>"
        )
        parts.append(
            "<tr>"
            + _th("Sales order")
            + _th("Sold-to")
            + _th("Sales org")
            + _th("P/I")
            + _th("Requested by")
            + "</tr>"
        )
        for e in runs:
            sold = ", ".join(e.get("sold_tos") or []) or "—"
            org = e.get("sales_org") or "—"
            who = e.get("mail_from") or "—"
            missing = set(e.get("missing_pi") or [])
            sos = e.get("so_numbers") or []
            if not sos:
                pi = "Not created"
                parts.append(
                    "<tr>"
                    + _td("—")
                    + _td(sold)
                    + _td(org)
                    + _td(pi)
                    + _td(who)
                    + "</tr>"
                )
                continue
            for so in sos:
                if so in missing:
                    pi = "Missing"
                elif e.get("email_to"):
                    pi = f"Sent to {e['email_to']}"
                elif e.get("pdf_names"):
                    pi = "Printed"
                else:
                    pi = "Unknown"
                parts.append(
                    "<tr>"
                    + _td(so)
                    + _td(sold)
                    + _td(org)
                    + _td(pi)
                    + _td(who)
                    + "</tr>"
                )
        parts.append("</table>")

    if all_sold:
        parts.append(
            f"<p style='margin:0 0 16px;color:#555'>Sold-to today: "
            f"{html_escape(', '.join(all_sold))}</p>"
        )

    parts.append("<p style='margin:0 0 8px'><b>Requests</b></p>")
    if not runs:
        parts.append("<p style='color:#555'>No Order Creation requests today.</p>")
    else:
        parts.append("<ul style='margin:0 0 18px;padding-left:20px'>")
        for e in runs:
            stamp = (
                e["created_at"].strftime("%H:%M")
                if e.get("created_at")
                else ""
            )
            who = e.get("mail_from") or "unknown sender"
            fname = e.get("upload_file") or "Excel"
            sos = ", ".join(e.get("so_numbers") or []) or "no SO created"
            org = e.get("sales_org") or "?"
            line = f"{stamp}  {who} — {fname}  (org {org})  {sos}"
            if (e.get("status") or "") != "ok":
                line += " — did not finish"
            parts.append(f"<li>{html_escape(line)}</li>")
        parts.append("</ul>")

    parts.append(
        "<p style='color:#777;font-size:12px;margin:16px 0 0'>"
        "This is the end-of-day recap of orders created from the Order Creation mailbox. "
        "Please check mixed 7101/7104 files, missing P/Is, and any request that did not finish."
        "</p></div>"
    )
    return "".join(parts)


def send_digest(*, force: bool = False) -> str:
    to = digest_recipients()
    if not to:
        raise RuntimeError(
            "No digest recipients. Set digest emails or Alert to in Settings."
        )
    until = datetime.now()
    since = last_sent_at() or (until - timedelta(hours=24))
    events = _events_since(since)
    runs = [e for e in events if e.get("kind") == "run"]
    if not force and skip_empty() and not runs:
        _log("No Order Creation requests since last digest — skipping empty email")
        _mark_sent(until)
        return ""
    body = build_digest_text(since=since, until=until)
    subject = f"Order Creation — {until:%d %b %Y}"
    from mail.sender import send_email
    send_email(to=to, subject=subject, body=body)
    _mark_sent(until)
    _log(f"Sent OC digest to {to}")
    return to


def maybe_send_daily_digest() -> None:
    """Called from the mail scheduler tick. Catch-up if the PC was off at send time."""
    if not digest_enabled():
        return
    if not digest_recipients():
        return
    now = datetime.now()
    try:
        target = datetime.strptime(digest_time_str(), "%H:%M").time()
    except ValueError:
        target = datetime.strptime("18:00", "%H:%M").time()
    if now.time() < target:
        return
    sent = last_sent_at()
    if sent and sent.date() == now.date():
        return
    try:
        send_digest(force=False)
    except Exception as e:
        _log(f"Daily digest send failed: {e}")
