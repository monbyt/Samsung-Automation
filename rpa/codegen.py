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
_FILECHOOSER_PATCHED = False


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


def _sanitize_upload_literals(source: str) -> str:
    """Fix saved scripts that embed Windows paths like C:\\Users (breaks compile)."""
    def fix(m):
        inner = m.group(1)
        if re.match(r"[A-Za-z]:", inner) or inner.startswith("\\\\"):
            inner = os.path.basename(inner.replace("\\", "/"))
        return f".set_input_files({inner!r})"

    return re.sub(r'\.set_input_files\(\s*["\']([^"\']*)["\']\s*\)', fix, source)


def _inject_post_upload_lines(indent: str) -> list[str]:
    """Wait for SAP to accept upload and dismiss error popups before Execute."""
    return [
        f'{indent}print("[RPA] Waiting for SAP after upload...")',
        f"{indent}_rpa_shell = page.locator('iframe[name=\"application-Shell-startGUI-iframe\"]').content_frame",
        f"{indent}try:",
        f'{indent}    _rpa_shell.get_by_role("dialog", name="Error").get_by_label("Close").click(timeout=5000)',
        f'{indent}    print("[RPA] Dismissed SAP error dialog")',
        f"{indent}except Exception:",
        f"{indent}    pass",
        f'{indent}_rpa_shell.get_by_role("button", name="Execute  Emphasized").wait_for(state="visible", timeout=60000)',
        f'{indent}print("[RPA] Execute button ready")',
    ]


def _automate_file_upload(source: str, upload_abs: str) -> str:
    """
    SAP upload via Chrome file picker: wrap the OK click (after help button) with
    expect_file_chooser so the file is selected at the right moment.
    Skips set_input_files to avoid double-upload / data errors.
    """
    lines = source.splitlines()
    out: list[str] = []
    i = 0
    used_chooser = False

    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()

        if (
            not used_chooser
            and 'get_by_role("button", name="OK")' in line
            and ".click()" in line
            and "page." in line
        ):
            ahead = "\n".join(lines[i + 1 : i + 8])
            if "set_input_files" in ahead:
                indent = line[: len(line) - len(stripped)]
                inner = indent + "    "
                out.append(f'{indent}print("[RPA] Uploading:", RPA_UPLOAD_FILE)')
                out.append(f"{indent}with page.expect_file_chooser() as _rpa_fc_info:")
                out.append(inner + stripped)
                out.append(f"{inner}_rpa_fc_info.value.set_files(RPA_UPLOAD_FILE)")
                out.extend(_inject_post_upload_lines(indent))
                used_chooser = True
                i += 1
                while i < len(lines) and "set_input_files" not in lines[i]:
                    out.append(lines[i])
                    i += 1
                if i < len(lines) and "set_input_files" in lines[i]:
                    i += 1
                continue

        out.append(line)
        i += 1

    if used_chooser:
        return "\n".join(out)

    source = re.sub(
        r'\.set_input_files\(\s*["\'][^"\']*["\']\s*\)',
        ".set_input_files(RPA_UPLOAD_FILE)",
        source,
    )
    out = []
    for line in source.splitlines():
        if "set_input_files(RPA_UPLOAD_FILE)" in line:
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f'{indent}print("[RPA] Uploading:", RPA_UPLOAD_FILE)')
            out.append(line)
            out.extend(_inject_post_upload_lines(indent))
        else:
            out.append(line)
    return "\n".join(out)


