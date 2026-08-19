"""
24/7 hang alerts: if a Live session is still running after ~1 hour, email
screenshots plus as much log text as we can gather.

The mail scheduler blocks while a job runs, so this watchdog is its own
thread. Screenshots use Chrome's debug port (subprocess, so it does not
touch the stuck Playwright session) and a desktop capture on Windows.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

import config

_LOCK = threading.Lock()
_LIVE: Dict[int, Dict[str, Any]] = {}
_WATCHDOG_STARTED = False
_TEE_INSTALLED = False
_DASHBOARD_LOG = ""

_TICK_S = 30
_MAX_ATTACH = 4
_MAX_ATTACH_BYTES = 4 * 1024 * 1024
_LOG_TAIL_CHARS = 24_000
_BODY_CHARS = 12_000


def _log(msg: str) -> None:
    print(f"[alert {datetime.now():%H:%M:%S}] {msg}", flush=True)


def log_dir() -> str:
    path = getattr(config, "LOG_DIR", None) or os.path.join(config.BASE_DIR, "logs")
    os.makedirs(path, exist_ok=True)
    os.makedirs(os.path.join(path, "workers"), exist_ok=True)
    os.makedirs(os.path.join(path, "alerts"), exist_ok=True)
    return path


def dashboard_log_path() -> str:
    global _DASHBOARD_LOG
    if _DASHBOARD_LOG:
        return _DASHBOARD_LOG
    path = os.path.join(log_dir(), f"dashboard-{datetime.now():%Y%m%d}.log")
    _DASHBOARD_LOG = path
    return path


def stuck_minutes() -> int:
    try:
        from mail.settings_db import get_setting
        raw = get_setting("stuck_minutes", "")
    except Exception:
        raw = ""
    if not (raw or "").strip():
        raw = str(getattr(config, "RPA_STUCK_MINUTES", 60))
    try:
        n = int(float(raw))
    except (TypeError, ValueError):
        n = 60
    return max(5, min(n, 24 * 60))


def alert_emails() -> str:
    try:
        from mail.settings_db import get_setting
        return (get_setting("alert_emails", "") or "").strip()
    except Exception:
        return ""


def worker_log_path(label: str = "", upload_file: str = "") -> str:
    base = os.path.basename(upload_file or "") or (label or "worker")
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in base)[:80]
    return os.path.join(log_dir(), "workers", f"{datetime.now():%Y%m%d-%H%M%S}_{safe}.log")


# ── stdout tee (dashboard + each worker process) ──────────────────────────


class _Tee:
    def __init__(self, stream, path: str):
        self.stream = stream
        self.path = path
        self._lock = threading.Lock()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def write(self, data):
        if not data:
            return 0
        try:
            self.stream.write(data)
        except Exception:
            pass
        try:
            with self._lock:
                with open(self.path, "a", encoding="utf-8", errors="replace") as fh:
                    fh.write(data if isinstance(data, str) else str(data))
        except Exception:
            pass
        return len(data) if isinstance(data, str) else 0

    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass

    def fileno(self):
        return self.stream.fileno()

    def isatty(self):
        try:
            return self.stream.isatty()
        except Exception:
            return False


def install_dashboard_tee() -> str:
    """Mirror dashboard stdout/stderr into logs/dashboard-YYYYMMDD.log."""
    global _TEE_INSTALLED
    path = dashboard_log_path()
    if _TEE_INSTALLED:
        return path
    sys.stdout = _Tee(sys.stdout, path)
    sys.stderr = _Tee(sys.stderr, path)
    _TEE_INSTALLED = True
    return path


def install_worker_tee(path: str) -> None:
    if not path:
        return
    sys.stdout = _Tee(sys.stdout, path)
    sys.stderr = _Tee(sys.stderr, path)


# ── live worker registry (parent dashboard process) ───────────────────────


def _registry_path() -> str:
    return os.path.join(log_dir(), "live-workers.json")


def _persist_live() -> None:
    try:
        serial = []
        for info in _LIVE.values():
            row = dict(info)
            for key in ("started_at", "last_alert_at"):
                val = row.get(key)
                if isinstance(val, datetime):
                    row[key] = val.isoformat()
            serial.append(row)
        with open(_registry_path(), "w", encoding="utf-8") as fh:
            json.dump(serial, fh, indent=2)
    except Exception:
        pass


def _load_live() -> None:
    path = _registry_path()
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            rows = json.load(fh)
    except Exception:
        return
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = int(row.get("pid") or 0)
        if not pid or not _pid_alive(pid):
            continue
        for key in ("started_at", "last_alert_at"):
            val = row.get(key)
            if isinstance(val, str) and val:
                try:
                    row[key] = datetime.fromisoformat(val)
                except Exception:
                    row[key] = datetime.now()
        _LIVE[pid] = row


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
            )
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == 259  # STILL_ACTIVE
                return True
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def register_worker(info: dict) -> None:
    pid = int(info.get("pid") or 0)
    if not pid:
        return
    row = dict(info)
    row["pid"] = pid
    row.setdefault("started_at", datetime.now())
    row.setdefault("last_alert_at", None)
    with _LOCK:
        _LIVE[pid] = row
        _persist_live()


def unregister_worker(pid: int) -> None:
    with _LOCK:
        _LIVE.pop(int(pid), None)
        _persist_live()


def live_workers() -> List[dict]:
    with _LOCK:
        dead = [pid for pid in list(_LIVE) if not _pid_alive(pid)]
        for pid in dead:
            _LIVE.pop(pid, None)
        if dead:
            _persist_live()
        return [dict(v) for v in _LIVE.values()]


# ── evidence: screenshots + logs ──────────────────────────────────────────


def _tail_file(path: str, max_chars: int = _LOG_TAIL_CHARS) -> str:
    if not path or not os.path.isfile(path):
        return ""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_chars:
                fh.seek(-max_chars, os.SEEK_END)
            data = fh.read()
        text = data.decode("utf-8", errors="replace")
        if size > max_chars:
            text = "[... truncated ...]\n" + text
        return text
    except Exception as e:
        return f"(could not read {path}: {e})"


def _dir_listing(path: str, limit: int = 40) -> str:
    if not path or not os.path.isdir(path):
        return f"(not a folder: {path})"
    try:
        names = os.listdir(path)
    except Exception as e:
        return f"(listdir failed: {e})"
    rows = []
    for name in names:
        full = os.path.join(path, name)
        try:
            st = os.stat(full)
            stamp = datetime.fromtimestamp(st.st_mtime).strftime("%H:%M:%S")
            rows.append((st.st_mtime, f"  {stamp}  {st.st_size:9d}  {name}"))
        except Exception:
            rows.append((0, f"  {name}"))
    rows.sort(reverse=True)
    extra = ""
    if len(rows) > limit:
        extra = f"\n  … {len(rows) - limit} more"
        rows = rows[:limit]
    return "\n".join(r[1] for r in rows) + extra if rows else "  (empty)"


def _desktop_screenshot(path: str) -> str:
    if sys.platform != "win32":
        return ""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ps_path = path.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
        "$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
        "$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
        "$g = [System.Drawing.Graphics]::FromImage($bmp); "
        "$g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size); "
        f"$bmp.Save('{ps_path}', [System.Drawing.Imaging.ImageFormat]::Png); "
        "$g.Dispose(); $bmp.Dispose();"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=20,
            check=False,
            capture_output=True,
        )
    except Exception as e:
        _log(f"Desktop screenshot failed: {e}")
        return ""
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    return ""


def capture_cdp_screenshots(port: int, out_dir: str) -> List[str]:
    """Connect to a live Chromium debug port from a child process and save PNGs."""
    if not port:
        return []
    os.makedirs(out_dir, exist_ok=True)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "rpa.hang_alert", "capture-cdp", str(int(port)), out_dir],
            cwd=config.BASE_DIR,
            timeout=25,
            capture_output=True,
            text=True,
            env={**os.environ, "NO_PROXY": "*", "no_proxy": "*"},
        )
    except Exception as e:
        _log(f"CDP screenshot spawn failed port={port}: {e}")
        return []
    paths = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line and os.path.isfile(line):
            paths.append(line)
    if not paths and proc.returncode:
        err = (proc.stderr or proc.stdout or "")[-400:]
        _log(f"CDP screenshot failed port={port}: {err}")
    return paths


def _run_cdp_capture(port: int, out_dir: str) -> None:
    os.environ.setdefault("NO_PROXY", "*")
    os.environ.setdefault("no_proxy", "*")
    from playwright.sync_api import sync_playwright

    os.makedirs(out_dir, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{int(port)}")
        n = 0
        for ctx in browser.contexts:
            for page in ctx.pages:
                n += 1
                dest = os.path.join(out_dir, f"browser-port{port}-tab{n}.png")
                try:
                    page.screenshot(path=dest, timeout=8000)
                    print(dest, flush=True)
                except Exception as e:
                    print(f"tab {n} failed: {e}", file=sys.stderr, flush=True)
        if n == 0:
            print("no open tabs", file=sys.stderr, flush=True)


def _write_text(path: str, body: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", errors="replace") as fh:
        fh.write(body or "")
    return path


def gather_evidence(run: Optional[dict], worker: Optional[dict]) -> tuple[str, List[str]]:
    """Build email body + attachment paths for one stuck session."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(log_dir(), "alerts", stamp)
    os.makedirs(out_dir, exist_ok=True)

    label = (run or {}).get("label") or (worker or {}).get("label") or "unknown session"
    rpa_id = (run or {}).get("ref_id") or (worker or {}).get("rpa_id") or ""
    duration_s = (run or {}).get("duration_s")
    if duration_s is None and worker and worker.get("started_at"):
        started = worker["started_at"]
        if isinstance(started, str):
            try:
                started = datetime.fromisoformat(started)
            except Exception:
                started = None
        if isinstance(started, datetime):
            duration_s = (datetime.now() - started).total_seconds()
    mins = round((duration_s or 0) / 60, 1)
    port = int((worker or {}).get("chrome_port") or 0)
    pid = int((worker or {}).get("pid") or 0)
    if not port:
        env_port = (os.environ.get("RPA_CHROME_DEBUG_PORT") or "").strip()
        if env_port:
            try:
                port = int(env_port)
            except ValueError:
                port = 0
        elif (run or {}).get("kind") == "rpa":
            port = 9329

    lines = [
        "STUCK SESSION ALERT",
        "This Live run has been going for about an hour and is still marked running.",
        "The job was NOT stopped — this is only an alert.",
        "",
        f"Time:     {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Label:    {label}",
        f"Kind:     {(run or {}).get('kind') or 'worker'}",
        f"Ref:      {rpa_id}",
        f"Run id:   {(run or {}).get('id') or '-'}",
        f"Started:  {(run or {}).get('started_at') or (worker or {}).get('started_at')}",
        f"Duration: {mins} minutes",
        f"PID:      {pid or '-'}",
        f"Chrome:   port {port}" if port else "Chrome:   (no debug port)",
        f"Upload:   {(worker or {}).get('upload_file') or '-'}",
        f"Upload dir:   {(worker or {}).get('upload_dir') or '-'}",
        f"Download dir: {(worker or {}).get('download_dir') or '-'}",
        f"Worker log:   {(worker or {}).get('log_path') or '-'}",
        "",
    ]

    if run:
        lines.append("Pipeline steps:")
        for step in run.get("steps") or []:
            lines.append(
                f"  [{step.get('status')}] {step.get('title')}"
                + (f" — {step.get('message')}" if step.get("message") else "")
            )
        if run.get("message"):
            lines.append(f"Run message: {run['message']}")
        lines.append("")

    attachments: List[str] = []

    desk = _desktop_screenshot(os.path.join(out_dir, "desktop.png"))
    if desk:
        attachments.append(desk)
        lines.append(f"Desktop screenshot: {os.path.basename(desk)}")

    cdp_ports = []
    if port:
        cdp_ports.append(port)
    if (run or {}).get("kind") == "mail":
        mail_port = int(getattr(config, "MAIL_CDP_PORT", 9222) or 9222)
        if mail_port and mail_port not in cdp_ports:
            cdp_ports.append(mail_port)
    for p in cdp_ports:
        shots = capture_cdp_screenshots(p, out_dir)
        attachments.extend(shots)
        if shots:
            lines.append(f"Browser screenshots (port {p}): " + ", ".join(os.path.basename(s) for s in shots))
        else:
            lines.append(f"Browser screenshot (port {p}): none (window busy or port closed)")

    log_chunks = []
    worker_log = (worker or {}).get("log_path") or ""
    if worker_log:
        log_chunks.append(("Worker log", worker_log, _tail_file(worker_log)))
    dash = dashboard_log_path()
    if dash and os.path.abspath(dash) != os.path.abspath(worker_log):
        log_chunks.append(("Dashboard log", dash, _tail_file(dash)))

    for title, path, text in log_chunks:
        lines.append("")
        lines.append(f"===== {title}: {path} =====")
        lines.append(text or "(empty)")

    upload_dir = (worker or {}).get("upload_dir") or ""
    download_dir = (worker or {}).get("download_dir") or ""
    if upload_dir:
        lines.append("")
        lines.append(f"===== Upload folder {upload_dir} =====")
        lines.append(_dir_listing(upload_dir))
    if download_dir:
        lines.append("")
        lines.append(f"===== Download folder {download_dir} =====")
        lines.append(_dir_listing(download_dir))

    if run:
        lines.append("")
        lines.append("===== Pipeline run =====")
        lines.append(json.dumps(run, indent=2, default=str)[:4000])

    body = "\n".join(lines)
    body_path = _write_text(os.path.join(out_dir, "alert-body.txt"), body)
    attachments.append(body_path)

    def _ok(path: str) -> bool:
        try:
            return os.path.isfile(path) and 0 < os.path.getsize(path) <= _MAX_ATTACH_BYTES
        except OSError:
            return False

    pngs = [p for p in attachments if p.lower().endswith(".png") and _ok(p)]
    texts = [p for p in attachments if p.lower().endswith(".txt") and _ok(p)]
    slim = []
    seen = set()
    for path in pngs + texts:
        ap = os.path.abspath(path)
        if ap in seen:
            continue
        seen.add(ap)
        slim.append(ap)
        if len(slim) >= _MAX_ATTACH:
            break
    return body, slim


