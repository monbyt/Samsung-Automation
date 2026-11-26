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
_ACTION_TIMEOUT_RE = re.compile(r",?\s*timeout=\d+(?:_\d+)*")
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

# Transient browser network failures on d2cbi / Tableau hash navigations.
_GOTO_RETRY_HINTS = (
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_ABORTED",
    "ERR_CONNECTION_FAILED",
    "ERR_NETWORK_CHANGED",
    "ERR_TIMED_OUT",
    "ERR_EMPTY_RESPONSE",
    "ERR_HTTP2",
    "net::ERR_",
    "Timeout",
    "NS_ERROR_NET",
)


def _log(msg: str) -> None:
    print(f"[RPA {datetime.now():%H:%M:%S}] {msg}", flush=True)


def _chrome_executable() -> str:
    """Installed Google Chrome, not Playwright's bundled Chromium (needs PDF viewer)."""
    env = (os.environ.get("CHROME_PATH") or os.environ.get("RPA_CHROME_EXE") or "").strip()
    if env and os.path.isfile(env):
        return env
    roots = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    candidates = [
        os.path.join(r, "Google", "Chrome", "Application", "chrome.exe")
        for r in roots if r
    ]
    candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return ""


def _validate_rpa_id(rpa_id: str) -> str:
    if not _RPA_ID_RE.match(rpa_id or ""):
        raise ValueError(f"Invalid RPA id: {rpa_id!r}")
    return rpa_id


def script_path(rpa_id: str) -> str:
    _validate_rpa_id(rpa_id)
    return os.path.join(config.RPA_SCRIPTS_DIR, f"{rpa_id}.py")


def resolve_script_rpa_id(rpa_id: str) -> str:
    """Return the rpa_id whose .py file to run.

    BI view clones (traffic_*, traffic_wb_*) reuse the traffic_raw recording when
    they have no script of their own — only start_url differs per job.
    """
    if not rpa_id:
        return rpa_id
    own = script_path(rpa_id)
    if os.path.isfile(own):
        return rpa_id
    if rpa_id != "traffic_raw" and (rpa_id.startswith("traffic_") or rpa_id.startswith("traffic_wb_")):
        shared = script_path("traffic_raw")
        if os.path.isfile(shared):
            return "traffic_raw"
    return rpa_id


def has_script(rpa_id: str) -> bool:
    return os.path.isfile(script_path(resolve_script_rpa_id(rpa_id)))


def read_script(rpa_id: str) -> str:
    path = script_path(resolve_script_rpa_id(rpa_id))
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


def _inject_step_logging(source: str) -> str:
    """Print a line before each major Playwright action in the recorded script."""
    out = []
    for line in source.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("page.goto("):
            indent = line[: len(line) - len(stripped)]
            # Don't mis-log page.goto(get_nerp_url()) as Navigate: get_nerp_url(
            if "get_nerp_url" in stripped:
                out.append(f'{indent}print("[RPA] Navigate: active NERP URL (test/prod switch)")')
            else:
                url = stripped.split("(", 1)[-1].rstrip(")").strip("\"'")
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
            # Skip multi-line call tails like `).click()` — injecting a print
            # between get_by_role( and ).click() causes SyntaxError.
            if stripped.startswith(")") or stripped.startswith("."):
                out.append(line)
                continue
            indent = line[: len(line) - len(stripped)]
            snippet = stripped[:100].replace('"', "'")
            if "Password" in snippet or "User Account" in snippet:
                snippet = re.sub(r"\.fill\([^)]*\)", ".fill('[redacted]')", snippet)
            out.append(f'{indent}print("[RPA] Step:", {snippet!r})')
        out.append(line)
    return "\n".join(out)


