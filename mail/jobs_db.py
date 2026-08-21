"""
Mail job definitions stored in SQL — managed from the dashboard.
"""
import os
import re
from datetime import datetime, timedelta

from sqlalchemy import (
    Column, DateTime, Integer, MetaData, String, Table, Text, select, update,
)

import config
from db import engine, init_db

metadata = MetaData()

mail_jobs = Table(
    "mail_jobs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("job_id", String(64), unique=True, nullable=False),
    Column("name", String(200), nullable=False),
    Column("mailbox", String(100), nullable=False),
    Column("subject_pattern", String(500), nullable=False),
    Column("target_table", String(100), nullable=False),
    Column("download_folder", String(200)),
    Column("ingest_mode", String(20), default="replace"),
    Column("extract_zip", Integer, default=0),
    Column("interval_hours", Integer, default=2),  # legacy — prefer interval_minutes
    Column("interval_minutes", Integer, default=120),
    Column("enabled", Integer, default=1),
    Column("last_run", DateTime),
    Column("next_run", DateTime),
    Column("last_status", String(20)),
    Column("last_message", Text),
    Column("created_at", DateTime),
)


def _ensure_tables():
    init_db()
    metadata.create_all(engine)
    _migrate_jobs_columns()


def _migrate_jobs_columns():
    if not config.DB_URL.startswith("sqlite"):
        return
    from sqlalchemy import text as sqltext
    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(sqltext("PRAGMA table_info(mail_jobs)"))}
        if "ingest_mode" not in cols:
            conn.execute(sqltext(
                "ALTER TABLE mail_jobs ADD COLUMN ingest_mode VARCHAR(20) DEFAULT 'replace'"
            ))
        if "interval_hours" not in cols:
            conn.execute(sqltext(
                "ALTER TABLE mail_jobs ADD COLUMN interval_hours INTEGER DEFAULT 2"
            ))
        if "interval_minutes" not in cols:
            conn.execute(sqltext(
                "ALTER TABLE mail_jobs ADD COLUMN interval_minutes INTEGER DEFAULT 120"
            ))
            cols.add("interval_minutes")
        # Prefer minutes: backfill from hours when minutes is missing/zero.
        if "interval_minutes" in cols and "interval_hours" in cols:
            conn.execute(sqltext(
                "UPDATE mail_jobs SET interval_minutes = MAX(1, COALESCE(interval_hours, 2) * 60) "
                "WHERE interval_minutes IS NULL OR interval_minutes = 0"
            ))
        if "download_folder" not in cols:
            conn.execute(sqltext(
                "ALTER TABLE mail_jobs ADD COLUMN download_folder VARCHAR(200)"
            ))
        if "extract_zip" not in cols:
            conn.execute(sqltext(
                "ALTER TABLE mail_jobs ADD COLUMN extract_zip INTEGER DEFAULT 0"
            ))


def resolve_download_dir(job: dict) -> str:
    """Return the download directory for a job.

    If download_folder is a full path (e.g. C:/Users/you/Documents/Reports)
    it is used directly; otherwise it is treated as a Desktop subfolder name.
    """
    folder = job.get("download_folder") or job.get("folder")
    if not folder:
        jid = job.get("job_id") or job.get("id") or "downloads"
        folder = jid.replace("_", "-").title()
    # Absolute Windows path (C:\... or C:/...) — use as-is on any OS
    if len(folder) >= 3 and folder[1] == ":" and folder[2] in ("/", "\\"):
        return folder
    desktop = getattr(config, "DESKTOP_DIR", os.path.dirname(config.DOWNLOAD_DIR))
    return os.path.join(desktop, folder)


def _interval_minutes(row):
    mins = getattr(row, "interval_minutes", None)
    if mins:
        return max(1, int(mins))
    hours = getattr(row, "interval_hours", None) or 2
    return max(1, int(hours) * 60)