# ── send ──────────────────────────────────────────────────────────────────


def _send(subject: str, body: str, files: List[str]) -> None:
    to = alert_emails()
    if not to:
        raise RuntimeError(
            "No alert emails in Settings. Open Settings → Stuck-session alerts."
        )
    from mail.sender import SendError, send_email

    short = body if len(body) <= _BODY_CHARS else body[:_BODY_CHARS] + "\n\n[... truncated; full text in alert-body.txt ...]"
    pngs = [p for p in (files or []) if str(p).lower().endswith((".png", ".jpg", ".jpeg"))]
    attempts = [
        (short, files or []),
        (short[:4000], pngs[:2]),
        (short[:2500], []),
    ]
    last_err: Optional[Exception] = None
    for i, (text, attach) in enumerate(attempts, start=1):
        try:
            send_email(to=to, subject=subject, body=text, files=attach)
            if i > 1:
                _log(f"Hang alert sent on fallback attempt {i} (attachments={len(attach)})")
            return
        except SendError as e:
            last_err = e
            _log(f"Hang alert send attempt {i}/3 failed: {e}")
    raise last_err or RuntimeError("Hang alert send failed")


def send_test_alert() -> str:
    """Settings button: prove Agent mail + screenshot path works."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(log_dir(), "alerts", f"test-{stamp}")
    os.makedirs(out_dir, exist_ok=True)
    files = []
    desk = _desktop_screenshot(os.path.join(out_dir, "desktop.png"))
    if desk:
        files.append(desk)
    body = (
        "Test stuck-session alert from Samsung Automation.\n"
        f"Time: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"Stuck threshold: {stuck_minutes()} minutes\n"
        f"Log folder: {log_dir()}\n"
        "If you received this, overnight hang emails will use the same path.\n"
    )
    _send("[ALERT TEST] Samsung Automation hang alert", body, files)
    return alert_emails()


def _match_worker(run: dict) -> Optional[dict]:
    workers = live_workers()
    label = (run.get("label") or "").strip()
    rid = (run.get("ref_id") or "").strip()
    for w in workers:
        if label and (w.get("label") or "").strip() == label:
            return w
    for w in workers:
        if rid and (w.get("rpa_id") or "") == rid:
            return w
    if len(workers) == 1:
        return workers[0]
    return None


def _should_skip_parent_mail(run: dict) -> bool:
    """Mail card that is only waiting on RPA workers — those workers alert themselves."""
    if run.get("kind") != "mail":
        return False
    running = [s for s in (run.get("steps") or []) if s.get("status") == "running"]
    if any(s.get("key") == "rpa" for s in running):
        return True
    return False


def _mark_worker_alerted(pid: int) -> None:
    with _LOCK:
        info = _LIVE.get(int(pid))
        if info is not None:
            info["last_alert_at"] = datetime.now()
            _persist_live()


def _worker_due(info: dict, threshold_s: float) -> bool:
    started = info.get("started_at")
    if isinstance(started, str):
        try:
            started = datetime.fromisoformat(started)
        except Exception:
            return False
    if not isinstance(started, datetime):
        return False
    if (datetime.now() - started).total_seconds() < threshold_s:
        return False
    last = info.get("last_alert_at")
    if isinstance(last, str):
        try:
            last = datetime.fromisoformat(last)
        except Exception:
            last = None
    if isinstance(last, datetime) and (datetime.now() - last).total_seconds() < threshold_s:
        return False
    return True


def check_once() -> List[str]:
    """Scan Live runs + worker PIDs; send at most one email per stuck session."""
    sent: List[str] = []
    minutes = stuck_minutes()
    threshold_s = minutes * 60
    to = alert_emails()
    if not to:
        return sent

    try:
        from pipeline_progress import claim_alert, get_active_runs
        runs = get_active_runs()
    except Exception:
        _log("Could not read live runs:\n" + traceback.format_exc()[-400:])
        runs = []

    handled_pids = set()
    for run in runs:
        dur = run.get("duration_s")
        if dur is None or dur < threshold_s:
            continue
        if _should_skip_parent_mail(run):
            continue
        worker = _match_worker(run)
        if not claim_alert(run["id"], threshold_s):
            continue
        try:
            body, files = gather_evidence(run, worker)
            mins = round(dur / 60, 1)
            subject = f"[ALERT] RPA still running {mins:.0f} min — {run.get('label') or run.get('ref_id')}"
            _send(subject, body, files)
            sent.append(run.get("label") or run["id"])
            _log(f"Sent hang alert for {run.get('label')} ({mins} min) to {to}")
            if worker and worker.get("pid"):
                _mark_worker_alerted(int(worker["pid"]))
                handled_pids.add(int(worker["pid"]))
        except Exception:
            _log("Hang alert send failed:\n" + traceback.format_exc()[-800:])

    for worker in live_workers():
        pid = int(worker.get("pid") or 0)
        if not pid or pid in handled_pids:
            continue
        if not _worker_due(worker, threshold_s):
            continue
        # No matching Live card (process started before start_run, or DB miss).
        try:
            body, files = gather_evidence(None, worker)
            started = worker.get("started_at")
            mins = 0.0
            if isinstance(started, datetime):
                mins = (datetime.now() - started).total_seconds() / 60
            subject = (
                f"[ALERT] RPA worker PID {pid} still running "
                f"{mins:.0f} min — {worker.get('label') or worker.get('rpa_id')}"
            )
            _send(subject, body, files)
            sent.append(worker.get("label") or str(pid))
            _mark_worker_alerted(pid)
            _log(f"Sent hang alert for worker PID {pid} to {to}")
        except Exception:
            _log("Worker hang alert failed:\n" + traceback.format_exc()[-800:])

    return sent


def _watchdog_loop() -> None:
    _log(
        f"Hang watchdog started — email after {stuck_minutes()} min "
        f"(to {alert_emails() or 'NO ADDRESS SET in Settings'})"
    )
    while True:
        try:
            check_once()
        except Exception:
            _log("Watchdog tick failed:\n" + traceback.format_exc()[-500:])
        time.sleep(_TICK_S)


def start_watchdog() -> None:
    global _WATCHDOG_STARTED
    with _LOCK:
        if _WATCHDOG_STARTED:
            return
        _WATCHDOG_STARTED = True
        _load_live()
    t = threading.Thread(target=_watchdog_loop, daemon=True, name="hang-watchdog")
    t.start()


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "capture-cdp":
        _run_cdp_capture(int(sys.argv[2]), sys.argv[3])
    else:
        print("Usage: python -m rpa.hang_alert capture-cdp PORT OUTDIR")
        sys.exit(2)