def _inject_runtime_preamble(source: str, needs_upload: bool, needs_download: bool) -> str:
    """Imports for injected upload/download lines."""
    uses_rpa_os = "_rpa_os" in source
    if not needs_upload and not needs_download and not uses_rpa_os:
        return source
    if re.search(r"^import os as _rpa_os\s*$", source, re.MULTILINE):
        if needs_download and "_RPA_DL_SEQ" not in source:
            source = source.replace(
                "import os as _rpa_os\n",
                "import os as _rpa_os\nimport time as _rpa_time\n_RPA_DL_SEQ = [0]\n",
                1,
            )
        return source
    header = "import os as _rpa_os\n"
    if needs_download:
        header += "import time as _rpa_time\n_RPA_DL_SEQ = [0]\n"
    return header + "\n" + source


def _inject_nerp_url(source: str) -> str:
    """Rewrite hardcoded nerps/nerpsr FLP gotos to the active NERP_URL (test/prod)."""
    try:
        from mail.settings_db import get_nerp_url
        url = get_nerp_url()
    except Exception:
        url = getattr(config, "NERP_URL", "") or ""
    if not url:
        return source
    # Match either host and any FLP hash fragment recorded in scripts.
    return re.sub(
        r"https://nerpsr?\.sec\.samsung\.net/sap/bc/ui2/flp#[^\s\"']+",
        url,
        source,
    )


_OTP_FIELD_HINT_RE = re.compile(
    r"verification|one[\s-]?time|otp|passcode|authenticator|"
    r"security code|mfa|2fa|two[\s-]?factor|totp",
    re.I,
)


def _line_looks_like_otp_field(stripped: str) -> bool:
    if _OTP_FIELD_HINT_RE.search(stripped):
        return True
    # Common Playwright get_by_role / get_by_label names for MFA boxes.
    for needle in (
        'name="OTP"',
        "name='OTP'",
        'name="Code"',
        "name='Code'",
        'name="Token"',
        "name='Token'",
    ):
        if needle in stripped:
            return True
    return False


_USERNAME_FIELD_RE = re.compile(
    r'name=(["\'])(?:User Account|Username|Email Address|E-mail)\1',
    re.I,
)

# One-time OpenID auth URLs die after one use — rewrite to the Tableau view.
_MFA_AUTH_GOTO_RE = re.compile(
    r"""page\.goto\(\s*(['"])https?://mfa-d2cbi\.sec\.samsung\.net/[^'"]*\1\s*\)""",
    re.I,
)
# Recorded Traffic Raw / workbook gotos — swap to this job's start_url.
_D2CBI_DEST_GOTO_RE = re.compile(
    r"""page\.goto\(\s*(['"])https?://d2cbi\.sec\.samsung\.net/[^'"]*\1\s*\)""",
    re.I,
)
_TRAFFIC_VIEW_DEFAULT = (
    "https://d2cbi.sec.samsung.net/#/views/03_ALLDIV/03-7_TrafficRaw"
    "?:embed=n&:iid=1&:origin=card_share_link"
)


def _strip_capslock_noise(source: str) -> str:
    """Codegen often records CapsLock presses while typing a password — drop them."""
    out = []
    for line in source.splitlines():
        if re.search(r'\.press\(\s*([\'"])CapsLock\1\s*\)', line):
            continue
        out.append(line)
    return "\n".join(out)


def _rewrite_mfa_auth_gotos(source: str, start_url: str = "") -> str:
    target = (start_url or "").strip() or _TRAFFIC_VIEW_DEFAULT
    if not _MFA_AUTH_GOTO_RE.search(source):
        return source
    replaced = _MFA_AUTH_GOTO_RE.sub(
        lambda m: f'page.goto({m.group(1)}{target}{m.group(1)})',
        source,
    )
    if replaced != source:
        _log(f"Rewrote mfa-d2cbi one-time auth goto → {target!r}")
    return replaced


def _rewrite_d2cbi_dest_gotos(source: str, start_url: str = "") -> str:
    """Point recorded d2cbi view/workbook gotos at this job's start_url."""
    target = (start_url or "").strip()
    if not target or "d2cbi.sec.samsung.net" not in target.lower():
        return source
    if not _D2CBI_DEST_GOTO_RE.search(source):
        return source
    replaced = _D2CBI_DEST_GOTO_RE.sub(
        lambda m: f'page.goto({m.group(1)}{target}{m.group(1)})',
        source,
    )
    if replaced != source:
        _log(f"Rewrote d2cbi destination goto → {target!r}")
    return replaced


