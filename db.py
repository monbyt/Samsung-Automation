"""
Database helpers — schema, WAL mode, monitor status tracking.
"""
import os
from datetime import datetime

from sqlalchemy import (
    MetaData, Table, Column, Integer, String, Text, DateTime,
    create_engine, text,
)

import config

engine = create_engine(
    config.DB_URL,
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)
metadata = MetaData()

ingestion_log = Table(
    "ingestion_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("batch_id", String(64)),
    Column("filter_id", String(64)),
    Column("mail_subject", String(500)),
    Column("source_file", String(500)),
    Column("file_hash", String(64)),
    Column("row_count", Integer),
    Column("status", String(50)),
    Column("message", Text),
    Column("loaded_at", DateTime),
)

monitor_runs = Table(
    "monitor_runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("checked_at", DateTime),
    Column("downloads", Integer, default=0),
    Column("errors", Integer, default=0),
    Column("error_detail", Text),
    Column("status", String(20)),
)

rpa_runs = Table(
    "rpa_runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("rpa_id", String(64)),
    Column("upload_file", String(500)),
    Column("status", String(20)),
    Column("message", Text),
    Column("ran_at", DateTime),
)


def init_db():
    metadata.create_all(engine)
    if config.DB_URL.startswith("sqlite"):
        with engine.begin() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.execute(text("PRAGMA synchronous=NORMAL"))
    _migrate_columns()


def _migrate_columns():
    """Add new ingestion_log columns on existing databases."""
    if not config.DB_URL.startswith("sqlite"):
        return
    with engine.begin() as conn:
        cols = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(ingestion_log)"))
        }
        if "filter_id" not in cols:
            conn.execute(text("ALTER TABLE ingestion_log ADD COLUMN filter_id VARCHAR(64)"))
        if "mail_subject" not in cols:
            conn.execute(text("ALTER TABLE ingestion_log ADD COLUMN mail_subject VARCHAR(500)"))


def record_monitor_run(summary, job_id=None):
    init_db()
    errors = summary.get("errors", [])
    detail = "; ".join(errors)[:2000] if errors else None
    if job_id:
        detail = f"[{job_id}] " + (detail or "")
    with engine.begin() as conn:
        conn.execute(monitor_runs.insert().values(
            checked_at=summary.get("checked_at", datetime.now()),
            downloads=len(summary.get("downloads", [])),
            errors=len(errors),
            error_detail=detail,
            status="error" if errors else "ok",
        ))


def record_rpa_run(rpa_id, status, message=None, upload_file=None):
    init_db()
    with engine.begin() as conn:
        conn.execute(rpa_runs.insert().values(
            rpa_id=rpa_id,
            upload_file=os.path.basename(upload_file) if upload_file else None,
            status=status,
            message=(message or "")[:2000],
            ran_at=datetime.now(),
        ))


def get_engine():
    return engine