def _normalize_job_id(raw: str) -> str:
    """Turn user input into a safe slug (lowercase, underscores)."""
    s = raw.strip().lower()
    s = re.sub(r"[^a-z0-9_]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def _slug_ok(job_id: str) -> bool:
    return bool(job_id) and bool(re.match(r"^[a-z][a-z0-9_]*$", job_id))


def _row_to_dict(row):
    return {
        "id": row.id,
        "job_id": row.job_id,
        "name": row.name,
        "mailbox": row.mailbox,
        "subject_pattern": row.subject_pattern,
        "target_table": row.target_table,
        "download_folder": getattr(row, "download_folder", None) or "",
        "download_dir": resolve_download_dir({
            "job_id": row.job_id,
            "download_folder": getattr(row, "download_folder", None),
        }),
        "ingest_mode": getattr(row, "ingest_mode", None) or "replace",
        "extract_zip": bool(getattr(row, "extract_zip", 0)),
        "interval_minutes": _interval_minutes(row),
        "enabled": bool(row.enabled),
        "last_run": row.last_run,
        "next_run": row.next_run,
        "last_status": row.last_status,
        "last_message": row.last_message,
        "created_at": row.created_at,
    }


def _filter_dict(job):
    return {
        "id": job["job_id"],
        "mailbox": job["mailbox"],
        "subject": job["subject_pattern"],
        "table": job["target_table"],
        "ingest_mode": job.get("ingest_mode", "replace"),
        "extract_zip": job.get("extract_zip", False),
        "download_dir": job.get("download_dir") or resolve_download_dir(job),
        "download_folder": job.get("download_folder", ""),
    }


def seed_from_config():
    """Import config.MAIL_FILTERS into mail_jobs if the table is empty."""
    _ensure_tables()
    with engine.connect() as conn:
        count = conn.execute(select(mail_jobs.c.id).limit(1)).first()
        if count:
            return

    now = datetime.now()
    minutes = getattr(config, "DEFAULT_JOB_INTERVAL_MINUTES", None)
    if not minutes:
        minutes = getattr(config, "DEFAULT_JOB_INTERVAL_HOURS", 2) * 60
    minutes = max(1, int(minutes))
    for f in config.MAIL_FILTERS:
        folder = f.get("folder", f["id"].replace("_", "-").title())
        with engine.begin() as conn:
            conn.execute(mail_jobs.insert().values(
                job_id=f["id"],
                name=f["id"].replace("_", " ").title(),
                mailbox=f["mailbox"],
                subject_pattern=f["subject"],
                target_table=f["table"],
                download_folder=folder,
                ingest_mode="replace",
                extract_zip=1 if f.get("extract_zip") else 0,
                interval_minutes=minutes,
                interval_hours=max(1, minutes // 60),
                enabled=1,
                next_run=now + timedelta(minutes=minutes),
                created_at=now,
            ))


def list_jobs():
    _ensure_tables()
    with engine.connect() as conn:
        rows = conn.execute(
            select(mail_jobs).order_by(mail_jobs.c.job_id)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_job(job_id: str):
    _ensure_tables()
    with engine.connect() as conn:
        row = conn.execute(
            select(mail_jobs).where(mail_jobs.c.job_id == job_id)
        ).first()
    return _row_to_dict(row) if row else None


def get_due_jobs(now=None):
    now = now or datetime.now()
    _ensure_tables()
    with engine.connect() as conn:
        rows = conn.execute(
            select(mail_jobs)
            .where(mail_jobs.c.enabled == 1)
            .where(mail_jobs.c.next_run <= now)
            .order_by(mail_jobs.c.next_run)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def add_job(job_id, name, mailbox, subject_pattern, target_table,
            interval_minutes=120, enabled=True, ingest_mode="replace",
            download_folder=None, extract_zip=False):
    job_id = _normalize_job_id(job_id)
    if not _slug_ok(job_id):
        raise ValueError(
            "Job ID must start with a letter and use only letters, numbers, "
            "underscores (e.g. order_extract)."
        )
    target_table = _normalize_job_id(target_table) if target_table else ""
    if not download_folder:
        download_folder = job_id.replace("_", "-").title()
    _ensure_tables()
    now = datetime.now()
    minutes = max(1, int(interval_minutes))
    try:
        with engine.begin() as conn:
            conn.execute(mail_jobs.insert().values(
                job_id=job_id,
                name=name,
                mailbox=mailbox,
                subject_pattern=subject_pattern,
                target_table=target_table,
                download_folder=download_folder,
                ingest_mode=ingest_mode if ingest_mode in ("replace", "append") else "replace",
                extract_zip=1 if extract_zip else 0,
                interval_minutes=minutes,
                interval_hours=max(1, minutes // 60),
                enabled=1 if enabled else 0,
                next_run=now + timedelta(minutes=minutes),
                created_at=now,
            ))
    except Exception as e:
        if "UNIQUE" in str(e).upper() or "unique" in str(e):
            raise ValueError(f"Job ID '{job_id}' already exists — pick a different ID.") from e
        raise


def update_job(job_id, **fields):
    allowed = {
        "name", "mailbox", "subject_pattern", "target_table",
        "interval_minutes", "enabled", "ingest_mode", "download_folder", "extract_zip",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return
    if "interval_minutes" in updates:
        minutes = max(1, int(updates["interval_minutes"]))
        updates["interval_minutes"] = minutes
        updates["interval_hours"] = max(1, minutes // 60)
    if "enabled" in updates:
        updates["enabled"] = 1 if updates["enabled"] else 0
    if "extract_zip" in updates:
        updates["extract_zip"] = 1 if updates["extract_zip"] else 0
    if "ingest_mode" in updates and updates["ingest_mode"] not in ("replace", "append"):
        updates["ingest_mode"] = "replace"
    _ensure_tables()
    with engine.begin() as conn:
        conn.execute(
            update(mail_jobs).where(mail_jobs.c.job_id == job_id).values(**updates)
        )
    if "interval_minutes" in updates:
        job = get_job(job_id)
        if job:
            schedule_next(job_id, from_time=datetime.now())


def delete_job(job_id: str):
    _ensure_tables()
    with engine.begin() as conn:
        conn.execute(mail_jobs.delete().where(mail_jobs.c.job_id == job_id))


def schedule_next(job_id: str, from_time=None, interval_minutes=None):
    """Set next_run without running the job (used after create / edit)."""
    job = get_job(job_id)
    if not job:
        return
    base = from_time or datetime.now()
    minutes = interval_minutes if interval_minutes is not None else job["interval_minutes"]
    minutes = max(1, int(minutes))
    nxt = base + timedelta(minutes=minutes)
    _ensure_tables()
    with engine.begin() as conn:
        conn.execute(
            update(mail_jobs)
            .where(mail_jobs.c.job_id == job_id)
            .values(next_run=nxt)
        )


def mark_job_finished(job_id: str, status: str, message: str = ""):
    now = datetime.now()
    job = get_job(job_id)
    if not job:
        return
    minutes = max(1, int(job["interval_minutes"]))
    nxt = now + timedelta(minutes=minutes)
    _ensure_tables()
    with engine.begin() as conn:
        conn.execute(
            update(mail_jobs)
            .where(mail_jobs.c.job_id == job_id)
            .values(
                last_run=now,
                next_run=nxt,
                last_status=status,
                last_message=(message or "")[:2000],
            )
        )


def job_as_filter(job):
    return _filter_dict(job)