def _rewrite_traffic_view_constant(source: str, start_url: str = "") -> str:
    """Override TRAFFIC_VIEW = (...) so workbook clones open their own start_url."""
    target = (start_url or "").strip()
    if not target or "TRAFFIC_VIEW" not in source:
        return source
    if "d2cbi.sec.samsung.net" not in target.lower():
        return source
    replaced, n = re.subn(
        r"^TRAFFIC_VIEW\s*=\s*\([\s\S]*?\)\s*$",
        f"TRAFFIC_VIEW = {target!r}",
        source,
        count=1,
        flags=re.MULTILINE,
    )
    if n:
        _log(f"Rewrote TRAFFIC_VIEW constant → {target!r}")
    return replaced


def _inject_sso_credentials(source: str) -> str:
    """Rewrite User Account / Username / Password / OTP fills to RPA_* values."""
    out = []
    pending = None  # "user" | "pass" | "otp" | None
    for line in source.splitlines():
        stripped = line.lstrip()
        if _USERNAME_FIELD_RE.search(stripped):
            if ".fill(" in stripped:
                line = re.sub(r'\.fill\((["\']).*?\1\)', ".fill(RPA_USERNAME)", line)
                pending = None
            else:
                pending = "user"
        elif 'name="Password"' in stripped or "name='Password'" in stripped:
            if ".fill(" in stripped:
                line = re.sub(r'\.fill\((["\']).*?\1\)', ".fill(RPA_PASSWORD)", line)
                pending = None
            else:
                pending = "pass"
        elif _line_looks_like_otp_field(stripped):
            if ".fill(" in stripped:
                # Call at fill time so the code is fresh (30s window).
                line = re.sub(r'\.fill\((["\']).*?\1\)', ".fill(RPA_OTP())", line)
                pending = None
            else:
                pending = "otp"
        elif pending and ".fill(" in stripped and "print(" not in stripped:
            if pending == "otp":
                line = re.sub(r'\.fill\((["\']).*?\1\)', ".fill(RPA_OTP())", line)
            else:
                token = "RPA_USERNAME" if pending == "user" else "RPA_PASSWORD"
                line = re.sub(r'\.fill\((["\']).*?\1\)', f".fill({token})", line)
            pending = None
        elif stripped and not stripped.startswith("#"):
            # clicked something else — clear pending intent
            if pending and not stripped.startswith("page.") and ".fill(" not in stripped:
                pass
            elif pending and ".click(" not in stripped:
                pending = None
        out.append(line)
    return "\n".join(out)


_SALES_ORG_FILL_RE = re.compile(r'\.fill\(\s*(["\'])(7101|7104)\1\s*\)')


def _inject_sales_org(source: str) -> str:
    """Rewrite recorded Sales Org fills (7101/7104) to RPA_SALES_ORG from the Excel."""
    out = []
    pending = False
    for line in source.splitlines():
        stripped = line.lstrip()
        looks_like_sales_org = (
            "Sales Org" in stripped
            or "name='Sales Org" in stripped
            or 'name="Sales Org' in stripped
        )
        if looks_like_sales_org:
            if ".fill(" in stripped:
                line = _SALES_ORG_FILL_RE.sub(".fill(RPA_SALES_ORG)", line)
                pending = False
            else:
                pending = True
        elif pending and ".fill(" in stripped and "print(" not in stripped:
            line = _SALES_ORG_FILL_RE.sub(".fill(RPA_SALES_ORG)", line)
            pending = False
        elif stripped and not stripped.startswith("#"):
            if pending and ".click(" not in stripped and ".fill(" not in stripped:
                pending = False
        out.append(line)
    return "\n".join(out)


