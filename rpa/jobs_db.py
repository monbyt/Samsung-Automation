"""
RPA tool definitions — managed from the dashboard, triggered after mail jobs.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, select, update

import config
from db import engine, init_db

metadata = MetaData()

rpa_jobs = Table(
    "rpa_jobs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("rpa_id", String(64), unique=True, nullable=False),
    Column("name", String(200), nullable=False),
    Column("tool", String(50), nullable=False),
    Column("description", Text),
    Column("trigger_mail_job", String(64)),
    Column("enabled", Integer, default=1),
    Column("last_run", DateTime),
    Column("last_status", String(20)),
    Column("last_message", Text),
    Column("created_at", DateTime),
)


def _ensure_tables():
    init_db()
    metadata.create_all(engine)


def _row_to_dict(row):
    return {
        "id": row.id,
        "rpa_id": row.rpa_id,
        "name": row.name,
        "tool": row.tool,
        "description": row.description or "",
        "trigger_mail_job": row.trigger_mail_job or "",
        "enabled": bool(row.enabled),
        "last_run": row.last_run,
        "last_status": row.last_status,
        "last_message": row.last_message,
        "created_at": row.created_at,
    }


def seed_from_config():
    """Seed built-in RPA tools (NERP) if table is empty."""
    _ensure_tables()
    with engine.connect() as conn:
        if conn.execute(select(rpa_jobs.c.id).limit(1)).first():
            return

    now = datetime.now()
    with engine.begin() as conn:
        conn.execute(rpa_jobs.insert().values(
            rpa_id="nerp_upload_pi",
            name="NERP Upload + P/I",
            tool="nerp",
            description=(
                f"Upload {config.NERP_PROGRAM_UPLOAD} then run "
                f"{config.NERP_PROGRAM_PI} (P/I print). Uses file from mail job or "
                f"{config.NERP_UPLOAD_FILE}."
            ),
            trigger_mail_job="",
            enabled=0,
            created_at=now,
        ))


def list_rpa_jobs():
    _ensure_tables()
    with engine.connect() as conn:
        rows = conn.execute(select(rpa_jobs).order_by(rpa_jobs.c.rpa_id)).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_rpa_job(rpa_id: str):
    _ensure_tables()
    with engine.connect() as conn:
        row = conn.execute(
            select(rpa_jobs).where(rpa_jobs.c.rpa_id == rpa_id)
        ).first()
    return _row_to_dict(row) if row else None


def list_for_mail_job(mail_job_id: str):
    if not mail_job_id:
        return []
    return [
        j for j in list_rpa_jobs()
        if j["trigger_mail_job"] == mail_job_id and j["enabled"]
    ]


def rpa_by_mail_job():
    """Map mail job_id → list of linked RPA job names (for dashboard column)."""
    mapping = {}
    for rpa in list_rpa_jobs():
        mid = rpa.get("trigger_mail_job")
        if mid:
            mapping.setdefault(mid, []).append(rpa["name"])
    return mapping


def update_rpa_job(rpa_id: str, **fields):
    allowed = {"name", "description", "trigger_mail_job", "enabled"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    if "enabled" in updates:
        updates["enabled"] = 1 if updates["enabled"] else 0
    if "trigger_mail_job" in updates and updates["trigger_mail_job"] is None:
        updates["trigger_mail_job"] = ""
    _ensure_tables()
    with engine.begin() as conn:
        conn.execute(
            update(rpa_jobs).where(rpa_jobs.c.rpa_id == rpa_id).values(**updates)
        )


def mark_rpa_finished(rpa_id: str, status: str, message: str = ""):
    _ensure_tables()
    with engine.begin() as conn:
        conn.execute(
            update(rpa_jobs)
            .where(rpa_jobs.c.rpa_id == rpa_id)
            .values(
                last_run=datetime.now(),
                last_status=status,
                last_message=(message or "")[:2000],
            )
        )
