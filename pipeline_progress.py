"""
Live pipeline step tracking for the dashboard.

Each mail/RPA/email run gets a run_id with ordered steps that flip
pending → running → ok/error so the UI can show a stepper in real time.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Column, DateTime, Integer, MetaData, String, Table, Text, create_engine, text,
)

import config

_engine = create_engine(
    config.DB_URL,
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)
_meta = MetaData()

pipeline_runs = Table(
    "pipeline_runs", _meta,
    Column("id", String(36), primary_key=True),
    Column("kind", String(32)),          # mail | rpa | email
    Column("label", String(200)),
    Column("ref_id", String(64)),        # job_id / rpa_id
    Column("status", String(20)),        # running | ok | error
    Column("started_at", DateTime),
    Column("finished_at", DateTime),
    Column("message", Text),
)

pipeline_steps = Table(
    "pipeline_steps", _meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(36), index=True),
    Column("step_key", String(64)),
    Column("title", String(200)),
    Column("sort_order", Integer),
    Column("status", String(20)),        # pending | running | ok | error | skipped
    Column("message", Text),
    Column("started_at", DateTime),
    Column("finished_at", DateTime),
)

_lock = threading.Lock()
_active_run_id: Optional[str] = None
_stop_event = threading.Event()

# Contextvar-like thread-local so nested helpers can emit steps without
# threading run_id through every call.
_tls = threading.local()


class PipelineCancelled(Exception):
    """Raised when the user hits Stop on the dashboard."""
    pass


def _init():
    _meta.create_all(_engine)
    if config.DB_URL.startswith("sqlite"):
        with _engine.begin() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))


_init()


def current_run_id() -> Optional[str]:
    return getattr(_tls, "run_id", None)


def set_current_run(run_id: Optional[str]) -> None:
    _tls.run_id = run_id


def is_stop_requested() -> bool:
    return _stop_event.is_set()


def check_cancelled(message: str = "Stopped by user") -> None:
    """Raise PipelineCancelled if Stop was pressed."""
    if _stop_event.is_set():
        raise PipelineCancelled(message)


def request_stop(message: str = "Stopped by user") -> Optional[str]:
    """Signal the running pipeline to halt after the current step."""
    _stop_event.set()
    run_id = None
    with _lock:
        run_id = _active_run_id
    if run_id:
        # Mark currently-running step + remaining as cancelled immediately for UI
        now = datetime.now()
        with _engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE pipeline_steps SET status='error', message=:m, finished_at=:t "
                    "WHERE run_id=:r AND status='running'"
                ),
                {"m": message, "t": now, "r": run_id},
            )
            conn.execute(
                text(
                    "UPDATE pipeline_steps SET status='skipped', message='Cancelled', "
                    "finished_at=:t WHERE run_id=:r AND status='pending'"
                ),
                {"t": now, "r": run_id},
            )
            conn.execute(
                text(
                    "UPDATE pipeline_runs SET status='cancelled', message=:m, finished_at=:t "
                    "WHERE id=:r AND status='running'"
                ),
                {"m": message, "t": now, "r": run_id},
            )
    return run_id


def reset_pipeline(message: str = "Reset") -> None:
    """Stop any active run and clear the cancel flag so a new run can start clean."""
    request_stop(message)
    # Also force-finish any still-running rows (in case DB race)
    with _engine.begin() as conn:
        now = datetime.now()
        conn.execute(
            text(
                "UPDATE pipeline_runs SET status='cancelled', message=:m, finished_at=:t "
                "WHERE status='running'"
            ),
            {"m": message, "t": now},
        )
    clear_stop()
    set_current_run(None)


def clear_stop() -> None:
    _stop_event.clear()


def start_run(
    kind: str,
    ref_id: str,
    label: str,
    steps: List[tuple],
) -> str:
    """Create a run with ordered steps. steps = [(key, title), ...]."""
    _init()
    clear_stop()
    run_id = str(uuid.uuid4())
    now = datetime.now()
    with _engine.begin() as conn:
        conn.execute(pipeline_runs.insert().values(
            id=run_id,
            kind=kind,
            label=label,
            ref_id=ref_id or "",
            status="running",
            started_at=now,
            finished_at=None,
            message="",
        ))
        for i, (key, title) in enumerate(steps):
            conn.execute(pipeline_steps.insert().values(
                run_id=run_id,
                step_key=key,
                title=title,
                sort_order=i,
                status="pending",
                message="",
                started_at=None,
                finished_at=None,
            ))
    with _lock:
        global _active_run_id
        _active_run_id = run_id
    set_current_run(run_id)
    return run_id


def begin_step(step_key: str, message: str = "", run_id: Optional[str] = None) -> None:
    run_id = run_id or current_run_id()
    if not run_id:
        return
    now = datetime.now()
    with _engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE pipeline_steps SET status='running', message=:m, "
                "started_at=COALESCE(started_at, :t) "
                "WHERE run_id=:r AND step_key=:k"
            ),
            {"m": (message or "")[:2000], "t": now, "r": run_id, "k": step_key},
        )


def finish_step(
    step_key: str,
    status: str = "ok",
    message: str = "",
    run_id: Optional[str] = None,
) -> None:
    run_id = run_id or current_run_id()
    if not run_id:
        return
    now = datetime.now()
    with _engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE pipeline_steps SET status=:s, message=:m, finished_at=:t "
                "WHERE run_id=:r AND step_key=:k"
            ),
            {
                "s": status,
                "m": (message or "")[:2000],
                "t": now,
                "r": run_id,
                "k": step_key,
            },
        )


def skip_step(step_key: str, message: str = "", run_id: Optional[str] = None) -> None:
    finish_step(step_key, "skipped", message or "Skipped", run_id=run_id)


def finish_run(
    status: str = "ok",
    message: str = "",
    run_id: Optional[str] = None,
) -> None:
    run_id = run_id or current_run_id()
    if not run_id:
        return
    now = datetime.now()
    with _engine.begin() as conn:
        # Any still-pending steps → skipped on success, error stays if already set
        if status == "ok":
            conn.execute(
                text(
                    "UPDATE pipeline_steps SET status='skipped', message='Skipped', "
                    "finished_at=:t WHERE run_id=:r AND status='pending'"
                ),
                {"t": now, "r": run_id},
            )
        elif status in ("error", "cancelled"):
            conn.execute(
                text(
                    "UPDATE pipeline_steps SET status=:st, "
                    "message=COALESCE(NULLIF(message,''), :m), finished_at=:t "
                    "WHERE run_id=:r AND status IN ('pending','running')"
                ),
                {
                    "st": "skipped" if status == "cancelled" else "error",
                    "m": "Cancelled" if status == "cancelled" else "Stopped",
                    "t": now,
                    "r": run_id,
                },
            )
        conn.execute(
            text(
                "UPDATE pipeline_runs SET status=:s, message=:m, finished_at=:t "
                "WHERE id=:r"
            ),
            {"s": status, "m": (message or "")[:2000], "t": now, "r": run_id},
        )
    with _lock:
        global _active_run_id
        if _active_run_id == run_id:
            _active_run_id = run_id  # keep pointing at last finished for UI
    set_current_run(None)
    if status in ("ok", "error", "cancelled"):
        clear_stop()


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    _init()
    with _engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM pipeline_runs WHERE id=:r"),
            {"r": run_id},
        ).mappings().first()
        if not row:
            return None
        steps = conn.execute(
            text(
                "SELECT step_key, title, sort_order, status, message, "
                "started_at, finished_at FROM pipeline_steps "
                "WHERE run_id=:r ORDER BY sort_order"
            ),
            {"r": run_id},
        ).mappings().all()
    return {
        "id": row["id"],
        "kind": row["kind"],
        "label": row["label"],
        "ref_id": row["ref_id"],
        "status": row["status"],
        "started_at": _fmt(row["started_at"]),
        "finished_at": _fmt(row["finished_at"]),
        "message": row["message"] or "",
        "steps": [
            {
                "key": s["step_key"],
                "title": s["title"],
                "status": s["status"],
                "message": s["message"] or "",
                "started_at": _fmt(s["started_at"]),
                "finished_at": _fmt(s["finished_at"]),
            }
            for s in steps
        ],
    }


def get_active_run() -> Optional[Dict[str, Any]]:
    """Most recent running run, else the latest finished one (for the panel)."""
    _init()
    with _engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT id FROM pipeline_runs WHERE status='running' "
                "ORDER BY started_at DESC LIMIT 1"
            )
        ).first()
        if not row:
            row = conn.execute(
                text(
                    "SELECT id FROM pipeline_runs "
                    "ORDER BY started_at DESC LIMIT 1"
                )
            ).first()
    if not row:
        return None
    return get_run(row[0])


def list_recent_runs(limit: int = 10) -> List[Dict[str, Any]]:
    _init()
    with _engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id FROM pipeline_runs "
                "ORDER BY started_at DESC LIMIT :n"
            ),
            {"n": limit},
        ).fetchall()
    out = []
    for (rid,) in rows:
        r = get_run(rid)
        if r:
            out.append(r)
    return out


def _fmt(dt) -> Optional[str]:
    if dt is None:
        return None
    if hasattr(dt, "strftime"):
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return str(dt)