def _automate_download_step(source: str) -> str:
    """
    Fix SAP download step: wait for button, use expect_download with a timeout,
    and keep the click properly indented inside the with block.
    """
    if "expect_download" not in source or "download_info" not in source:
        return source

    lines = source.splitlines()
    out: list[str] = []
    i = 0
    shell_declared = "_rpa_shell" in source

    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()

        if "with page.expect_download()" in stripped and "download_info" in stripped:
            indent = line[: len(line) - len(stripped)]
            inner = indent + "    "
            out.append(f'{indent}print("[RPA] Preparing download...")')
            if not shell_declared:
                out.append(
                    f"{indent}_rpa_shell = page.locator('iframe[name=\"application-Shell-startGUI-iframe\"]').content_frame"
                )
                shell_declared = True
            out.append(f'{indent}_rpa_dl_btn = _rpa_shell.get_by_role("button", name="Download Result Data")')
            out.append(f'{indent}_rpa_dl_btn.wait_for(state="visible", timeout=120000)')
            out.append(f'{indent}print("[RPA] Download Result Data button ready")')
            out.append(f"{indent}with page.expect_download(timeout=120000) as download_info:")
            i += 1
            while i < len(lines):
                inner_line = lines[i]
                inner_stripped = inner_line.lstrip()
                if not inner_stripped:
                    i += 1
                    continue
                if inner_stripped.startswith("print("):
                    i += 1
                    continue
                if ".click()" in inner_stripped and "Download Result Data" in inner_stripped:
                    out.append(inner + inner_stripped)
                    out.append(f'{inner}print("[RPA] Download Result Data clicked")')
                    i += 1
                    break
                if ".click()" in inner_stripped:
                    out.append(inner + inner_stripped)
                    out.append(f'{inner}print("[RPA] Download click")')
                    i += 1
                    break
                i += 1
            continue

        out.append(line)
        i += 1

    return "\n".join(out)


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


def _inject_runtime_preamble(source: str, needs_upload: bool, needs_download: bool) -> str:
    """Imports for injected upload/download lines."""
    uses_rpa_os = "_rpa_os" in source
    if not needs_upload and not needs_download and not uses_rpa_os:
        return source
    if re.search(r"^import os as _rpa_os\s*$", source, re.MULTILINE):
        return source
    return "import os as _rpa_os\n\n" + source


def prepare_script_source(
    source: str,
    upload_file: Optional[str] = None,
    download_dir: Optional[str] = None,
) -> str:
    from rpa.debug_log import debug_log

    source = _inject_no_timeout_setup(_strip_action_timeouts(source))
    source = _sanitize_upload_literals(source)
    needs_upload = bool(upload_file and os.path.isfile(upload_file))
    raw_upload_lines = [ln.strip() for ln in source.splitlines() if "set_input_files" in ln]
    # region agent log
    debug_log(
        "H4",
        "codegen.py:prepare_script_source:before",
        "upload transform input",
        {
            "needs_upload": needs_upload,
            "upload_file": upload_file,
            "raw_set_input_files_lines": raw_upload_lines[:5],
            "has_help_button": "ls-inputfieldhelpbutton" in source,
            "has_webgui_upload": "webgui_filebrowser_file_upload" in source,
        },
    )
    # endregion
    needs_download = "expect_download" in source and "download_info" in source
    source = _inject_step_logging(source)
    if needs_upload:
        source = _automate_file_upload(source, upload_file)
    if needs_download:
        source = _automate_download_step(source)
    if needs_download and download_dir:
        source = _inject_download_save(source)
    transformed_upload_lines = [ln.strip() for ln in source.splitlines() if "set_input_files" in ln or "Uploading:" in ln or "expect_file_chooser" in ln]
    # region agent log
    debug_log(
        "H4",
        "codegen.py:prepare_script_source:after",
        "upload transform output",
        {
            "transformed_upload_lines": transformed_upload_lines[:8],
            "uses_rpa_upload_file": "RPA_UPLOAD_FILE" in source,
        },
    )
    # endregion
    return _inject_runtime_preamble(source, needs_upload, needs_download)


def _inject_download_save(source: str) -> str:
    """Auto-save Playwright downloads to RPA_DOWNLOAD_DIR (no path literals in source)."""
    if "download_info.value" not in source:
        return source

    out = []
    for line in source.splitlines():
        if re.match(r"\s*download\s*=\s*download_info\.value\s*$", line):
            indent = line[: len(line) - len(line.lstrip())]
            inner = indent + "    "
            out.append(f"{indent}try:")
            out.append(f"{inner}download = download_info.value")
            out.append(f"{indent}except Exception as _rpa_dl_err:")
            out.append(f'{inner}print("[RPA] No browser download event:", _rpa_dl_err)')
            out.append(f"{inner}win_save_as(RPA_DOWNLOAD_DIR)")
            out.append(f"{inner}download = None")
            out.append(f"{indent}if download:")
            out.append(f"{inner}_rpa_os.makedirs(RPA_DOWNLOAD_DIR, exist_ok=True)")
            out.append(
                f"{inner}_rpa_dl = _rpa_os.path.join(RPA_DOWNLOAD_DIR, download.suggested_filename)"
            )
            out.append(f"{inner}download.save_as(_rpa_dl)")
            out.append(f'{inner}print("[RPA] Saved download:", _rpa_dl)')
            continue
        out.append(line)
    return "\n".join(out)


