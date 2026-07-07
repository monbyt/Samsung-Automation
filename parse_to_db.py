"""
Parses Excel files into the SQL database.

- Decrypts password-protected workbooks via Excel COM (Windows).
- Unzips .zip attachments and parses the Excel inside.
- ingest_mode=replace: new version replaces prior rows for the same filter_id.
- ingest_mode=append: keeps history (every ingest adds rows).
- Identical file hash is always skipped (unchanged attachment).
"""
import hashlib
import os
import uuid
import zipfile
from datetime import datetime

import pandas as pd
from sqlalchemy import func, select, text

import config
from db import engine, ingestion_log, init_db
from excel_decrypt import prepare_for_reading


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_hash(path):
    return file_hash(path)


def _already_ingested(file_hash_value):
    init_db()
    with engine.connect() as conn:
        count = conn.execute(
            select(func.count())
            .select_from(ingestion_log)
            .where(ingestion_log.c.file_hash == file_hash_value)
            .where(ingestion_log.c.status == "success")
        ).scalar()
    return bool(count)


def get_last_ingest_hash_for_filter(filter_id):
    """Hash of the last successful ingest for this mail job (if any)."""
    if not filter_id:
        return None
    init_db()
    with engine.connect() as conn:
        row = conn.execute(
            select(ingestion_log.c.file_hash)
            .where(ingestion_log.c.filter_id == filter_id)
            .where(ingestion_log.c.status == "success")
            .order_by(ingestion_log.c.id.desc())
            .limit(1)
        ).first()
    return row[0] if row else None


def _clear_filter_rows(table, filter_id):
    """Remove prior rows for this mail job before loading an updated file."""
    if not filter_id or filter_id == "manual":
        return 0
    init_db()
    with engine.begin() as conn:
        result = conn.execute(
            text(f"DELETE FROM {table} WHERE filter_id = :fid"),
            {"fid": filter_id},
        )
        return result.rowcount or 0


def _clean_columns(df):
    df.columns = [
        str(c).strip().lower().replace(" ", "_").replace("-", "_").replace("/", "_")
        for c in df.columns
    ]
    return df


def extract_zip_if_needed(path: str) -> str:
    """If *path* is a .zip, extract the Excel inside and return its path."""
    if not path.lower().endswith(".zip"):
        return path

    dest_dir = os.path.dirname(path) or "."
    with zipfile.ZipFile(path, "r") as zf:
        members = [m for m in zf.namelist() if m.lower().endswith((".xlsx", ".xls"))]
        if not members:
            raise ValueError(f"No Excel file found inside zip: {os.path.basename(path)}")
        member = (
            members[0] if len(members) == 1
            else max(members, key=lambda m: zf.getinfo(m).file_size)
        )
        zf.extract(member, dest_dir)
        extracted = os.path.normpath(os.path.join(dest_dir, member))

    print(f"Extracted {os.path.basename(extracted)} from {os.path.basename(path)}")
    return extracted


def _log(batch_id, path, file_hash_value, rows, status, message,
         filter_id=None, mail_subject=None):
    with engine.begin() as conn:
        conn.execute(ingestion_log.insert().values(
            batch_id=batch_id,
            filter_id=filter_id,
            mail_subject=mail_subject,
            source_file=os.path.basename(path),
            file_hash=file_hash_value,
            row_count=rows,
            status=status,
            message=(message or "")[:1000],
            loaded_at=datetime.now(),
        ))


def parse_file(path, table=None, filter_id=None, mail_subject=None,
               ingest_mode="replace", force=False):
    """
    Load Excel into SQL.

    ingest_mode:
      replace — drop existing rows for this filter_id, then insert (latest snapshot).
      append  — add rows without removing older versions.
    """
    table = table or config.ORDERS_TABLE
    archive_name = os.path.basename(path)
    file_hash_value = file_hash(path)

    if not force and _already_ingested(file_hash_value):
        print(f"Unchanged file, skipping: {archive_name}")
        return {"skipped": True, "reason": "unchanged", "rows": 0}

    batch_id = uuid.uuid4().hex[:12]
    mode = (ingest_mode or "replace").lower()

    try:
        spreadsheet = extract_zip_if_needed(path)
        readable = prepare_for_reading(spreadsheet)
        df = pd.read_excel(readable)
        df = _clean_columns(df)
        df["source_file"] = archive_name
        df["batch_id"] = batch_id
        df["filter_id"] = filter_id or ""
        df["loaded_at"] = datetime.now()

        replaced = 0
        if mode == "replace":
            replaced = _clear_filter_rows(table, filter_id)

        df.to_sql(table, engine, if_exists="append", index=False)
        note = f"replaced {replaced} old rows" if replaced else mode
        _log(batch_id, path, file_hash_value, len(df), "success", note,
             filter_id=filter_id, mail_subject=mail_subject)
        print(f"Loaded {len(df)} rows from {archive_name} → {table} ({note})")
        return {"skipped": False, "rows": len(df), "replaced": replaced, "batch_id": batch_id}
    except Exception as e:
        _log(batch_id, path, file_hash_value, 0, "error", str(e),
             filter_id=filter_id, mail_subject=mail_subject)
        print(f"Error parsing {archive_name}: {e}")
        raise


def ingest_download(path, table=None, filter_id=None, mail_subject=None,
                    ingest_mode="replace", force=False):
    """Decrypt (if needed) and load one downloaded attachment."""
    return parse_file(
        path, table=table, filter_id=filter_id, mail_subject=mail_subject,
        ingest_mode=ingest_mode, force=force,
    )


def parse_latest():
    if not os.path.isdir(config.DOWNLOAD_DIR):
        print("No download folder yet.")
        return
    files = [
        os.path.join(config.DOWNLOAD_DIR, f)
        for f in os.listdir(config.DOWNLOAD_DIR)
        if f.lower().endswith((".xlsx", ".xls", ".zip"))
    ]
    if not files:
        print("No Excel files to parse.")
        return
    latest = max(files, key=os.path.getmtime)
    parse_file(latest)


if __name__ == "__main__":
    parse_latest()
