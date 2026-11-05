"""
RPA tool definitions — managed from the dashboard, triggered after mail jobs
or on their own cron schedule (interval_minutes + clock offset).
"""
import os
import re
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, delete, select, update

import config
from db import engine, init_db
from mail.jobs_db import next_clock_run, slot_label, slot_options, _snap_interval_minutes


def _nerp_start_url() -> str:
    try:
        from mail.settings_db import get_nerp_url
        return get_nerp_url()
    except Exception:
        return config.NERP_URL


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
    # Keep cookies/MFA in chrome-profile-rpa/<rpa_id>/ across record + run.
    Column("sticky_chrome", Integer, default=0),
    # Standalone cron (0 = manual / mail-trigger only).
    Column("interval_minutes", Integer, default=0),
    Column("schedule_offset_minutes", Integer, default=0),
    Column("next_run", DateTime),
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
        if "sticky_chrome" not in cols:
            conn.execute(sqltext(
                "ALTER TABLE rpa_jobs ADD COLUMN sticky_chrome INTEGER DEFAULT 0"
            ))
        if "interval_minutes" not in cols:
            conn.execute(sqltext(
                "ALTER TABLE rpa_jobs ADD COLUMN interval_minutes INTEGER DEFAULT 0"
            ))
        if "schedule_offset_minutes" not in cols:
            conn.execute(sqltext(
                "ALTER TABLE rpa_jobs ADD COLUMN schedule_offset_minutes INTEGER DEFAULT 0"
            ))
        if "next_run" not in cols:
            conn.execute(sqltext("ALTER TABLE rpa_jobs ADD COLUMN next_run DATETIME"))
        conn.execute(
            sqltext(
                "UPDATE rpa_jobs SET start_url = :url "
                "WHERE rpa_id = 'nerp_upload_pi' AND (start_url IS NULL OR start_url = '')"
            ),
            {"url": _nerp_start_url()},
        )


def _normalize_rpa_id(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", (raw or "").strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    if not slug:
        raise ValueError("RPA id is required (letters, numbers, underscores).")
    if slug in _BUILTIN_IDS:
        raise ValueError(f"RPA id '{slug}' is reserved.")
    return slug


def _interval_minutes(row) -> int:
    try:
        return max(0, int(getattr(row, "interval_minutes", 0) or 0))
    except (TypeError, ValueError):
        return 0


def daily_slot_options():
    """Clock times for a once-per-day schedule (every 30 minutes)."""
    opts = []
    for h in range(24):
        for m in (0, 30):
            off = h * 60 + m
            h12 = h % 12 or 12
            ampm = "AM" if h < 12 else "PM"
            opts.append((off, f"Daily at {h12}:{m:02d} {ampm}"))
    return opts


def schedule_slot_options(interval_minutes: int):
    minutes = int(interval_minutes or 0)
    if minutes <= 0:
        return []
    if minutes >= 1440:
        return daily_slot_options()
    return slot_options(minutes)


def _format_slot_label(interval_minutes: int, offset_minutes: int) -> str:
    interval = int(interval_minutes or 0)
    if interval <= 0:
        return "Manual / mail"
    offset = int(offset_minutes or 0)
    if interval >= 1440:
        off = offset % 1440
        h, m = divmod(off, 60)
        h12 = h % 12 or 12
        ampm = "AM" if h < 12 else "PM"
        return f"Daily {h12}:{m:02d} {ampm}"
    return slot_label(offset, interval)


def _row_to_dict(row):
    interval = _interval_minutes(row)
    offset = int(getattr(row, "schedule_offset_minutes", 0) or 0)
    scheduled = interval > 0
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
        "sticky_chrome": bool(getattr(row, "sticky_chrome", 0)),
        "chrome_profile": (
            config.rpa_sticky_profile_dir(row.rpa_id)
            if getattr(row, "sticky_chrome", 0)
            else ""
        ),
        "interval_minutes": interval,
        "schedule_offset_minutes": offset,
        "next_run": getattr(row, "next_run", None),
        "scheduled": scheduled,
        "slot_label": _format_slot_label(interval, offset),
        "enabled": bool(row.enabled),
        "last_run": row.last_run,
        "last_status": row.last_status,
        "last_message": row.last_message,
        "created_at": row.created_at,
    }


