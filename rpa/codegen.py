"""
Playwright codegen — record, save, and run custom RPA scripts.
"""
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from typing import Optional

import config

_RPA_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_INPUT_FILES_RE = re.compile(r'\.set_input_files\(\s*["\']([^"\']+)["\']\s*\)')
_ACTION_TIMEOUT_RE = re.compile(r",?\s*timeout=\d+")
_NEW_CONTEXT_RE = re.compile(
    r"(context\s*=\s*browser\.new_context\([^)]*\)\s*\n)",
    re.MULTILINE,
)
_PERSISTENT_CONTEXT_RE = re.compile(
    r"(context\s*=\s*\w+\.chromium\.launch_persistent_context\([^)]*\)\s*\n)",
    re.MULTILINE,
)
_NO_TIMEOUT_BLOCK = (
    "    context.set_default_timeout(0)\n"
    "    context.set_default_navigation_timeout(0)\n"
)
_PLAYWRIGHT_PATCHED = False
_LOG_PATCHED = False


def _log(msg: str) -> None:
    print(f"[RPA {datetime.now():%H:%M:%S}] {msg}", flush=True)


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


def _strip_action_timeouts(source: str) -> str:
    return _ACTION_TIMEOUT_RE.sub("", source)


def _inject_no_timeout_setup(source: str) -> str:
    if "set_default_timeout(0)" in source:
        return source
    source = _NEW_CONTEXT_RE.sub(r"\1" + _NO_TIMEOUT_BLOCK, source, count=1)
    source = _PERSISTENT_CONTEXT_RE.sub(r"\1" + _NO_TIMEOUT_BLOCK, source, count=1)
    return source


def _automate_file_upload(source: str, upload_abs: str) -> str:
    """
    SAP webgui upload: skip native file-browser dialog clicks and inject the real file path.
    Recorded flows often hang on #ls-inputfieldhelpbutton + OK waiting for a manual pick.
    """
    upload_abs = os.path.abspath(upload_abs)
    upload_literal = repr(upload_abs)

    source = re.sub(
        r'\.set_input_files\(\s*["\'][^"\']*["\']\s*\)',
        f".set_input_files({upload_literal})",
        source,
    )

    lines = source.splitlines()
    filtered = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if "#ls-inputfieldhelpbutton" in line:
            i += 1
            continue

        if 'get_by_role("button", name="OK")' in line:
            lookahead = "\n".join(lines[i + 1 : i + 5])
            if "set_input_files" in lookahead or "#webgui_filebrowser" in lookahead:
                i += 1
                continue

        if "set_input_files" in line:
            filtered.append(f'{line[: len(line) - len(line.lstrip())]}print("[RPA] Uploading:", {upload_literal})')

        filtered.append(line)
        i += 1

    return "\n".join(filtered)


def _inject_step_logging(source: str) -> str:
    """Print a line before each major Playwright action in the recorded script."""
    out = []
    for line in source.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("page.goto("):
            url = stripped.split("(", 1)[-1].rstrip(")").strip("\"'")
            indent = line[: len(line) - len(stripped)]
            out.append(f'{indent}print("[RPA] Navigate:", {url!r})')
        elif any(
            token in stripped
            for token in (
                ".click()",
                ".fill(",
                '.press("',
                "set_input_files",
                "expect_download",
            )
        ) and "print(" not in stripped:
            indent = line[: len(line) - len(stripped)]
            snippet = stripped[:100].replace('"', "'")
            out.append(f'{indent}print("[RPA] Step:", {snippet!r})')
        out.append(line)
    return "\n".join(out)


def prepare_script_source(source: str, upload_file: Optional[str] = None) -> str:
    source = _inject_no_timeout_setup(_strip_action_timeouts(source))
    if upload_file and os.path.isfile(upload_file):
        source = _automate_file_upload(source, upload_file)
    source = _inject_step_logging(source)
    return source


