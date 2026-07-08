"""NDJSON debug logging for upload troubleshooting (debug session af08a5)."""
import json
import os
import time

_SESSION = "af08a5"
_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".cursor",
    f"debug-{_SESSION}.log",
)


def debug_log(hypothesis_id: str, location: str, message: str, data=None, run_id: str = "pre-fix") -> None:
    # region agent log
    try:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "sessionId": _SESSION,
                        "runId": run_id,
                        "hypothesisId": hypothesis_id,
                        "location": location,
                        "message": message,
                        "data": data or {},
                        "timestamp": int(time.time() * 1000),
                    },
                    default=str,
                )
                + "\n"
            )
    except Exception:
        pass
    # endregion
