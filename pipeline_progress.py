"""
Live pipeline step tracking for the dashboard.

Supports multiple concurrent runs (parallel RPA workers). Each run has its
own cancel flag so Stop on one card does not kill the others.
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
    Column("status", String(20)),        # running | ok | error | cancelled
    Column("started_at", DateTime),
    Column("finished_at", DateTime),
    Column("message", Text),
    Column("cancel_requested", Integer, default=0),
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
_active_run_ids: set[str] = set()
# In-process fast path (same process as the worker thread). Cross-process
# cancel uses pipeline_runs.cancel_requested in SQLite.
_stop_events: Dict[str, threading.Event] = {}

_tls = threading.local()


class PipelineCancelled(Exception):
    """Raised when the user hits Stop on the dashboard."""
    pass


def _init():
    _meta.create_all(_engine)
    if config.DB_URL.startswith("sqlite"):
        with _engine.begin() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(pipeline_runs)"))}
            if "cancel_requested" not in cols:
                conn.execute(text(
                    "ALTER TABLE pipeline_runs ADD COLUMN cancel_requested INTEGER DEFAULT 0"
                ))


_init()


def current_run_id() -> Optional[str]:
    return getattr(_tls, "run_id", None)


def set_current_run(run_id: Optional[str]) -> None:
    _tls.run_id = run_id


def is_stop_requested(run_id: Optional[str] = None) -> bool:
    run_id = run_id or current_run_id()
    if not run_id:
        return False
    with _lock:
        ev = _stop_events.get(run_id)
        if ev is not None and ev.is_set():
            return True
    try:
        with _engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT cancel_requested FROM pipeline_runs "
                    "WHERE id=:r AND status='running'"
                ),
                {"r": run_id},
            ).first()
        return bool(row and row[0])
    except Exception:
        return False


def check_cancelled(message: str = "Stopped by user", run_id: Optional[str] = None) -> None:
    """Raise PipelineCancelled if Stop was pressed for this run."""
    if is_stop_requested(run_id):
        raise PipelineCancelled(message)


def _mark_run_cancelled_in_db(run_id: str, message: str) -> None:
    now = datetime.now()
    with _engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE pipeline_runs SET cancel_requested=1 WHERE id=:r"
            ),
            {"r": run_id},
        )
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


def request_stop(message: str = "Stopped by user", run_id: Optional[str] = None) -> Optional[str]:
    """Stop one run (run_id) or every running run if run_id is omitted."""
    targets: List[str] = []
    if run_id:
        targets = [run_id]
    else:
        with _engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id FROM pipeline_runs WHERE status='running'")
            ).fetchall()
        targets = [r[0] for r in rows]
        with _lock:
            targets = list(dict.fromkeys(list(_active_run_ids) + targets))

    if not targets:
        return None

    for rid in targets:
        with _lock:
            ev = _stop_events.get(rid)
            if ev is None:
                ev = threading.Event()
                _stop_events[rid] = ev
            ev.set()
        try:
            _mark_run_cancelled_in_db(rid, message)
        except Exception:
            pass
    return targets[0] if len(targets) == 1 else targets[0]


def reset_pipeline(message: str = "Reset") -> None:
    """Cancel every running run and clear stop flags."""
    request_stop(message)
    with _engine.begin() as conn:
        now = datetime.now()
        conn.execute(
            text(
                "UPDATE pipeline_runs SET status='cancelled', message=:m, finished_at=:t, "
                "cancel_requested=1 WHERE status='running'"
            ),
            {"m": message, "t": now},
        )
    with _lock:
        for ev in _stop_events.values():
            ev.clear()
        _stop_events.clear()
        _active_run_ids.clear()
    set_current_run(None)


def clear_stop(run_id: Optional[str] = None) -> None:
    run_id = run_id or current_run_id()
    if not run_id:
        return
    with _lock:
        ev = _stop_events.get(run_id)
        if ev is not None:
            ev.clear()


def start_run(
    kind: str,
    ref_id: str,
    label: str,
    steps: List[tuple],
) -> str:
    """Create a run with ordered steps. steps = [(key, title), ...]."""
    _init()
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
            cancel_requested=0,
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
        _active_run_ids.add(run_id)
        _stop_events[run_id] = threading.Event()
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
        _active_run_ids.discard(run_id)
        _stop_events.pop(run_id, None)
    if current_run_id() == run_id:
        set_current_run(None)


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
        "duration_s": _duration_s(row["started_at"], row["finished_at"]),
        "message": row["message"] or "",
        "cancel_requested": bool(row.get("cancel_requested")),
        "steps": [
            {
                "key": s["step_key"],
                "title": s["title"],
                "status": s["status"],
                "message": s["message"] or "",
                "started_at": _fmt(s["started_at"]),
                "finished_at": _fmt(s["finished_at"]),
                "duration_s": _duration_s(s["started_at"], s["finished_at"]),
            }
            for s in steps
        ],
    }


def get_active_run() -> Optional[Dict[str, Any]]:
    """Most recent currently running run, or None."""
    runs = get_active_runs()
    return runs[0] if runs else None


def get_active_runs() -> List[Dict[str, Any]]:
    """Currently running runs only (Live execution)."""
    _init()
    out: List[Dict[str, Any]] = []
    with _engine.connect() as conn:
        running = conn.execute(
            text(
                "SELECT id FROM pipeline_runs WHERE status='running' "
                "ORDER BY started_at DESC"
            )
        ).fetchall()
    for (rid,) in running:
        r = get_run(rid)
        if r:
            out.append(r)
    return out


def list_recent_runs(limit: int = 30) -> List[Dict[str, Any]]:
    _init()
    with _engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id FROM pipeline_runs "
                "WHERE status != 'running' "
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


def _duration_s(start, end) -> Optional[float]:
    if start is None:
        return None
    try:
        delta = (end or datetime.now()) - start
        return round(delta.total_seconds(), 1)
    except Exception:
        return None


def _fmt(dt) -> Optional[str]:
    if dt is None:
        return None
    if hasattr(dt, "strftime"):
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return str(dt)
