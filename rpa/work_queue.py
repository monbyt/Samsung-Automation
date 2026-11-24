"""
Durable RPA work items — enqueue, claim/lease, finish, reclaim.

Each (rpa_id × file_hash) is tracked so parallel workers cannot double-upload
the same Excel, and the dashboard can show mistakes / skips / failures.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, select, text

import config
from db import engine

_meta = MetaData()

rpa_work_items = Table(
    "rpa_work_items",
    _meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("work_id", String(64), unique=True, nullable=False),
    Column("rpa_id", String(64), nullable=False),
    Column("mail_job_id", String(64)),
    Column("pipeline_run_id", String(64)),
    Column("source_file", String(500)),
    Column("file_hash", String(64)),
    Column("status", String(32), nullable=False),
    Column("attempt", Integer, default=1),
    Column("claimed_at", DateTime),
    Column("lease_until", DateTime),
    Column("worker_pid", Integer),
    Column("chrome_port", Integer),
    Column("started_at", DateTime),
    Column("finished_at", DateTime),
    Column("message", Text),
    Column("error", Text),
    Column("created_at", DateTime),
)

# Statuses that block a second Chrome run for the same file.
_ACTIVE_OR_DONE = ("queued", "claimed", "running", "succeeded")

_LEASE_STATUSES = ("claimed", "running")


def _lease_minutes() -> int:
    try:
        return max(5, int(getattr(config, "RPA_STUCK_MINUTES", 60) or 60))
    except (TypeError, ValueError):
        return 60


_work_queue_ready = False


def init_work_queue() -> None:
    """Create table + partial unique index (idempotency for in-flight/success).

    Does not call db.init_db() — that would recurse because init_db invokes us.
    """
    global _work_queue_ready
    if _work_queue_ready:
        return
    _meta.create_all(engine)
    if config.DB_URL.startswith("sqlite"):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_rpa_work_active "
                    "ON rpa_work_items(rpa_id, file_hash) "
                    "WHERE status IN ('queued','claimed','running','succeeded') "
                    "AND file_hash IS NOT NULL AND file_hash != ''"
                )
            )
            # Link column on legacy rpa_runs (optional correlation).
            try:
                cols = {
                    row[1]
                    for row in conn.execute(text("PRAGMA table_info(rpa_runs)"))
                }
                if cols and "work_id" not in cols:
                    conn.execute(
                        text("ALTER TABLE rpa_runs ADD COLUMN work_id VARCHAR(64)")
                    )
            except Exception:
                pass
    _work_queue_ready = True


def _ensure() -> None:
    """Ensure base DB + work-queue schema exist."""
    from db import init_db
    init_db()


def hash_upload_file(path: Optional[str]) -> str:
    """SHA-256 of file bytes, or empty string when no file."""
    if not path or not os.path.isfile(path):
        return ""
    from parse_to_db import file_hash

    return file_hash(path)


def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    return dict(row)


def find_blocking_work(rpa_id: str, file_hash: str) -> Optional[dict]:
    """Return an active/succeeded work item for this rpa+hash, if any."""
    if not file_hash:
        return None
    _ensure()
    with engine.connect() as conn:
        row = conn.execute(
            select(rpa_work_items)
            .where(rpa_work_items.c.rpa_id == rpa_id)
            .where(rpa_work_items.c.file_hash == file_hash)
            .where(rpa_work_items.c.status.in_(_ACTIVE_OR_DONE))
            .order_by(rpa_work_items.c.id.desc())
            .limit(1)
        ).fetchone()
    return _row_to_dict(row) if row else None


def enqueue_or_skip(
    rpa_id: str,
    *,
    source_file: Optional[str] = None,
    file_hash: Optional[str] = None,
    mail_job_id: Optional[str] = None,
    pipeline_run_id: Optional[str] = None,
) -> dict:
    """Create a queued work item, or record skipped_duplicate if already done/in-flight.

    Returns dict with at least: work_id, status, skipped (bool), message.
    """
    _ensure()
    path = source_file or ""
    h = file_hash if file_hash is not None else hash_upload_file(path or None)
    basename = os.path.basename(path) if path else ""

    blocking = find_blocking_work(rpa_id, h) if h else None
    now = datetime.now()
    work_id = str(uuid.uuid4())

    if blocking:
        with engine.begin() as conn:
            conn.execute(
                rpa_work_items.insert().values(
                    work_id=work_id,
                    rpa_id=rpa_id,
                    mail_job_id=mail_job_id,
                    pipeline_run_id=pipeline_run_id,
                    source_file=basename or blocking.get("source_file"),
                    file_hash=h,
                    status="skipped_duplicate",
                    attempt=1,
                    started_at=now,
                    finished_at=now,
                    message=(
                        f"Skipped — already {blocking.get('status')} "
                        f"(work {blocking.get('work_id', '')[:8]})"
                    ),
                    error=None,
                    created_at=now,
                )
            )
        return {
            "work_id": work_id,
            "status": "skipped_duplicate",
            "skipped": True,
            "blocking_work_id": blocking.get("work_id"),
            "blocking_status": blocking.get("status"),
            "file_hash": h,
            "source_file": basename,
            "message": (
                f"Duplicate of {blocking.get('status')} work "
                f"{(blocking.get('work_id') or '')[:8]}"
            ),
            "rpa_id": rpa_id,
        }

    try:
        with engine.begin() as conn:
            conn.execute(
                rpa_work_items.insert().values(
                    work_id=work_id,
                    rpa_id=rpa_id,
                    mail_job_id=mail_job_id,
                    pipeline_run_id=pipeline_run_id,
                    source_file=basename,
                    file_hash=h or None,
                    status="queued",
                    attempt=1,
                    created_at=now,
                    message="Queued",
                )
            )
    except Exception as e:
        # Concurrent enqueue of same (rpa_id, file_hash) hit the unique index.
        if h and ("UNIQUE" in str(e).upper() or "unique" in str(e).lower()):
            blocking = find_blocking_work(rpa_id, h) or {}
            skip_id = str(uuid.uuid4())
            with engine.begin() as conn:
                conn.execute(
                    rpa_work_items.insert().values(
                        work_id=skip_id,
                        rpa_id=rpa_id,
                        mail_job_id=mail_job_id,
                        pipeline_run_id=pipeline_run_id,
                        source_file=basename or blocking.get("source_file"),
                        file_hash=h,
                        status="skipped_duplicate",
                        attempt=1,
                        started_at=now,
                        finished_at=now,
                        message=f"Skipped — race with {(blocking.get('work_id') or '')[:8]}",
                        created_at=now,
                    )
                )
            return {
                "work_id": skip_id,
                "status": "skipped_duplicate",
                "skipped": True,
                "blocking_work_id": blocking.get("work_id"),
                "file_hash": h,
                "source_file": basename,
                "message": "Duplicate (concurrent enqueue)",
                "rpa_id": rpa_id,
            }
        raise
    return {
        "work_id": work_id,
        "status": "queued",
        "skipped": False,
        "file_hash": h,
        "source_file": basename,
        "message": "Queued",
        "rpa_id": rpa_id,
    }


def get_work(work_id: str) -> Optional[dict]:
    if not work_id:
        return None
    _ensure()
    with engine.connect() as conn:
        row = conn.execute(
            select(rpa_work_items).where(rpa_work_items.c.work_id == work_id)
        ).fetchone()
    return _row_to_dict(row) if row else None


def claim_work(
    work_id: str,
    *,
    worker_pid: Optional[int] = None,
    chrome_port: Optional[int] = None,
) -> Optional[dict]:
    """Move queued → claimed with a lease. Returns updated row or None."""
    _ensure()
    now = datetime.now()
    lease = now + timedelta(minutes=_lease_minutes())
    with engine.begin() as conn:
        result = conn.execute(
            rpa_work_items.update()
            .where(rpa_work_items.c.work_id == work_id)
            .where(rpa_work_items.c.status == "queued")
            .values(
                status="claimed",
                claimed_at=now,
                lease_until=lease,
                worker_pid=worker_pid,
                chrome_port=int(chrome_port) if chrome_port else None,
                message="Claimed by worker",
            )
        )
        if result.rowcount == 0:
            return get_work(work_id)
    return get_work(work_id)


def mark_running(work_id: str) -> Optional[dict]:
    _ensure()
    now = datetime.now()
    lease = now + timedelta(minutes=_lease_minutes())
    with engine.begin() as conn:
        conn.execute(
            rpa_work_items.update()
            .where(rpa_work_items.c.work_id == work_id)
            .where(rpa_work_items.c.status.in_(("queued", "claimed", "running")))
            .values(
                status="running",
                started_at=now,
                lease_until=lease,
                worker_pid=os.getpid(),
                message="Running",
            )
        )
    return get_work(work_id)


def finish_work(
    work_id: str,
    status: str,
    *,
    message: Optional[str] = None,
    error: Optional[str] = None,
    upload_file: Optional[str] = None,
    record_legacy: bool = True,
) -> Optional[dict]:
    """Mark terminal status and optionally append to rpa_runs."""
    if not work_id:
        return None
    _ensure()
    now = datetime.now()
    terminal = status if status in (
        "succeeded", "failed", "cancelled", "skipped_duplicate"
    ) else "failed"
    # Map runner "ok"/"error" to work statuses.
    if status in ("ok", "success"):
        terminal = "succeeded"
    elif status in ("error", "err"):
        terminal = "failed"

    with engine.begin() as conn:
        conn.execute(
            rpa_work_items.update()
            .where(rpa_work_items.c.work_id == work_id)
            .values(
                status=terminal,
                finished_at=now,
                lease_until=None,
                message=(message or "")[:2000] or None,
                error=(error or "")[:4000] or None,
            )
        )

    item = get_work(work_id)
    if record_legacy and item:
        legacy_status = "ok" if terminal == "succeeded" else (
            "cancelled" if terminal == "cancelled" else "error"
        )
        if terminal == "skipped_duplicate":
            legacy_status = "skipped"
        try:
            _record_rpa_run_with_work(
                item.get("rpa_id") or "",
                legacy_status,
                message=message or error,
                upload_file=upload_file or item.get("source_file"),
                work_id=work_id,
            )
        except Exception:
            pass
    return item


def _record_rpa_run_with_work(
    rpa_id: str,
    status: str,
    message: Optional[str] = None,
    upload_file: Optional[str] = None,
    work_id: Optional[str] = None,
) -> None:
    _ensure()
    from db import rpa_runs

    with engine.begin() as conn:
        values = {
            "rpa_id": rpa_id,
            "upload_file": os.path.basename(upload_file) if upload_file else None,
            "status": status,
            "message": (message or "")[:2000],
            "ran_at": datetime.now(),
        }
        # work_id column may exist after migration
        try:
            cols = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(rpa_runs)"))
            }
            if "work_id" in cols:
                values["work_id"] = work_id
        except Exception:
            pass
        conn.execute(rpa_runs.insert().values(**values))


def attach_pipeline_run(work_id: str, pipeline_run_id: str) -> None:
    if not work_id or not pipeline_run_id:
        return
    _ensure()
    with engine.begin() as conn:
        conn.execute(
            rpa_work_items.update()
            .where(rpa_work_items.c.work_id == work_id)
            .values(pipeline_run_id=pipeline_run_id)
        )


def reclaim_stale_work(now: Optional[datetime] = None) -> int:
    """Mark claimed/running past lease_until as failed (orphaned worker)."""
    _ensure()
    now = now or datetime.now()
    with engine.begin() as conn:
        result = conn.execute(
            rpa_work_items.update()
            .where(rpa_work_items.c.status.in_(_LEASE_STATUSES))
            .where(rpa_work_items.c.lease_until.isnot(None))
            .where(rpa_work_items.c.lease_until < now)
            .values(
                status="failed",
                finished_at=now,
                lease_until=None,
                message="Orphaned worker — lease expired",
                error="Work item reclaimed after lease_until; Chrome worker likely crashed or hung",
            )
        )
        return int(result.rowcount or 0)


def list_work_items(
    *,
    limit: int = 50,
    status: Optional[str] = None,
    rpa_id: Optional[str] = None,
) -> list[dict]:
    _ensure()
    limit = max(1, min(int(limit or 50), 200))
    q = select(rpa_work_items).order_by(rpa_work_items.c.id.desc()).limit(limit)
    if status:
        q = q.where(rpa_work_items.c.status == status)
    if rpa_id:
        q = q.where(rpa_work_items.c.rpa_id == rpa_id)
    with engine.connect() as conn:
        rows = conn.execute(q).fetchall()
    return [_row_to_dict(r) for r in rows]


def work_item_counts_24h() -> dict[str, Any]:
    """Status counts for items created or finished in the last 24 hours."""
    _ensure()
    since = datetime.now() - timedelta(hours=24)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT status, COUNT(*) AS n FROM rpa_work_items "
                "WHERE created_at >= :since OR finished_at >= :since "
                "GROUP BY status"
            ),
            {"since": since},
        ).fetchall()
    counts = {r[0]: int(r[1]) for r in rows}
    return {
        "since": since.isoformat(timespec="seconds"),
        "counts": counts,
        "succeeded": counts.get("succeeded", 0),
        "failed": counts.get("failed", 0),
        "skipped_duplicate": counts.get("skipped_duplicate", 0),
        "running": counts.get("running", 0) + counts.get("claimed", 0) + counts.get("queued", 0),
        "cancelled": counts.get("cancelled", 0),
        "total": sum(counts.values()),
    }
