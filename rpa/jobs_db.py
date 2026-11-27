"""
RPA tool definitions — managed from the dashboard, triggered after mail jobs
or on an optional daily clock schedule.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, delete, select, update

import config
from db import engine, init_db


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
    # Daily clock schedule (optional) — independent of mail triggers.
    Column("schedule_enabled", Integer, default=0),
    Column("schedule_time", String(8)),  # "HH:MM" 24h local
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
        if "schedule_enabled" not in cols:
            conn.execute(
                sqltext("ALTER TABLE rpa_jobs ADD COLUMN schedule_enabled INTEGER DEFAULT 0")
            )
        if "schedule_time" not in cols:
            conn.execute(sqltext("ALTER TABLE rpa_jobs ADD COLUMN schedule_time VARCHAR(8)"))
        if "next_run" not in cols:
            conn.execute(sqltext("ALTER TABLE rpa_jobs ADD COLUMN next_run DATETIME"))
        conn.execute(
            sqltext(
                "UPDATE rpa_jobs SET start_url = :url "
                "WHERE rpa_id = 'nerp_upload_pi' AND (start_url IS NULL OR start_url = '')"
            ),
            {"url": _nerp_start_url()},
        )


def normalize_schedule_time(raw: str) -> str:
    """Accept '8:00', '08:00', '8:00 AM' → 'HH:MM' 24h. Empty → ''."""
    text = (raw or "").strip()
    if not text:
        return ""
    m = re.match(r"^(\d{1,2}):(\d{2})\s*([AaPp][Mm])?$", text)
    if not m:
        raise ValueError("Schedule time must look like 08:00 or 8:30 AM")
    hh, mm = int(m.group(1)), int(m.group(2))
    ampm = (m.group(3) or "").upper()
    if ampm == "PM" and hh < 12:
        hh += 12
    elif ampm == "AM" and hh == 12:
        hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("Schedule time out of range")
    return f"{hh:02d}:{mm:02d}"


def next_daily_run(from_time: datetime | None, schedule_time: str) -> datetime | None:
    """Next local datetime at schedule_time (HH:MM), strictly after from_time."""
    hhmm = normalize_schedule_time(schedule_time)
    if not hhmm:
        return None
    hh, mm = map(int, hhmm.split(":"))
    base = from_time or datetime.now()
    candidate = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= base:
        candidate += timedelta(days=1)
    return candidate


def _normalize_rpa_id(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", (raw or "").strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    if not slug:
        raise ValueError("RPA id is required (letters, numbers, underscores).")
    if slug in _BUILTIN_IDS:
        raise ValueError(f"RPA id '{slug}' is reserved.")
    return slug


def _row_to_dict(row):
    schedule_enabled = bool(getattr(row, "schedule_enabled", 0) or 0)
    schedule_time = (getattr(row, "schedule_time", None) or "") or ""
    next_run = getattr(row, "next_run", None)
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
        "schedule_enabled": schedule_enabled,
        "schedule_time": schedule_time,
        "next_run": next_run,
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
    # BI / d2cbi views — same traffic_raw script; only start_url differs.
    _bi_view_jobs = (
        (
            "traffic_sales_raw",
            "BI Sales Raw",
            "https://d2cbi.sec.samsung.net/#/views/2-4_SalesRaw/2-4-1_SalesRaw?:iid=1&:original_view=y",
        ),
        (
            "traffic_raw",
            "BI Traffic Raw ALLDIV",
            "https://d2cbi.sec.samsung.net/#/views/03_ALLDIV/03-7_TrafficRaw",
        ),
        (
            "traffic_mx",
            "BI Traffic Raw MX",
            "https://d2cbi.sec.samsung.net/#/views/04_MX/04-7_TrafficRaw",
        ),
        (
            "traffic_vd",
            "BI Traffic Raw VD",
            "https://d2cbi.sec.samsung.net/#/views/05_VD/05-7_TrafficRaw?:iid=1",
        ),
        (
            "traffic_da",
            "BI Traffic Raw DA",
            "https://d2cbi.sec.samsung.net/#/views/06_DA/06-7_TrafficRaw?:iid=2",
        ),
    )
    for rid, name, url in _bi_view_jobs:
        if rid in existing:
            continue
        to_insert.append(dict(
            rpa_id=rid,
            name=name,
            tool="codegen",
            start_url=url,
            description=(
                "Same BI Crosstab CSV flow as traffic_raw (reuses that script). "
                "Tableau login + MFA from Settings."
            ),
            trigger_mail_job="",
            enabled=1,
            created_at=now,
        ))

    if to_insert:
        with engine.begin() as conn:
            for row in to_insert:
                conn.execute(rpa_jobs.insert().values(**row))

    # Keep start_urls / names in sync when links change; drop old workbook clones.
    _sync_bi_view_jobs(_bi_view_jobs)


def _sync_bi_view_jobs(jobs: tuple) -> None:
    """Upsert BI view start_urls and delete obsolete traffic_wb_* workbook jobs."""
    _ensure_tables()
    removed_ids: list[str] = []
    with engine.begin() as conn:
        for rid, name, url in jobs:
            conn.execute(
                update(rpa_jobs)
                .where(rpa_jobs.c.rpa_id == rid)
                .values(
                    name=name,
                    start_url=url,
                    tool="codegen",
                    enabled=1,
                    description=(
                        "Same BI Crosstab CSV flow as traffic_raw (reuses that script). "
                        "Tableau login + MFA from Settings."
                    ),
                )
            )
        # Old workbook-number clones (traffic_wb_15, …) used wrong #/workbooks/N links.
        old_rows = conn.execute(
            select(rpa_jobs.c.rpa_id).where(rpa_jobs.c.rpa_id.like("traffic_wb_%"))
        ).fetchall()
        removed_ids = [r[0] for r in old_rows]
        if removed_ids:
            conn.execute(
                delete(rpa_jobs).where(rpa_jobs.c.rpa_id.like("traffic_wb_%"))
            )
    if removed_ids:
        try:
            from rpa.codegen import script_path
        except Exception:
            script_path = None
        for rid in removed_ids:
            if not script_path:
                break
            try:
                path = script_path(rid)
                if os.path.isfile(path):
                    os.remove(path)
            except Exception:
                pass
        print(
            f"[RPA] Removed obsolete workbook jobs: {', '.join(removed_ids)}",
            flush=True,
        )


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
        "schedule_enabled", "schedule_time", "next_run",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    if "enabled" in updates:
        updates["enabled"] = 1 if updates["enabled"] else 0
    if "schedule_enabled" in updates:
        updates["schedule_enabled"] = 1 if updates["schedule_enabled"] else 0
    if "trigger_mail_job" in updates and updates["trigger_mail_job"] is None:
        updates["trigger_mail_job"] = ""
    if "start_url" in updates and updates["start_url"] is not None:
        updates["start_url"] = updates["start_url"].strip()
    if "schedule_time" in updates:
        updates["schedule_time"] = normalize_schedule_time(updates["schedule_time"] or "")

    # Recompute next_run when schedule toggled/time changed (unless caller set next_run).
    if "next_run" not in updates and (
        "schedule_enabled" in updates or "schedule_time" in updates
    ):
        current = get_rpa_job(rpa_id) or {}
        sched_on = updates.get(
            "schedule_enabled",
            current.get("schedule_enabled"),
        )
        sched_on = bool(sched_on)
        hhmm = updates.get("schedule_time", current.get("schedule_time") or "")
        if sched_on and hhmm:
            updates["next_run"] = next_daily_run(datetime.now(), hhmm)
        else:
            updates["next_run"] = None

    _ensure_tables()
    with engine.begin() as conn:
        conn.execute(
            update(rpa_jobs).where(rpa_jobs.c.rpa_id == rpa_id).values(**updates)
        )


def get_due_scheduled_rpas(now: datetime | None = None) -> list[dict]:
    """Enabled RPAs whose daily schedule next_run is due."""
    _ensure_tables()
    now = now or datetime.now()
    with engine.connect() as conn:
        rows = conn.execute(
            select(rpa_jobs)
            .where(rpa_jobs.c.enabled == 1)
            .where(rpa_jobs.c.schedule_enabled == 1)
            .where(rpa_jobs.c.next_run.isnot(None))
            .where(rpa_jobs.c.next_run <= now)
            .order_by(rpa_jobs.c.next_run.asc())
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def claim_scheduled_rpa(rpa_id: str, now: datetime | None = None) -> bool:
    """Advance next_run to tomorrow so the tick cannot double-fire. Returns True if claimed."""
    _ensure_tables()
    now = now or datetime.now()
    job = get_rpa_job(rpa_id)
    if not job or not job.get("schedule_enabled") or not job.get("schedule_time"):
        return False
    nxt = next_daily_run(now, job["schedule_time"])
    with engine.begin() as conn:
        result = conn.execute(
            update(rpa_jobs)
            .where(rpa_jobs.c.rpa_id == rpa_id)
            .where(rpa_jobs.c.schedule_enabled == 1)
            .where(rpa_jobs.c.next_run.isnot(None))
            .where(rpa_jobs.c.next_run <= now)
            .values(next_run=nxt)
        )
        return bool(result.rowcount)


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