def prepare_script_source(
    source: str,
    upload_file: Optional[str] = None,
    download_dir: Optional[str] = None,
    start_url: str = "",
) -> str:
    from rpa.debug_log import debug_log

    source = _inject_no_timeout_setup(_strip_action_timeouts(source))
    source = _strip_capslock_noise(source)
    source = _sanitize_upload_literals(source)
    source = _inject_nerp_url(source)
    source = _rewrite_mfa_auth_gotos(source, start_url=start_url)
    source = _rewrite_d2cbi_dest_gotos(source, start_url=start_url)
    source = _rewrite_traffic_view_constant(source, start_url=start_url)
    source = _inject_sso_credentials(source)
    source = _inject_sales_org(source)
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
    needs_download = bool(
        download_dir and re.search(r"download\d*_info\.value", source)
    )
    source = _inject_step_logging(source)
    if needs_upload:
        source = _automate_file_upload(source, upload_file)
    if needs_download:
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
    if not re.search(r"download\d*_info\.value", source):
        return source

    out = []
    for line in source.splitlines():
        out.append(line)
        m = re.match(r"\s*(download\d*)\s*=\s*\1_info\.value\s*$", line)
        if m:
            var = m.group(1)
            indent = line[: len(line) - len(line.lstrip())]
            # SAP/browser often reuses the same suggested_filename (e.g. document.pdf),
            # which would overwrite earlier PDFs and leave only one file to attach.
            out.append(f"{indent}_rpa_os.makedirs(RPA_DOWNLOAD_DIR, exist_ok=True)")
            out.append(
                f"{indent}_rpa_base, _rpa_ext = _rpa_os.path.splitext("
                f"{var}.suggested_filename or 'download.bin')"
            )
            out.append(f"{indent}_RPA_DL_SEQ[0] += 1")
            out.append(
                f"{indent}_rpa_dl = _rpa_os.path.join("
                f"RPA_DOWNLOAD_DIR, "
                f"'%s_%03d_%d%s' % (_rpa_base, _RPA_DL_SEQ[0], "
                f"int(_rpa_time.time() * 1000), _rpa_ext))"
            )
            out.append(f"{indent}{var}.save_as(_rpa_dl)")
            out.append(f'{indent}print("[RPA] Saved download:", _rpa_dl)')
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
        return _disable(_orig_new_context(self, *args, **kwargs))

    Browser.new_context = new_context

    _orig_launch = BrowserType.launch

    def launch(self, *args, **kwargs):
        # Never use installed Google Chrome (channel="chrome"). That executable
        # has a process singleton: the 2nd worker waits until the 1st closes.
        # Playwright's bundled Chromium can run many windows at once.
        kwargs = dict(kwargs)
        kwargs.pop("channel", None)
        extra = list(kwargs.get("args") or [])
        extra.extend([
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
        ])
        port = (os.environ.get("RPA_CHROME_DEBUG_PORT") or "").strip()
        if port and not any(str(a).startswith("--remote-debugging-port") for a in extra):
            extra.append(f"--remote-debugging-port={port}")
        kwargs["args"] = extra
        kwargs.setdefault("headless", False)
        _log(f"Playwright Chromium launch (not installed Chrome) port={port or '-'}")
        return _orig_launch(self, *args, **kwargs)

    BrowserType.launch = launch

    _orig_persistent = BrowserType.launch_persistent_context

    def launch_persistent_context(self, *args, **kwargs):
        kwargs = dict(kwargs)
        kwargs.pop("channel", None)
        profile = (os.environ.get("RPA_CHROME_PROFILE") or "").strip()
        if profile:
            os.makedirs(profile, exist_ok=True)
            if args:
                args = (profile,) + tuple(args[1:])
            else:
                kwargs["user_data_dir"] = profile
        _log(f"Playwright Chromium persistent profile={profile or kwargs.get('user_data_dir')}")
        return _disable(_orig_persistent(self, *args, **kwargs))

    BrowserType.launch_persistent_context = launch_persistent_context
    _PLAYWRIGHT_PATCHED = True


