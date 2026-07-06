"""
Parses the newest Order Extract Excel file into the SQL database.

- Rows go into the `orders` table (tagged with source_file, batch_id, loaded_at).
- Every run is recorded in `ingestion_log`.
- Files are de-duplicated by content hash, so re-downloading the same file
  won't create duplicate rows.
"""
import os
import uuid
import hashlib
from datetime import datetime

import pandas as pd
from sqlalchemy import (
    create_engine, MetaData, Table, Column,
    Integer, String, Text, DateTime, select, func,
)

import config

engine = create_engine(config.DB_URL)
metadata = MetaData()

ingestion_log = Table(
    "ingestion_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("batch_id", String(64)),
    Column("source_file", String(500)),
    Column("file_hash", String(64)),
    Column("row_count", Integer),
    Column("status", String(50)),
    Column("message", Text),
    Column("loaded_at", DateTime),
)
metadata.create_all(engine)


def _file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _already_ingested(file_hash):
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


def _log(batch_id, path, file_hash, rows, status, message):
    with engine.begin() as conn:
        conn.execute(ingestion_log.insert().values(
            batch_id=batch_id,
            source_file=os.path.basename(path),
            file_hash=file_hash,
            row_count=rows,
            status=status,
            message=message[:1000],
            loaded_at=datetime.now(),
        ))


def parse_file(path):
    file_hash = _file_hash(path)
    name = os.path.basename(path)

    if _already_ingested(file_hash):
        print(f"Already ingested, skipping: {name}")
        return

    batch_id = uuid.uuid4().hex[:12]
    try:
        df = pd.read_excel(path)          # reads the first sheet
        df = _clean_columns(df)
        df["source_file"] = name
        df["batch_id"] = batch_id
        df["loaded_at"] = datetime.now()

        df.to_sql(config.ORDERS_TABLE, engine, if_exists="append", index=False)
        _log(batch_id, path, file_hash, len(df), "success", "")
        print(f"Loaded {len(df)} rows from {name}")
    except Exception as e:
        _log(batch_id, path, file_hash, 0, "error", str(e))
        print(f"Error parsing {name}: {e}")


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
