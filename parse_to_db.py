"""
Parses Excel files into the SQL database.

- Decrypts password-protected workbooks via Excel COM (Windows).
- Rows go into per-filter tables (tagged with source_file, batch_id, loaded_at).
- De-duplicated by content hash — re-downloading the same file is skipped.
"""
import hashlib
import os
import uuid
from datetime import datetime

import pandas as pd
from sqlalchemy import func, select

import config
from db import engine, ingestion_log, init_db
from excel_decrypt import prepare_for_reading


def _file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _already_ingested(file_hash):
    init_db()
    with engine.connect() as conn:
        count = conn.execute(
            select(func.count())
            .select_from(ingestion_log)
            .where(ingestion_log.c.file_hash == file_hash)
            .where(ingestion_log.c.status == "success")
        ).scalar()
    return bool(count)


def _clean_columns(df):
    df.columns = [
        str(c).strip().lower().replace(" ", "_").replace("-", "_").replace("/", "_")
        for c in df.columns
    ]
    return df


def _log(batch_id, path, file_hash, rows, status, message,
         filter_id=None, mail_subject=None):
    with engine.begin() as conn:
        conn.execute(ingestion_log.insert().values(
            batch_id=batch_id,
            filter_id=filter_id,
            mail_subject=mail_subject,
            source_file=os.path.basename(path),
            file_hash=file_hash,
            row_count=rows,
            status=status,
            message=(message or "")[:1000],
            loaded_at=datetime.now(),
        ))


def parse_file(path, table=None, filter_id=None, mail_subject=None):
    table = table or config.ORDERS_TABLE
    file_hash = _file_hash(path)
    name = os.path.basename(path)

    if _already_ingested(file_hash):
        print(f"Already ingested, skipping: {name}")
        return

    batch_id = uuid.uuid4().hex[:12]
    try:
        readable = prepare_for_reading(path)
        df = pd.read_excel(readable)
        df = _clean_columns(df)
        df["source_file"] = name
        df["batch_id"] = batch_id
        df["filter_id"] = filter_id or ""
        df["loaded_at"] = datetime.now()

        df.to_sql(table, engine, if_exists="append", index=False)
        _log(batch_id, path, file_hash, len(df), "success", "",
             filter_id=filter_id, mail_subject=mail_subject)
        print(f"Loaded {len(df)} rows from {name} → {table}")
    except Exception as e:
        _log(batch_id, path, file_hash, 0, "error", str(e),
             filter_id=filter_id, mail_subject=mail_subject)
        print(f"Error parsing {name}: {e}")
        raise


def ingest_download(path, table=None, filter_id=None, mail_subject=None):
    """Decrypt (if needed) and load one downloaded attachment."""
    parse_file(path, table=table, filter_id=filter_id, mail_subject=mail_subject)


def parse_latest():
    if not os.path.isdir(config.DOWNLOAD_DIR):
        print("No download folder yet.")
        return
    files = [
        os.path.join(config.DOWNLOAD_DIR, f)
        for f in os.listdir(config.DOWNLOAD_DIR)
        if f.lower().endswith((".xlsx", ".xls"))
    ]
    if not files:
        print("No Excel files to parse.")
        return
    latest = max(files, key=os.path.getmtime)
    parse_file(latest)


if __name__ == "__main__":
    parse_latest()
