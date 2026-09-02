"""Read ZLSDF50270 upload Excels — sales org is 7101 or 7104."""
from __future__ import annotations

import os
import re
from collections import Counter
from typing import Optional

KNOWN_SALES_ORGS = ("7101", "7104")
DEFAULT_SALES_ORG = "7101"

_HEADER_RE = re.compile(
    r"sales[\s._-]*org|vkorg|sales[\s._-]*organi[sz]ation",
    re.I,
)
_ORG_CELL_RE = re.compile(r"^710[14](?:\.0+)?$")


def _as_org(value) -> Optional[str]:
    """Return 7101/7104 if *value* is that sales org, else None."""
    if value is None:
        return None
    try:
        import pandas as pd
        if pd.isna(value):
            return None
    except Exception:
        pass
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none"):
        return None
    compact = re.sub(r"[\s,]", "", text)
    if _ORG_CELL_RE.match(compact):
        return compact[:4]
    if compact in KNOWN_SALES_ORGS:
        return compact
    return None


def _pick_org(counts: Counter[str], *, source: str) -> Optional[str]:
    if not counts:
        return None
    if len(counts) == 1:
        org = next(iter(counts))
        print(f"[RPA] Sales Org from Excel ({source}): {org}")
        return org
    org, n = counts.most_common(1)[0]
    print(
        f"[RPA] Mixed Sales Org values in Excel ({source}): {dict(counts)} "
        f"— using majority {org} ({n})"
    )
    return org


def _read_sheets(path: str):
    import pandas as pd

    try:
        return pd.read_excel(path, sheet_name=None, header=None, dtype=object)
    except Exception as first:
        try:
            from excel_decrypt import prepare_for_reading
            readable = prepare_for_reading(path)
            return pd.read_excel(readable, sheet_name=None, header=None, dtype=object)
        except Exception as second:
            raise RuntimeError(
                f"Could not open Excel for Sales Org: {first}; decrypt retry: {second}"
            ) from second


def detect_sales_org(path: Optional[str] = None, default: str = DEFAULT_SALES_ORG) -> str:
    """Open the upload Excel and return 7101 or 7104.

    Prefers a Sales Org / VKORG column. Falls back to scanning cells for those
    two codes. Defaults to 7101 when the file is missing or has no match.
    """
    path = (path or os.environ.get("RPA_UPLOAD_FILE") or "").strip()
    if not path or not os.path.isfile(path):
        print(f"[RPA] No upload Excel for Sales Org — defaulting to {default}")
        return default

    try:
        sheets = _read_sheets(path)
    except Exception as e:
        print(f"[RPA] Could not read Excel for Sales Org ({e}) — defaulting to {default}")
        return default

    header_hits: Counter[str] = Counter()
    cell_hits: Counter[str] = Counter()

    for sheet_name, df in (sheets or {}).items():
        if df is None or df.empty:
            continue
        org_cols: list[int] = []
        header_row = None
        for r in range(min(25, len(df))):
            row_vals = [str(c).strip() if c is not None else "" for c in df.iloc[r].tolist()]
            hits = [i for i, v in enumerate(row_vals) if _HEADER_RE.search(v or "")]
            if hits:
                org_cols = hits
                header_row = r
                break
        if header_row is not None and org_cols:
            for r in range(header_row + 1, len(df)):
                for c in org_cols:
                    if c >= len(df.columns):
                        continue
                    org = _as_org(df.iloc[r, c])
                    if org:
                        header_hits[org] += 1
            if header_hits:
                print(
                    f"[RPA] Sales Org column in sheet={sheet_name!r} "
                    f"cols={org_cols} row={header_row}"
                )
                picked = _pick_org(header_hits, source=os.path.basename(path))
                if picked:
                    return picked

        for r in range(len(df)):
            for c in range(len(df.columns)):
                org = _as_org(df.iloc[r, c])
                if org:
                    cell_hits[org] += 1

    picked = _pick_org(cell_hits, source=f"cell scan {os.path.basename(path)}")
    if picked:
        return picked

    print(
        f"[RPA] No 7101/7104 Sales Org in {os.path.basename(path)} "
        f"— defaulting to {default}"
    )
    return default