def _patch_playwright_no_timeout() -> None:
    global _PLAYWRIGHT_PATCHED
    if _PLAYWRIGHT_PATCHED:
        return

    from playwright.sync_api import Browser, BrowserType

    def _disable(ctx):
        ctx.set_default_timeout(0)
        ctx.set_default_navigation_timeout(0)
        return ctx

    _orig_new_context = Browser.new_context

    def new_context(self, *args, **kwargs):
        return _disable(_orig_new_context(self, *args, **kwargs))

    Browser.new_context = new_context

    _orig_persistent = BrowserType.launch_persistent_context

    def launch_persistent_context(self, *args, **kwargs):
        return _disable(_orig_persistent(self, *args, **kwargs))

    BrowserType.launch_persistent_context = launch_persistent_context
    _PLAYWRIGHT_PATCHED = True


def _patch_playwright_logging() -> None:
    global _LOG_PATCHED
    if _LOG_PATCHED:
        return

    from playwright.sync_api import Locator, Page

    def _wrap(cls, name):
        original = getattr(cls, name)

        def wrapper(self, *args, **kwargs):
            label = name
            if args and name in ("fill", "press"):
                label = f"{name} {args[0]!r}"
            elif args and name == "goto":
                label = f"goto {args[0]!r}"
            elif name == "set_input_files" and args:
                label = f"set_input_files {args[0]!r}"
            _log(f"{cls.__name__}.{label}")
            return original(self, *args, **kwargs)

        setattr(cls, name, wrapper)

    for method in ("goto",):
        _wrap(Page, method)
    for method in ("click", "fill", "press", "set_input_files"):
        _wrap(Locator, method)

    _LOG_PATCHED = True


def save_script(rpa_id: str, content: str) -> str:
    path = script_path(rpa_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(_inject_no_timeout_setup(_strip_action_timeouts(content)))
    return path


def launch_recorder(rpa_id: str, start_url: str) -> str:
    _validate_rpa_id(rpa_id)
    url = (start_url or "").strip()
    if not url:
        raise ValueError("Start URL is required to record.")

    path = script_path(rpa_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    if sys.platform == "win32":
        shell_cmd = (
            f'start "Playwright Codegen — {rpa_id}" '
            f'"{sys.executable}" -m playwright codegen '
            f'"{url}" -o "{path}" --target python -b chromium --channel chrome'
        )
        subprocess.Popen(shell_cmd, shell=True, cwd=config.BASE_DIR)
    else:
        subprocess.Popen(
            [
                sys.executable, "-m", "playwright", "codegen", url,
                "-o", path, "--target", "python", "-b", "chromium", "--channel", "chrome",
            ],
            cwd=config.BASE_DIR,
        )

    return path


def _resolve_recorded_path(recorded: str) -> str:
    if os.path.isabs(recorded):
        return os.path.normpath(recorded)
    return os.path.normpath(os.path.join(config.BASE_DIR, recorded))


def stage_upload_for_script(rpa_id: str, upload_path: str) -> list[str]:
    """Also copy mail file to recorded filename(s) as a fallback."""
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
        _log(f"Staged copy → {dest}")
    return staged


def run_recorded_script(rpa_id: str, upload_file: Optional[str] = None) -> None:
    os.environ.setdefault("NO_PROXY", "*")
    os.environ.setdefault("no_proxy", "*")

    path = script_path(rpa_id)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"No script for '{rpa_id}'. Open Record in the dashboard and save codegen output."
        )

    _log(f"Starting script: {rpa_id}")

    if upload_file and os.path.isfile(upload_file):
        upload_abs = os.path.abspath(upload_file)
        os.environ["RPA_UPLOAD_FILE"] = upload_abs
        _log(f"Upload file: {upload_abs} ({os.path.getsize(upload_abs)} bytes)")
        stage_upload_for_script(rpa_id, upload_abs)
    else:
        os.environ.pop("RPA_UPLOAD_FILE", None)
        _log("No upload file — set_input_files will use paths from the script as-is")

    _patch_playwright_no_timeout()
    _patch_playwright_logging()

    with open(path, encoding="utf-8") as f:
        raw = f.read()
    source = prepare_script_source(raw, upload_file=upload_file)

    _log("Launching browser...")
    code = compile(source, path, "exec")
    exec(code, {"__name__": "__main__", "__file__": path})
    _log(f"Script finished: {rpa_id}")