def _upload_file_path() -> str:
    path = os.environ.get("RPA_UPLOAD_FILE", "")
    return path if path and os.path.isfile(path) else ""


def _bind_file_chooser(page) -> None:
    """Disabled — upload uses expect_file_chooser on OK click to avoid early picks."""
    return


def _bind_file_chooser_context(context) -> None:
    def _on_page(page):
        _bind_file_chooser(page)

    context.on("page", _on_page)
    for page in context.pages:
        _on_page(page)


def _start_win_open_fallback() -> None:
    """Background fallback when Playwright cannot intercept the native Open dialog."""
    if sys.platform != "win32":
        return
    upload_path = _upload_file_path()
    if not upload_path:
        return

    import threading

    def _run():
        import time

        from win_file_dialog import dismiss_open_file_dialog

        time.sleep(0.4)
        dismiss_open_file_dialog(
            directory=os.environ.get("RPA_UPLOAD_DIR") or os.path.dirname(upload_path),
            filename=os.path.basename(upload_path),
            timeout=90,
        )

    threading.Thread(target=_run, daemon=True).start()


def _start_win_save_fallback() -> None:
    """Background fallback when SAP opens native Save As instead of a browser download."""
    if sys.platform != "win32":
        return
    download_dir = os.environ.get("RPA_DOWNLOAD_DIR", "")
    if not download_dir:
        return

    import threading

    def _run():
        import time

        from win_file_dialog import dismiss_save_as_dialog

        time.sleep(0.5)
        dismiss_save_as_dialog(directory=download_dir, timeout=90)

    threading.Thread(target=_run, daemon=True).start()


def _patch_file_chooser_upload() -> None:
    global _FILECHOOSER_PATCHED
    if _FILECHOOSER_PATCHED or not _upload_file_path():
        return
    _FILECHOOSER_PATCHED = True
    _log(f"File picker auto-upload enabled → {_upload_file_path()}")


def _patch_playwright_no_timeout() -> None:
    global _PLAYWRIGHT_PATCHED
    if _PLAYWRIGHT_PATCHED:
        return

    from playwright.sync_api import Browser, BrowserType

    def _disable(ctx):
        ctx.set_default_timeout(0)
        ctx.set_default_navigation_timeout(0)
        _bind_file_chooser_context(ctx)
        return ctx

    _orig_new_context = Browser.new_context

    def new_context(self, *args, **kwargs):
        kwargs.setdefault("accept_downloads", True)
        return _disable(_orig_new_context(self, *args, **kwargs))

    Browser.new_context = new_context

    _orig_persistent = BrowserType.launch_persistent_context

    def launch_persistent_context(self, *args, **kwargs):
        kwargs.setdefault("accept_downloads", True)
        return _disable(_orig_persistent(self, *args, **kwargs))

    BrowserType.launch_persistent_context = launch_persistent_context
    _PLAYWRIGHT_PATCHED = True


def _patch_playwright_logging() -> None:
    global _LOG_PATCHED
    if _LOG_PATCHED:
        return

    import traceback

    from playwright.sync_api import Locator, Page
    from rpa.debug_log import debug_log

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
            if name == "set_input_files":
                upload_path = args[0] if args else ""
                # region agent log
                debug_log(
                    "H2",
                    "codegen.py:set_input_files:before",
                    "set_input_files call",
                    {
                        "path": upload_path,
                        "path_exists": bool(upload_path and os.path.isfile(str(upload_path))),
                        "locator": str(self)[:200],
                    },
                )
                # endregion
                opts = dict(kwargs)
                opts.setdefault("timeout", 15_000)
                try:
                    result = original(self, *args, **opts)
                    # region agent log
                    debug_log("H2", "codegen.py:set_input_files:after", "set_input_files ok", {"path": upload_path})
                    # endregion
                    return result
                except Exception as exc:
                    # region agent log
                    debug_log(
                        "H2",
                        "codegen.py:set_input_files:error",
                        "set_input_files failed",
                        {"path": upload_path, "error": str(exc), "trace": traceback.format_exc()[-400:]},
                    )
                    # endregion
                    _log(f"set_input_files skipped ({exc}) — file picker may have handled upload")
                    return None
            if name == "click":
                snippet = str(self)[:200]
                # region agent log
                if any(k in snippet for k in ("Upload file", "ls-inputfieldhelpbutton", "webgui_filebrowser", "OK")):
                    debug_log("H3", "codegen.py:click", "upload-related click", {"locator": snippet})
                # endregion
                if _upload_file_path() and 'get_by_role("button", name="OK")' in snippet:
                    _start_win_open_fallback()
                if "Download Result Data" in snippet:
                    _start_win_save_fallback()
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
        f.write(_sanitize_upload_literals(_inject_no_timeout_setup(_strip_action_timeouts(content))))
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


