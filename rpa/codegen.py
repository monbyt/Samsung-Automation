"""
Playwright codegen — record, save, and run custom RPA scripts.
"""
import os
import re
import runpy
import shutil
import subprocess
import sys
from typing import Optional

import config

_RPA_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_INPUT_FILES_RE = re.compile(r'\.set_input_files\(\s*["\']([^"\']+)["\']\s*\)')


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


def _resolve_recorded_path(recorded: str) -> str:
    if os.path.isabs(recorded):
        return os.path.normpath(recorded)
    return os.path.normpath(os.path.join(config.BASE_DIR, recorded))


def stage_upload_for_script(rpa_id: str, upload_path: str) -> list[str]:
    """
    Copy the run file to every path used in set_input_files(...) in the script.
    Record with any filename (e.g. Book1.xlsx) — the latest mail file is copied there before run.
    """
    script = script_path(rpa_id)
    with open(script, encoding="utf-8") as f:
        content = f.read()

    targets = {_resolve_recorded_path(m.group(1)) for m in _INPUT_FILES_RE.finditer(content)}
    if not targets:
        return []

    staged = []
    for dest in sorted(targets):
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.copy2(upload_path, dest)
        staged.append(dest)
        print(f"  Auto-upload staged → {dest}")
    return staged


def run_recorded_script(rpa_id: str, upload_file: Optional[str] = None) -> None:
    """Execute a saved codegen script."""
    os.environ.setdefault("NO_PROXY", "*")
    os.environ.setdefault("no_proxy", "*")

    path = script_path(rpa_id)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"No script for '{rpa_id}'. Open Record in the dashboard and save codegen output."
        )

    if upload_file and os.path.isfile(upload_file):
        os.environ["RPA_UPLOAD_FILE"] = os.path.abspath(upload_file)
        staged = stage_upload_for_script(rpa_id, upload_file)
        if staged:
            print(f"  Using file: {upload_file}")
        else:
            print(
                f"  Upload file ready at RPA_UPLOAD_FILE={upload_file} "
                "(script has no set_input_files paths to auto-fill)"
            )
    else:
        os.environ.pop("RPA_UPLOAD_FILE", None)

    print(f"  Running recorded script: {path}")
    runpy.run_path(path, run_name="__main__")
