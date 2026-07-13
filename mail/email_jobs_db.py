"""
Email send-job definitions — one per RPA that should auto-send.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column, DateTime, Integer, MetaData, String, Table, Text, select, update,
)

from db import engine, init_db

metadata = MetaData()

email_jobs = Table(
    "email_jobs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("rpa_id", String(64), unique=True, nullable=False),
    Column("to_emails", Text, nullable=False),
    Column("cc_emails", Text),
    Column("subject", String(500)),
    Column("body", Text),
    Column("attach_folder", String(500)),
    Column("attach_count", Integer, default=1),
    Column("enabled", Integer, default=1),
    Column("last_sent_at", DateTime),
    Column("last_status", String(20)),
    Column("last_message", Text),
    Column("created_at", DateTime),
)


def _ensure_table():
    init_db()
    metadata.create_all(engine)


def _row_to_dict(row) -> dict:
    return {
        "id": row.id,
        "rpa_id": row.rpa_id,
        "to_emails": row.to_emails or "",
        "cc_emails": row.cc_emails or "",
        "subject": row.subject or "",
        "body": row.body or "",
        "attach_folder": row.attach_folder or "",
        "attach_count": row.attach_count or 1,
        "enabled": bool(row.enabled),
        "last_sent_at": row.last_sent_at,
        "last_status": row.last_status or "",
        "last_message": row.last_message or "",
        "created_at": row.created_at,
    }


def list_email_jobs() -> list[dict]:
    _ensure_table()
    with engine.connect() as conn:
        rows = conn.execute(select(email_jobs).order_by(email_jobs.c.rpa_id)).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_email_job_for_rpa(rpa_id: str) -> Optional[dict]:
    if not rpa_id:
        return None
    _ensure_table()
    with engine.connect() as conn:
        row = conn.execute(
            select(email_jobs).where(email_jobs.c.rpa_id == rpa_id)
        ).first()
    return _row_to_dict(row) if row else None


def upsert_email_job(
    rpa_id: str,
    to_emails: str,
    cc_emails: str = "",
    subject: str = "",
    body: str = "",
    attach_folder: str = "",
    attach_count: int = 1,
    enabled: bool = True,
):
    if not rpa_id:
        raise ValueError("rpa_id is required")
    if not to_emails or not to_emails.strip():
        raise ValueError("At least one recipient email is required.")

    _ensure_table()
    values = dict(
        to_emails=to_emails.strip(),
        cc_emails=(cc_emails or "").strip(),
        subject=(subject or "").strip(),
        body=body or "",
        attach_folder=(attach_folder or "").strip(),
        attach_count=max(1, int(attach_count or 1)),
        enabled=1 if enabled else 0,
    )
    with engine.begin() as conn:
        existing = conn.execute(
            select(email_jobs.c.id).where(email_jobs.c.rpa_id == rpa_id)
        ).first()
        if existing:
            conn.execute(
                update(email_jobs).where(email_jobs.c.rpa_id == rpa_id).values(**values)
            )
        else:
            values.update(rpa_id=rpa_id, created_at=datetime.now())
            conn.execute(email_jobs.insert().values(**values))


def delete_email_job(rpa_id: str):
    _ensure_table()
    with engine.begin() as conn:
        conn.execute(email_jobs.delete().where(email_jobs.c.rpa_id == rpa_id))


def mark_send_finished(rpa_id: str, status: str, message: str = ""):
    _ensure_table()
    with engine.begin() as conn:
        conn.execute(
            update(email_jobs)
            .where(email_jobs.c.rpa_id == rpa_id)
            .values(
                last_sent_at=datetime.now(),
                last_status=status,
                last_message=(message or "")[:2000],
            )
        )