def run_recorded_script(
    rpa_id: str,
    upload_file: Optional[str] = None,
    upload_dir: Optional[str] = None,
    download_dir: Optional[str] = None,
) -> None:
    os.environ.setdefault("NO_PROXY", "*")
    os.environ.setdefault("no_proxy", "*")

    path = script_path(rpa_id)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"No script for '{rpa_id}'. Open Record in the dashboard and save codegen output."
        )

    _log(f"Starting script: {rpa_id}")
    if upload_dir:
        _log(f"Upload folder: {upload_dir}")
        os.environ["RPA_UPLOAD_DIR"] = upload_dir
    if download_dir:
        _log(f"Download folder: {download_dir}")
        os.environ["RPA_DOWNLOAD_DIR"] = download_dir

    if upload_file and os.path.isfile(upload_file):
        upload_abs = os.path.abspath(upload_file)
        staged = stage_upload_for_script(rpa_id, upload_abs)
        sap_upload = staged[0] if staged else upload_abs
        os.environ["RPA_UPLOAD_FILE"] = sap_upload
        _log(f"Upload file: {sap_upload} ({os.path.getsize(sap_upload)} bytes)")
        if staged:
            _log(f"Staged as recorded filename: {os.path.basename(sap_upload)}")
    else:
        os.environ.pop("RPA_UPLOAD_FILE", None)
        _log("No upload file — set_input_files uses script paths or win_open_file()")

    _patch_playwright_no_timeout()
    _patch_file_chooser_upload()
    _patch_playwright_logging()

    with open(path, encoding="utf-8") as f:
        raw = f.read()
    sap_upload = os.environ.get("RPA_UPLOAD_FILE") or None
    source = prepare_script_source(
        raw,
        upload_file=sap_upload or upload_file,
        download_dir=download_dir,
    )

    from win_file_dialog import dismiss_open_file_dialog, dismiss_save_as_dialog

    run_globals = {
        "__name__": "__main__",
        "__file__": path,
        "_rpa_os": os,
        "RPA_UPLOAD_FILE": os.environ.get("RPA_UPLOAD_FILE", ""),
        "RPA_UPLOAD_DIR": upload_dir or "",
        "RPA_DOWNLOAD_DIR": download_dir or "",
        "win_open_file": dismiss_open_file_dialog,
        "win_save_as": dismiss_save_as_dialog,
    }

    from rpa.debug_log import debug_log

    # region agent log
    debug_log(
        "H1",
        "codegen.py:run_recorded_script",
        "exec start",
        {
            "rpa_id": rpa_id,
            "RPA_UPLOAD_FILE": run_globals["RPA_UPLOAD_FILE"],
            "RPA_UPLOAD_DIR": run_globals["RPA_UPLOAD_DIR"],
            "upload_file_exists": bool(run_globals["RPA_UPLOAD_FILE"] and os.path.isfile(run_globals["RPA_UPLOAD_FILE"])),
        },
    )
    # endregion

    _log("Launching browser...")
    code = compile(source, f"<rpa:{rpa_id}>", "exec")
    try:
        exec(code, run_globals)
    except Exception as exc:
        import traceback

        # region agent log
        debug_log(
            "H5",
            "codegen.py:run_recorded_script:error",
            "script exec failed",
            {"rpa_id": rpa_id, "error": str(exc), "trace": traceback.format_exc()[-600:]},
        )
        # endregion
        raise
    # region agent log
    debug_log("H5", "codegen.py:run_recorded_script", "exec finished", {"rpa_id": rpa_id})
    # endregion
    _log(f"Script finished: {rpa_id}")