def _fill_is_secret(locator, value) -> bool:
    loc = str(locator)
    if re.search(r"Password|User Account|Username|One-time|OTP", loc, re.I):
        return True
    if not isinstance(value, str) or len(value) < 3:
        return False
    try:
        from rpa.hang_alert import _secret_values
        return value in set(_secret_values())
    except Exception:
        secrets = {
            os.environ.get("RPA_USERNAME", ""),
            os.environ.get("RPA_PASSWORD", ""),
            getattr(config, "NERP_USERNAME", "") or "",
            getattr(config, "NERP_PASSWORD", "") or "",
        }
        return value in {s for s in secrets if s}


def _patch_playwright_logging() -> None:
    global _LOG_PATCHED
    if _LOG_PATCHED:
        return

    import traceback

    from playwright.sync_api import Locator, Page
    from rpa.debug_log import debug_log

    def _snap_failure_screen(target) -> None:
        try:
            page = getattr(target, "page", None) or target
            from rpa.hang_alert import save_page_screenshot
            save_page_screenshot(page)
        except Exception:
            pass

    def _is_transient_goto(exc: BaseException) -> bool:
        msg = str(exc) or ""
        return any(h in msg for h in _GOTO_RETRY_HINTS)

    def _wrap(cls, name):
        original = getattr(cls, name)

        def wrapper(self, *args, **kwargs):
            label = name
            if args and name in ("fill", "press"):
                shown = args[0]
                if name == "fill" and _fill_is_secret(self, shown):
                    shown = "[redacted]"
                label = f"{name} {shown!r}"
            elif args and name == "goto":
                label = f"goto {args[0]!r}"
            elif name == "set_input_files" and args:
                label = f"set_input_files {args[0]!r}"
            _log(f"{cls.__name__}.{label}")
            if name == "goto":
                # d2cbi Tableau hash views often reset the TCP connection on
                # wait_until="load". Prefer domcontentloaded + retry.
                opts = dict(kwargs)
                url = str(args[0]) if args else ""
                if "d2cbi.sec.samsung.net" in url.lower():
                    opts.setdefault("wait_until", "domcontentloaded")
                    opts.setdefault("timeout", 120_000)
                attempts = 4 if "d2cbi.sec.samsung.net" in url.lower() else 1
                last_exc: Optional[BaseException] = None
                for i in range(1, attempts + 1):
                    try:
                        return original(self, *args, **opts)
                    except Exception as exc:
                        last_exc = exc
                        if i >= attempts or not _is_transient_goto(exc):
                            _snap_failure_screen(self)
                            raise
                        wait_ms = min(8000, 1500 * i)
                        _log(
                            f"Page.goto transient failure {i}/{attempts}: {exc} "
                            f"— retry in {wait_ms}ms"
                        )
                        try:
                            self.wait_for_timeout(wait_ms)
                        except Exception:
                            import time as _time
                            _time.sleep(wait_ms / 1000.0)
                if last_exc:
                    raise last_exc
                return None
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
            try:
                return original(self, *args, **kwargs)
            except Exception:
                _snap_failure_screen(self)
                raise

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
        try:
            from mail.mail_meta import copy_mail_meta
            copy_mail_meta(upload_path, dest)
        except Exception:
            pass
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

    script_id = resolve_script_rpa_id(rpa_id)
    path = script_path(script_id)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"No script for '{rpa_id}'"
            + (f" (shared '{script_id}')" if script_id != rpa_id else "")
            + ". Open Record in the dashboard and save codegen output "
            "(or record traffic_raw for BI workbook clones)."
        )

    os.environ.setdefault("RPA_CHROME_DEBUG_PORT", "9329")

    _log(f"Starting script: {rpa_id}" + (f" → {script_id}.py" if script_id != rpa_id else ""))
    if upload_dir:
        _log(f"Upload folder: {upload_dir}")
        os.environ["RPA_UPLOAD_DIR"] = upload_dir
    if download_dir:
        _log(f"Download folder: {download_dir}")
        os.environ["RPA_DOWNLOAD_DIR"] = download_dir

    if upload_file and os.path.isfile(upload_file):
        upload_abs = os.path.abspath(upload_file)
        if upload_dir:
            # Parallel workers: keep the file inside the isolated upload dir.
            # Never overwrite a shared recorded LAYOUT path in the repo.
            os.makedirs(upload_dir, exist_ok=True)
            dest = os.path.join(upload_dir, os.path.basename(upload_abs))
            if os.path.abspath(dest) != upload_abs:
                shutil.copy2(upload_abs, dest)
            try:
                from mail.mail_meta import copy_mail_meta
                copy_mail_meta(upload_abs, dest)
            except Exception:
                pass
            sap_upload = dest
            staged = [dest]
        else:
            staged = stage_upload_for_script(script_id, upload_abs)
            sap_upload = staged[0] if staged else upload_abs
        os.environ["RPA_UPLOAD_FILE"] = sap_upload
        _log(f"Upload file: {sap_upload} ({os.path.getsize(sap_upload)} bytes)")
        if staged:
            _log(f"Staged upload copy: {os.path.basename(sap_upload)}")
    else:
        os.environ.pop("RPA_UPLOAD_FILE", None)
        _log("No upload file — set_input_files uses script paths or win_open_file()")

    _patch_playwright_no_timeout()
    _patch_file_chooser_upload()
    _patch_playwright_logging()

    with open(path, encoding="utf-8") as f:
        raw = f.read()
    sap_upload = os.environ.get("RPA_UPLOAD_FILE") or None
    start_url = ""
    try:
        from rpa.jobs_db import get_rpa_job
        job = get_rpa_job(rpa_id) or {}
        start_url = (job.get("start_url") or "").strip()
    except Exception:
        start_url = ""
    source = prepare_script_source(
        raw,
        upload_file=sap_upload or upload_file,
        download_dir=download_dir,
        start_url=start_url,
    )

    from win_file_dialog import dismiss_open_file_dialog, dismiss_save_as_dialog
    from mail.settings_db import (
        get_nerp_env,
        get_nerp_url,
        get_sso_password,
        get_sso_username,
        get_tableau_password,
        get_tableau_username,
    )
    from rpa.excel_meta import detect_sales_org

    nerp_url = get_nerp_url()
    sales_org = detect_sales_org(sap_upload or upload_file)
    _log(f"NERP environment: {get_nerp_env()} → {nerp_url}")
    _log(f"Sales Org from Excel: {sales_org}")

    # Tableau / d2cbi jobs use a different password than NERP.
    use_tableau_login = (
        (rpa_id or "").startswith("traffic")
        or "d2cbi" in (start_url or "").lower()
        or "tableau" in (rpa_id or "").lower()
    )
    if use_tableau_login:
        rpa_user = get_tableau_username() or get_sso_username() or config.NERP_USERNAME
        rpa_pass = get_tableau_password()
        if not rpa_pass:
            raise RuntimeError(
                "Tableau / d2cbi password not set — open Settings → "
                "Tableau / d2cbi login and save it (separate from NERP)."
            )
        _log("Using Tableau / d2cbi credentials from Settings")
    else:
        rpa_user = get_sso_username() or config.NERP_USERNAME
        rpa_pass = get_sso_password() or config.NERP_PASSWORD

    def _rpa_otp() -> str:
        """Fresh TOTP at fill time (codes expire every 30s)."""
        from mail.otp import current_code
        code = current_code()
        if not code:
            raise RuntimeError(
                "MFA OTP secret not set — add it under Settings → MFA OTP"
            )
        return code

    run_globals = {
        "__name__": "__main__",
        "__file__": path,
        "_rpa_os": os,
        "RPA_UPLOAD_FILE": os.environ.get("RPA_UPLOAD_FILE", ""),
        "RPA_UPLOAD_DIR": upload_dir or "",
        "RPA_DOWNLOAD_DIR": download_dir or "",
        "RPA_NERP_URL": nerp_url,
        "RPA_SALES_ORG": sales_org,
        "RPA_USERNAME": rpa_user,
        "RPA_PASSWORD": rpa_pass,
        "RPA_OTP": _rpa_otp,
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
