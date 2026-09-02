"""Sidecar next to a downloaded mail Excel: original From / Cc addresses."""
from __future__ import annotations

import json
import os
from typing import Iterable, Optional


def meta_path(excel_path: str) -> str:
    return (excel_path or "") + ".mail.json"


def write_mail_meta(
    excel_path: str,
    *,
    from_email: str = "",
    cc_emails: Optional[Iterable[str]] = None,
    subject: str = "",
) -> str:
    path = meta_path(excel_path)
    if not excel_path:
        return ""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    cc = [e.strip() for e in (cc_emails or []) if (e or "").strip()]
    data = {
        "from": (from_email or "").strip(),
        "cc": cc,
        "subject": (subject or "").strip(),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return path


def read_mail_meta(excel_path: str) -> dict:
    path = meta_path(excel_path)
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def copy_mail_meta(src_excel: str, dest_excel: str) -> str:
    src = meta_path(src_excel)
    if not src or not os.path.isfile(src) or not dest_excel:
        return ""
    dest = meta_path(dest_excel)
    if os.path.abspath(src) == os.path.abspath(dest):
        return dest
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(src, "rb") as fh:
        raw = fh.read()
    with open(dest, "wb") as fh:
        fh.write(raw)
    return dest