def seed_from_config():
    """Seed built-in RPA tools and known codegen scripts if missing."""
    _ensure_tables()
    now = datetime.now()

    with engine.connect() as conn:
        existing = {
            row.rpa_id
            for row in conn.execute(select(rpa_jobs.c.rpa_id)).fetchall()
        }

    to_insert = []
    if "nerp_upload_pi" not in existing:
        to_insert.append(dict(
            rpa_id="nerp_upload_pi",
            name="NERP Upload + P/I",
            tool="nerp",
            start_url=_nerp_start_url(),
            description=(
                f"Upload {config.NERP_PROGRAM_UPLOAD} then run "
                f"{config.NERP_PROGRAM_PI} (P/I print). Uses file from mail job or "
                f"{config.NERP_UPLOAD_FILE}."
            ),
            trigger_mail_job="",
            enabled=0,
            created_at=now,
        ))
    if "final_oc" not in existing:
        to_insert.append(dict(
            rpa_id="final_oc",
            name="Final Order Creation",
            tool="codegen",
            start_url=_nerp_start_url(),
            description=(
                "ZLSDF50270 upload → create sales order → ZSDM31520 zpdf print/download."
            ),
            trigger_mail_job="",
            enabled=1,
            created_at=now,
        ))
    if "final_oc_multi" not in existing:
        to_insert.append(dict(
            rpa_id="final_oc_multi",
            name="Final Order Creation (all SOs)",
            tool="codegen",
            start_url=_nerp_start_url(),
            description=(
                "ZLSDF50270 upload → capture ALL sales orders → ZSDM31520 PDF per SO. "
                "Upload folder = Excel; Download folder = PDFs only (for email)."
            ),
            trigger_mail_job="",
            enabled=1,
            created_at=now,
        ))

    if not to_insert:
        return
    with engine.begin() as conn:
        for row in to_insert:
            conn.execute(rpa_jobs.insert().values(**row))


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
    sticky_chrome: bool = False,
    interval_minutes: int = 0,
    schedule_offset_minutes: int = 0,
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
    interval = max(0, int(interval_minutes or 0))
    if interval > 0:
        interval = _snap_interval_minutes(interval)
    offset = max(0, int(schedule_offset_minutes or 0))
    nxt = next_clock_run(now, interval, offset) if interval > 0 else None
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
            sticky_chrome=1 if sticky_chrome else 0,
            interval_minutes=interval,
            schedule_offset_minutes=offset,
            next_run=nxt,
            enabled=1 if enabled else 0,
            created_at=now,
        ))
    if sticky_chrome:
        os.makedirs(config.rpa_sticky_profile_dir(slug), exist_ok=True)
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
        "upload_folder", "download_folder", "next_rpa", "sticky_chrome",
        "interval_minutes", "schedule_offset_minutes", "next_run",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    if "enabled" in updates:
        updates["enabled"] = 1 if updates["enabled"] else 0
    if "sticky_chrome" in updates:
        updates["sticky_chrome"] = 1 if updates["sticky_chrome"] else 0
    if "trigger_mail_job" in updates and updates["trigger_mail_job"] is None:
        updates["trigger_mail_job"] = ""
    if "start_url" in updates and updates["start_url"] is not None:
        updates["start_url"] = updates["start_url"].strip()
    if "interval_minutes" in updates:
        interval = max(0, int(updates["interval_minutes"] or 0))
        if interval > 0:
            interval = _snap_interval_minutes(interval)
        updates["interval_minutes"] = interval
    if "schedule_offset_minutes" in updates:
        updates["schedule_offset_minutes"] = max(
            0, int(updates["schedule_offset_minutes"] or 0)
        )
    # Recompute next_run when schedule changes (unless caller set next_run explicitly).
    if "next_run" not in updates and (
        "interval_minutes" in updates or "schedule_offset_minutes" in updates
    ):
        job = get_rpa_job(rpa_id) or {}
        interval = updates.get("interval_minutes", job.get("interval_minutes") or 0)
        offset = updates.get(
            "schedule_offset_minutes", job.get("schedule_offset_minutes") or 0
        )
        if int(interval or 0) > 0:
            updates["next_run"] = next_clock_run(datetime.now(), int(interval), int(offset))
        else:
            updates["next_run"] = None
    _ensure_tables()
    with engine.begin() as conn:
        conn.execute(
            update(rpa_jobs).where(rpa_jobs.c.rpa_id == rpa_id).values(**updates)
        )
    if updates.get("sticky_chrome"):
        os.makedirs(config.rpa_sticky_profile_dir(rpa_id), exist_ok=True)


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


def get_due_rpa_jobs(now=None):
    """Enabled RPA jobs whose standalone cron next_run is due."""
    now = now or datetime.now()
    _ensure_tables()
    with engine.connect() as conn:
        rows = conn.execute(
            select(rpa_jobs)
            .where(rpa_jobs.c.enabled == 1)
            .where(rpa_jobs.c.interval_minutes > 0)
            .where(rpa_jobs.c.next_run.isnot(None))
            .where(rpa_jobs.c.next_run <= now)
            .order_by(rpa_jobs.c.next_run)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def mark_rpa_finished(rpa_id: str, status: str, message: str = ""):
    now = datetime.now()
    job = get_rpa_job(rpa_id) or {}
    values = {
        "last_run": now,
        "last_status": status,
        "last_message": (message or "")[:2000],
    }
    interval = int(job.get("interval_minutes") or 0)
    if interval > 0:
        values["next_run"] = next_clock_run(
            now, interval, job.get("schedule_offset_minutes") or 0
        )
    _ensure_tables()
    with engine.begin() as conn:
        conn.execute(
            update(rpa_jobs)
            .where(rpa_jobs.c.rpa_id == rpa_id)
            .values(**values)
        )
