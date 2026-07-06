"""
Mail job definitions stored in SQL — managed from the dashboard.
"""
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
    Column("interval_minutes", Integer, default=60),
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


def _slug_ok(job_id: str) -> bool:
    return bool(re.match(r"^[a-z][a-z0-9_]{1,62}$", job_id))


def _row_to_dict(row):
    return {
        "id": row.id,
        "job_id": row.job_id,
        "name": row.name,
        "mailbox": row.mailbox,
        "subject_pattern": row.subject_pattern,
        "target_table": row.target_table,
        "interval_minutes": row.interval_minutes,
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
    }


def seed_from_config():
    """Import config.MAIL_FILTERS into mail_jobs if the table is empty."""
    _ensure_tables()
    with engine.connect() as conn:
        count = conn.execute(select(mail_jobs.c.id).limit(1)).first()
        if count:
            return

    now = datetime.now()
    for f in config.MAIL_FILTERS:
        interval = getattr(config, "DEFAULT_JOB_INTERVAL_MINUTES", 60)
        with engine.begin() as conn:
            conn.execute(mail_jobs.insert().values(
                job_id=f["id"],
                name=f["id"].replace("_", " ").title(),
                mailbox=f["mailbox"],
                subject_pattern=f["subject"],
                target_table=f["table"],
                interval_minutes=interval,
                enabled=1,
                next_run=now + timedelta(minutes=interval),
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
            interval_minutes=60, enabled=True):
    if not _slug_ok(job_id):
        raise ValueError("Job ID must be lowercase letters, numbers, underscores (e.g. order_extract).")
    _ensure_tables()
    now = datetime.now()
    interval = max(1, int(interval_minutes))
    with engine.begin() as conn:
        conn.execute(mail_jobs.insert().values(
            job_id=job_id,
            name=name,
            mailbox=mailbox,
            subject_pattern=subject_pattern,
            target_table=target_table,
            interval_minutes=interval,
            enabled=1 if enabled else 0,
            next_run=now + timedelta(minutes=interval),
            created_at=now,
        ))


def update_job(job_id, **fields):
    allowed = {
        "name", "mailbox", "subject_pattern", "target_table",
        "interval_minutes", "enabled",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return
    if "interval_minutes" in updates:
        updates["interval_minutes"] = max(1, int(updates["interval_minutes"]))
    if "enabled" in updates:
        updates["enabled"] = 1 if updates["enabled"] else 0
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
    interval = interval_minutes or job["interval_minutes"]
    nxt = base + timedelta(minutes=interval)
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
    nxt = now + timedelta(minutes=job["interval_minutes"])
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
