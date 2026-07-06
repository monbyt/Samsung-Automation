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
    Column("ingest_mode", String(20), default="replace"),
    Column("interval_hours", Integer, default=2),
    Column("interval_minutes", Integer),  # legacy — migrated to interval_hours
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
            if "interval_minutes" in cols:
                conn.execute(sqltext(
                    "UPDATE mail_jobs SET interval_hours = MAX(1, interval_minutes / 60) "
                    "WHERE interval_hours IS NULL OR interval_hours = 0"
                ))


def _interval_hours(row):
    if getattr(row, "interval_hours", None):
        return max(1, int(row.interval_hours))
    mins = getattr(row, "interval_minutes", None) or 60
    return max(1, int(mins) // 60)


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
        "ingest_mode": getattr(row, "ingest_mode", None) or "replace",
        "interval_hours": _interval_hours(row),
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
    }


def seed_from_config():
    """Import config.MAIL_FILTERS into mail_jobs if the table is empty."""
    _ensure_tables()
    with engine.connect() as conn:
        count = conn.execute(select(mail_jobs.c.id).limit(1)).first()
        if count:
            return

    now = datetime.now()
    hours = getattr(config, "DEFAULT_JOB_INTERVAL_HOURS", 2)
    for f in config.MAIL_FILTERS:
        with engine.begin() as conn:
            conn.execute(mail_jobs.insert().values(
                job_id=f["id"],
                name=f["id"].replace("_", " ").title(),
                mailbox=f["mailbox"],
                subject_pattern=f["subject"],
                target_table=f["table"],
                ingest_mode="replace",
                interval_hours=hours,
                enabled=1,
                next_run=now + timedelta(hours=hours),
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
            interval_hours=2, enabled=True, ingest_mode="replace"):
    if not _slug_ok(job_id):
        raise ValueError("Job ID must be lowercase letters, numbers, underscores (e.g. order_extract).")
    _ensure_tables()
    now = datetime.now()
    hours = max(1, int(interval_hours))
    with engine.begin() as conn:
        conn.execute(mail_jobs.insert().values(
            job_id=job_id,
            name=name,
            mailbox=mailbox,
            subject_pattern=subject_pattern,
            target_table=target_table,
            ingest_mode=ingest_mode if ingest_mode in ("replace", "append") else "replace",
            interval_hours=hours,
            enabled=1 if enabled else 0,
            next_run=now + timedelta(hours=hours),
            created_at=now,
        ))


def update_job(job_id, **fields):
    allowed = {
        "name", "mailbox", "subject_pattern", "target_table",
        "interval_hours", "enabled", "ingest_mode",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return
    if "interval_hours" in updates:
        updates["interval_hours"] = max(1, int(updates["interval_hours"]))
    if "enabled" in updates:
        updates["enabled"] = 1 if updates["enabled"] else 0
    if "ingest_mode" in updates and updates["ingest_mode"] not in ("replace", "append"):
        updates["ingest_mode"] = "replace"
    _ensure_tables()
    with engine.begin() as conn:
        conn.execute(
            update(mail_jobs).where(mail_jobs.c.job_id == job_id).values(**updates)
        )
    if "interval_hours" in updates:
        job = get_job(job_id)
        if job:
            schedule_next(job_id, from_time=datetime.now())


def delete_job(job_id: str):
    _ensure_tables()
    with engine.begin() as conn:
        conn.execute(mail_jobs.delete().where(mail_jobs.c.job_id == job_id))


def schedule_next(job_id: str, from_time=None, interval_hours=None):
    """Set next_run without running the job (used after create / edit)."""
    job = get_job(job_id)
    if not job:
        return
    base = from_time or datetime.now()
    hours = interval_hours if interval_hours is not None else job["interval_hours"]
    hours = max(1, int(hours))
    nxt = base + timedelta(hours=hours)
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
    hours = max(1, int(job["interval_hours"]))
    nxt = now + timedelta(hours=hours)
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
