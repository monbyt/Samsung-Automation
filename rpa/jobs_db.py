"""
RPA tool definitions — managed from the dashboard, triggered after mail jobs.
"""
import os
import re
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, delete, select, update

import config
from db import engine, init_db

metadata = MetaData()

rpa_jobs = Table(
    "rpa_jobs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("rpa_id", String(64), unique=True, nullable=False),
    Column("name", String(200), nullable=False),
    Column("tool", String(50), nullable=False),
    Column("start_url", Text),
    Column("upload_folder", String(500)),
    Column("download_folder", String(500)),
    Column("description", Text),
    Column("trigger_mail_job", String(64)),
    Column("enabled", Integer, default=1),
    Column("last_run", DateTime),
    Column("last_status", String(20)),
    Column("last_message", Text),
    Column("created_at", DateTime),
    Column("next_rpa", String(64)),
)

_BUILTIN_IDS = frozenset({"nerp_upload_pi"})


def _ensure_tables():
    init_db()
    metadata.create_all(engine)
    _migrate_rpa_columns()


def _migrate_rpa_columns():
    if not config.DB_URL.startswith("sqlite"):
        return
    from sqlalchemy import text as sqltext
    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(sqltext("PRAGMA table_info(rpa_jobs)"))}
        if "start_url" not in cols:
            conn.execute(sqltext("ALTER TABLE rpa_jobs ADD COLUMN start_url TEXT"))
        if "upload_folder" not in cols:
            conn.execute(sqltext("ALTER TABLE rpa_jobs ADD COLUMN upload_folder VARCHAR(500)"))
        if "download_folder" not in cols:
            conn.execute(sqltext("ALTER TABLE rpa_jobs ADD COLUMN download_folder VARCHAR(500)"))
        if "next_rpa" not in cols:
            conn.execute(sqltext("ALTER TABLE rpa_jobs ADD COLUMN next_rpa VARCHAR(64)"))
        conn.execute(
            sqltext(
                "UPDATE rpa_jobs SET start_url = :url "
                "WHERE rpa_id = 'nerp_upload_pi' AND (start_url IS NULL OR start_url = '')"
            ),
            {"url": config.NERP_URL},
        )


def _normalize_rpa_id(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", (raw or "").strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    if not slug:
        raise ValueError("RPA id is required (letters, numbers, underscores).")
    if slug in _BUILTIN_IDS:
        raise ValueError(f"RPA id '{slug}' is reserved.")
    return slug


def _row_to_dict(row):
    return {
        "id": row.id,
        "rpa_id": row.rpa_id,
        "name": row.name,
        "tool": row.tool,
        "start_url": row.start_url or "",
        "upload_folder": row.upload_folder or "",
        "download_folder": row.download_folder or "",
        "description": row.description or "",
        "trigger_mail_job": row.trigger_mail_job or "",
        "next_rpa": row.next_rpa or "",
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
            start_url=config.NERP_URL,
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


def add_rpa_job(
    rpa_id: str,
    name: str,
    start_url: str,
    *,
    description: str = "",
    trigger_mail_job: str = "",
    next_rpa: str = "",
    enabled: bool = False,
) -> str:
    slug = _normalize_rpa_id(rpa_id)
    if get_rpa_job(slug):
        raise ValueError(f"RPA id '{slug}' already exists.")
    if not name.strip():
        raise ValueError("Display name is required.")
    if not start_url.strip():
        raise ValueError("Start URL is required for recording.")

    now = datetime.now()
    _ensure_tables()
    with engine.begin() as conn:
        conn.execute(rpa_jobs.insert().values(
            rpa_id=slug,
            name=name.strip(),
            tool="codegen",
            start_url=start_url.strip(),
            description=description.strip(),
            trigger_mail_job=(trigger_mail_job or "").strip(),
            next_rpa=(next_rpa or "").strip(),
            enabled=1 if enabled else 0,
            created_at=now,
        ))
    return slug


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
    allowed = {
        "name", "description", "trigger_mail_job", "enabled", "start_url",
        "upload_folder", "download_folder", "next_rpa",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    if "enabled" in updates:
        updates["enabled"] = 1 if updates["enabled"] else 0
    if "trigger_mail_job" in updates and updates["trigger_mail_job"] is None:
        updates["trigger_mail_job"] = ""
    if "start_url" in updates and updates["start_url"] is not None:
        updates["start_url"] = updates["start_url"].strip()
    _ensure_tables()
    with engine.begin() as conn:
        conn.execute(
            update(rpa_jobs).where(rpa_jobs.c.rpa_id == rpa_id).values(**updates)
        )


def delete_rpa_job(rpa_id: str):
    if rpa_id in _BUILTIN_IDS:
        raise ValueError("Cannot delete built-in RPA tools.")
    _ensure_tables()
    with engine.begin() as conn:
        conn.execute(rpa_jobs.delete().where(rpa_jobs.c.rpa_id == rpa_id))
    from rpa.codegen import script_path
    path = script_path(rpa_id)
    if os.path.isfile(path):
        os.remove(path)


def get_next_rpa(rpa_id: str):
    """Return the next_rpa id for the given job, or None if not set."""
    job = get_rpa_job(rpa_id)
    if not job:
        return None
    return job.get("next_rpa") or None


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
