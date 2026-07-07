"""
Playwright codegen — record, save, and run custom RPA scripts.
"""
import os
import re
import runpy
import subprocess
import sys

import config

_RPA_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def _validate_rpa_id(rpa_id: str) -> str:
    if not _RPA_ID_RE.match(rpa_id or ""):
        raise ValueError(f"Invalid RPA id: {rpa_id!r}")
    return rpa_id


def script_path(rpa_id: str) -> str:
    _validate_rpa_id(rpa_id)
    return os.path.join(config.RPA_SCRIPTS_DIR, f"{rpa_id}.py")


def has_script(rpa_id: str) -> bool:
    return os.path.isfile(script_path(rpa_id))


def read_script(rpa_id: str) -> str:
    path = script_path(rpa_id)
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def save_script(rpa_id: str, content: str) -> str:
    path = script_path(rpa_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    return path


def launch_recorder(rpa_id: str, start_url: str) -> str:
    """Open Playwright codegen in a new window; saves to rpa/scripts/<id>.py."""
    _validate_rpa_id(rpa_id)
    url = (start_url or "").strip()
    if not url:
        raise ValueError("Start URL is required to record.")

    path = script_path(rpa_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "playwright",
        "codegen",
        url,
        "-o",
        path,
        "--target",
        "python",
        "-b",
        "chromium",
        "--channel",
        "chrome",
    ]

    if sys.platform == "win32":
        shell_cmd = (
            f'start "Playwright Codegen — {rpa_id}" '
            f'"{sys.executable}" -m playwright codegen '
            f'"{url}" -o "{path}" --target python -b chromium --channel chrome'
        )
        subprocess.Popen(shell_cmd, shell=True, cwd=config.BASE_DIR)
    else:
        subprocess.Popen(cmd, cwd=config.BASE_DIR)

    return path


def run_recorded_script(rpa_id: str) -> None:
    """Execute a saved codegen script."""
    os.environ.setdefault("NO_PROXY", "*")
    os.environ.setdefault("no_proxy", "*")

    path = script_path(rpa_id)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"No script for '{rpa_id}'. Open Record in the dashboard and save codegen output."
        )

    print(f"  Running recorded script: {path}")
    runpy.run_path(path, run_name="__main__")
