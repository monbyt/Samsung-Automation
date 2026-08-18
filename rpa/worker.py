"""Standalone process: one inbox Excel → RPA → email, own Chrome window."""
from __future__ import annotations

import json
import os
import sys
import traceback

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")


def _write_result(path: str, payload: dict) -> None:
    if not path:
        return
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def _load_payload() -> dict:
    if len(sys.argv) >= 3 and sys.argv[1] in ("--payload-file", "-f"):
        with open(sys.argv[2], encoding="utf-8") as fh:
            return json.load(fh)
    raw = sys.argv[1] if len(sys.argv) > 1 else "{}"
    if raw.endswith(".json") and os.path.isfile(raw):
        with open(raw, encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(raw)


def main() -> int:
    payload = _load_payload()
    log_path = (payload.get("log_path") or "").strip()
    if log_path:
        from rpa.hang_alert import install_worker_tee
        install_worker_tee(log_path)
        print(f"[RPA worker] log file: {log_path}", flush=True)
    result_path = payload.get("result_path") or ""
    profile = (payload.get("chrome_profile") or "").strip()
    if profile:
        os.makedirs(profile, exist_ok=True)
        os.environ["RPA_CHROME_PROFILE"] = profile
        print(f"[RPA worker] Chrome profile: {profile}", flush=True)
    port = payload.get("chrome_port")
    if port:
        os.environ["RPA_CHROME_DEBUG_PORT"] = str(port)
        print(f"[RPA worker] debug port: {port}", flush=True)
    print(
        f"[RPA worker] pid={os.getpid()} file={os.path.basename(payload.get('upload_file') or '')}",
        flush=True,
    )

    from pipeline_progress import set_current_run
    from rpa.runner import _parallel_worker

    set_current_run(None)
    try:
        result = _parallel_worker(payload)
    except Exception as e:
        result = {
            "rpa_id": payload.get("rpa_id"),
            "status": "error",
            "message": str(e),
            "upload_file": payload.get("upload_file"),
            "trace": traceback.format_exc()[-500:],
        }
    if not isinstance(result, dict):
        result = {"status": "ok", "raw": str(result)}
    _write_result(result_path, result)
    status = (result.get("status") or "ok").lower()
    return 0 if status in ("ok", "skipped") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        payload = {}
        try:
            payload = _load_payload()
        except Exception:
            pass
        _write_result(
            payload.get("result_path") or "",
            {"status": "error", "message": str(e), "trace": traceback.format_exc()[-500:]},
        )
        raise
