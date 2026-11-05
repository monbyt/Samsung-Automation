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


def _inject_sso_credentials(source: str) -> str:
    """Rewrite hardcoded User Account / Password fills to use RPA_USERNAME / RPA_PASSWORD."""
    out = []
    pending = None  # "user" | "pass" | None
    for line in source.splitlines():
        stripped = line.lstrip()
        if 'name="User Account"' in stripped or "name='User Account'" in stripped:
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
        elif pending and ".fill(" in stripped and "print(" not in stripped:
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


def _sticky_page_bind_lines(indent: str) -> list[str]:
    """Pick a signed-in Tableau tab — pages[0] is often a stale MFA tab."""
    return [
        f"{indent}page = None\n",
        f"{indent}for _rpa_p in list(context.pages):\n",
        f"{indent}    _rpa_u = (_rpa_p.url or \"\")\n",
        f"{indent}    if (\"d2cbi.sec.samsung.net\" in _rpa_u\n",
        f"{indent}            and \"mfa-\" not in _rpa_u\n",
        f"{indent}            and \"openid-connect\" not in _rpa_u):\n",
        f"{indent}        page = _rpa_p\n",
        f"{indent}        break\n",
        f"{indent}if page is None:\n",
        f"{indent}    page = context.pages[0] if context.pages else context.new_page()\n",
        f'{indent}print("[RPA] Bound page:", ((page.url or ""))[:160])\n',
        f"{indent}try:\n",
        f"{indent}    _rpa_cks = [c for c in context.cookies() if \"samsung\" in (c.get(\"domain\") or \"\")]\n",
        f'{indent}    print("[RPA] Sticky samsung cookies in this profile:", len(_rpa_cks))\n',
        f"{indent}except Exception as _rpa_ce:\n",
        f'{indent}    print("[RPA] Cookie probe failed:", _rpa_ce)\n',
    ]


def _ensure_page_binding(source: str) -> str:
    """Codegen with --user-data-dir often uses `page` without assigning it.

    Sticky Record opens a persistent profile; Playwright sometimes emits only
    context = launch_persistent_context(...) and then page.goto(...) → NameError.
    """
    sticky = _use_system_chrome()
    # Replace naive pages[0] binding — wrong tab = looks logged out every Run.
    if sticky and re.search(r"(?m)^\s*page\s*=\s*context\.pages\[0\]", source):
        def _repl(m):
            indent = m.group(1)
            return "".join(_sticky_page_bind_lines(indent))

        source = re.sub(
            r"(?m)^(\s*)page\s*=\s*context\.pages\[0\][^\n]*\n"
            r"(?:\s*print\(\"\[RPA\] Bound page[^\n]*\n)?",
            _repl,
            source,
            count=1,
        )
        _log("Replaced pages[0] with sticky tab picker (avoid stale MFA tab)")
        return source

    if re.search(r"(?m)^\s*page\s*=", source):
        return source
    if not re.search(r"\bpage\.", source):
        return source

    lines = source.splitlines(keepends=True)
    out: list[str] = []
    injected = False
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        if not injected and re.search(
            r"context\s*=\s*.*\.(launch_persistent_context|new_context)\(",
            line,
        ):
            buf = line
            while buf.count("(") > buf.count(")") and i + 1 < len(lines):
                i += 1
                out.append(lines[i])
                buf += lines[i]
            indent_m = re.match(r"^(\s*)", line)
            indent = indent_m.group(1) if indent_m else "    "
            if sticky:
                out.extend(_sticky_page_bind_lines(indent))
                _log("Injected sticky tab picker (prefer signed-in d2cbi tab)")
            else:
                out.append(
                    f"{indent}page = context.pages[0] if context.pages else context.new_page()\n"
                )
                out.append(f'{indent}print("[RPA] Bound page from sticky context")\n')
                _log("Injected missing page = context.pages[0] (sticky codegen omit)")
            injected = True
        i += 1

    if not injected:
        _log("WARNING: script uses page. but no context= launch — cannot auto-bind")
    return "".join(out)


_STICKY_AUTH_HINT_RE = re.compile(
    r"verification|one[\s-]?time|otp|passcode|authenticator|security code|"
    r"enter code|password|passwort|sign[\s-]?in|log[\s-]?in|username|"
    r"user account|email address|e-mail|mfa|2fa|two[\s-]?factor|capslock",
    re.I,
)


def _strip_sticky_auth_actions(source: str) -> str:
    """Remove recorded login/OTP clicks on sticky runs — do not execute them.

    Soft try/except still ran Sign In and broke the Tableau session. Sticky means
    cookies keep you signed in; OTP/password steps must not replay.
    """
    if not _use_system_chrome():
        return source
    out: list[str] = []
    removed = 0
    for line in source.splitlines(keepends=True):
        stripped = line.lstrip()
        is_action = any(
            tok in stripped
            for tok in (".fill(", ".click(", ".press(", ".type(", ".check(")
        )
        if (
            is_action
            and "print(" not in stripped
            and not stripped.startswith("#")
            and _STICKY_AUTH_HINT_RE.search(stripped)
        ):
            removed += 1
            _log(f"Sticky drop auth step: {stripped[:100]}")
            continue
        out.append(line)
    if removed:
        _log(f"Removed {removed} sticky login/OTP step(s) from Run (session cookies only)")
    return "".join(out)


_LOGIN_GOTO_HINT_RE = re.compile(
    r"login|sign[\s_-]?in|sso|oauth|okta|auth0|microsoftonline|mfa-|"
    r"openid-connect|realms/|vizportal/.*/auth|accounts\.google|"
    r"saml|adfs|/idp/|passwd|session|/auth\b",
    re.I,
)
_PAGE_GOTO_RE = re.compile(
    r'page\.goto\(\s*(["\'])([^"\']*)\1([^)]*)\)'
)


def _fix_sticky_gotos(source: str, start_url: str = "") -> str:
    """Codegen often records page.goto(sign-in URL). That forces login every Run.

    Sticky jobs rewrite the first goto (and any login-looking goto) to the job's
    start_url so cookies can open the real app already signed in.
    """
    if not _use_system_chrome():
        return source
    target = (start_url or os.environ.get("RPA_START_URL") or "").strip()
    if not target:
        _log("Sticky run: no start_url set — cannot rewrite login page.goto")
        return source

    state = {"first": True, "rewrites": 0}

    def _replace(match: re.Match) -> str:
        url = match.group(2)
        rest = match.group(3) or ""
        is_first = state["first"]
        state["first"] = False
        if is_first or _LOGIN_GOTO_HINT_RE.search(url):
            if url.rstrip("/") != target.rstrip("/"):
                state["rewrites"] += 1
                _log(f"Sticky rewrite goto: {url!r} → {target!r}")
            return f"page.goto({target!r}{rest})"
        return match.group(0)

    out = _PAGE_GOTO_RE.sub(_replace, source)
    if state["rewrites"]:
        _log(f"Sticky: rewrote {state['rewrites']} page.goto(s) to job start_url")

    # If SSO still shows, wait for YOU to sign in in THIS window (not normal Chrome).
    gate = """
    _rpa_url = (page.url or "")
    if ("mfa-" in _rpa_url or "openid-connect" in _rpa_url
            or "/protocol/openid-connect/auth" in _rpa_url):
        print("[RPA] ===== MFA LOGIN REQUIRED IN THIS WINDOW =====")
        print("[RPA] Your everyday Chrome session does NOT apply here.")
        print("[RPA] Complete username + OTP in this RPA Chrome, then wait...")
        import re as _rpa_re
        page.wait_for_url(
            _rpa_re.compile(r"https://d2cbi\\.sec\\.samsung\\.net/.*"),
            timeout=600_000,
        )
        print("[RPA] Tableau session OK — continuing automation")
"""
    # Insert gate once after the first page.goto(...)
    lines = out.splitlines(keepends=True)
    gated: list[str] = []
    done = False
    i = 0
    while i < len(lines):
        gated.append(lines[i])
        if not done and "page.goto(" in lines[i]:
            buf = lines[i]
            while buf.count("(") > buf.count(")") and i + 1 < len(lines):
                i += 1
                gated.append(lines[i])
                buf += lines[i]
            gated.append(gate)
            done = True
            _log("Injected sticky MFA wait gate after first goto")
        i += 1
    return "".join(gated)


def prepare_script_source(
    source: str,
    upload_file: Optional[str] = None,
    download_dir: Optional[str] = None,
    start_url: Optional[str] = None,
) -> str:
    from rpa.debug_log import debug_log

    source = _inject_no_timeout_setup(_strip_action_timeouts(source))
    source = _ensure_page_binding(source)
    source = _sanitize_upload_literals(source)
    source = _inject_nerp_url(source)
    # Sticky MFA jobs: operator signs in once by hand; do NOT rewrite login fills
    # to NERP SSO (OTP changes every time and must not be automated).
    if _use_system_chrome():
        _log("Sticky profile run — skipping SSO credential injection")
        source = _fix_sticky_gotos(source, start_url or "")
        source = _strip_sticky_auth_actions(source)
    else:
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


# CDP browser handle kept so GC does not disconnect while the script runs.
_STICKY_CDP_BROWSER = None


class _StickyBrowser:
    """Wrap the sticky Chrome context for codegen's launch()+new_context() pattern.

    MFA/OIDC uses *session* cookies — they die when Chrome exits. Sticky jobs keep
    a real Chrome process alive and Playwright only connects/disconnects via CDP.
    """

    def __init__(self, context, *, browser=None, keepalive: bool = False):
        self._context = context
        self._browser = browser
        self._keepalive = keepalive
        if keepalive:
            # Script calls context.close() — must NOT kill the SSO session.
            try:
                context.close = lambda *a, **k: _log(
                    "Sticky: leaving Chrome open (session cookies)"
                )
            except Exception:
                pass

    def new_context(self, *args, **kwargs):
        return self._context

    def new_page(self, *args, **kwargs):
        return self._context.new_page(*args, **kwargs)

    def close(self):
        if self._keepalive:
            _log("Sticky: disconnect Playwright — Chrome stays open for next Run")
            try:
                if self._browser is not None:
                    self._browser.close()  # CDP disconnect only
            except Exception:
                pass
            return
        try:
            self._context.close()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._context, name)


def _sticky_debug_port() -> int:
    raw = (os.environ.get("RPA_CHROME_DEBUG_PORT") or "9329").strip()
    try:
        return int(raw)
    except ValueError:
        return 9329


def _cdp_is_up(port: int) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=1.5
        ) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        return False


def _ensure_keepalive_chrome(profile: str, port: int) -> None:
    """Start Google Chrome with the sticky profile if CDP is not already listening.

    Must fully detach from the Python/dashboard process — otherwise Windows kills
    Chrome when the RPA script exits and MFA session cookies are lost.
    """
    if _cdp_is_up(port):
        _log(f"Sticky Chrome already running on CDP port {port}")
        return

    exe = _chrome_executable()
    if not exe:
        raise RuntimeError(
            "Google Chrome not found — sticky MFA needs installed Chrome "
            "(set CHROME_PATH)."
        )
    os.makedirs(profile, exist_ok=True)
    cmd = [
        exe,
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--disable-blink-features=AutomationControlled",
        "about:blank",
    ]
    _log(f"Starting keepalive Chrome (detached): profile={profile} port={port}")
    if sys.platform == "win32":
        # `start` breaks away from the dashboard job object so Chrome survives Run.
        quoted = subprocess.list2cmdline(cmd)
        subprocess.Popen(
            f'start "RPA-sticky-chrome-{port}" {quoted}',
            shell=True,
            cwd=config.BASE_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
            | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000),
        )
    else:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    import time

    for _ in range(60):
        if _cdp_is_up(port):
            _log(
                f"Sticky Chrome CDP ready on {port} — leave this window open after MFA"
            )
            return
        time.sleep(0.25)
    raise RuntimeError(
        f"Sticky Chrome did not open CDP on port {port}. "
        "Close any Chrome using this profile and try again."
    )


def _connect_sticky_chrome(browser_type, profile: str):
    """Connect to a long-lived Chrome (keeps MFA session cookies across Runs)."""
    global _STICKY_CDP_BROWSER
    port = _sticky_debug_port()
    _log_profile_debug(profile, channel="chrome-keepalive")
    _ensure_keepalive_chrome(profile, port)
    endpoint = f"http://127.0.0.1:{port}"
    _log(f"Connecting Playwright over CDP {endpoint}")
    browser = browser_type.connect_over_cdp(endpoint)
    _STICKY_CDP_BROWSER = browser
    if browser.contexts:
        context = browser.contexts[0]
    else:
        context = browser.new_context(accept_downloads=True)
    try:
        context.set_default_timeout(0)
        context.set_default_navigation_timeout(0)
    except Exception:
        pass
    _bind_file_chooser_context(context)
    _log(
        "Sticky keepalive ON — do not close this Chrome window; "
        "MFA session lives until you quit that window"
    )
    return browser, context


def _use_system_chrome() -> bool:
    """Sticky MFA profiles must use installed Chrome — same as Record (--channel chrome).

    Bundled Chromium cannot read cookies written by Google Chrome into the profile,
    which looks like 'sticky failed' / always redirected to sign-in.
    """
    return (os.environ.get("RPA_USE_SYSTEM_CHROME") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _log_profile_debug(profile: str, *, channel: str) -> None:
    """Print enough for the operator to confirm Record and Run share one profile."""
    profile = os.path.abspath(profile)
    default = os.path.join(profile, "Default")
    cookie_candidates = [
        os.path.join(default, "Network", "Cookies"),
        os.path.join(default, "Cookies"),
    ]
    cookie_path = next((p for p in cookie_candidates if os.path.isfile(p)), "")
    marker = os.path.join(profile, ".rpa_sticky_profile")
    try:
        with open(marker, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat()} channel={channel}\n")
    except OSError:
        pass
    _log(f"===== STICKY PROFILE DEBUG =====")
    _log(f"user_data_dir = {profile}")
    _log(f"channel      = {channel}")
    _log(f"Default/     = {'yes' if os.path.isdir(default) else 'NO (fresh profile)'}")
    if cookie_path:
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(cookie_path))
            _log(f"Cookies      = {cookie_path} (mtime {mtime:%Y-%m-%d %H:%M:%S})")
        except OSError:
            _log(f"Cookies      = {cookie_path}")
    else:
        _log("Cookies      = missing (not signed in yet, or wrong browser wrote them)")
    _log(f"marker file  = {marker}")
    _log(f"================================")


def _chromium_launch_args(kwargs: dict, *, allow_system_chrome: bool = False) -> tuple[dict, str]:
    """Shared Chromium flags. System Chrome only when sticky MFA needs it."""
    kwargs = dict(kwargs)
    sticky = allow_system_chrome and _use_system_chrome()
    if sticky:
        kwargs["channel"] = "chrome"
        # SSO providers often refuse to persist sessions when Chrome looks automated.
        ignore = list(kwargs.get("ignore_default_args") or [])
        if "--enable-automation" not in ignore:
            ignore.append("--enable-automation")
        kwargs["ignore_default_args"] = ignore
    else:
        kwargs.pop("channel", None)
    extra = list(kwargs.get("args") or [])
    extra.extend([
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
    ])
    if sticky and not any("AutomationControlled" in str(a) for a in extra):
        extra.append("--disable-blink-features=AutomationControlled")
    port = (os.environ.get("RPA_CHROME_DEBUG_PORT") or "").strip()
    if port and not any(str(a).startswith("--remote-debugging-port") for a in extra):
        extra.append(f"--remote-debugging-port={port}")
    kwargs["args"] = extra
    kwargs.setdefault("headless", False)
    return kwargs, port


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

    _orig_persistent = BrowserType.launch_persistent_context

    def launch_persistent_context(self, *args, **kwargs):
        sticky = _use_system_chrome()
        profile = (os.environ.get("RPA_CHROME_PROFILE") or "").strip()
        # Sticky MFA: attach to a Chrome that stays open (session cookies).
        if sticky and profile:
            os.makedirs(profile, exist_ok=True)
            browser, ctx = _connect_sticky_chrome(self, profile)
            # Return a browser-shaped wrapper: codegen may call browser.close().
            return _StickyBrowser(ctx, browser=browser, keepalive=True)

        kwargs, port = _chromium_launch_args(kwargs, allow_system_chrome=True)
        kwargs.setdefault("accept_downloads", True)
        if profile:
            os.makedirs(profile, exist_ok=True)
            if args:
                args = (profile,) + tuple(args[1:])
            else:
                kwargs["user_data_dir"] = profile
            channel = "chrome" if sticky else "chromium"
            _log_profile_debug(profile, channel=channel)
            _log(
                f"Playwright persistent profile={profile} "
                f"channel={channel} port={port or '-'}"
            )
        else:
            _log(
                f"Playwright Chromium persistent profile="
                f"{kwargs.get('user_data_dir')}"
            )
        return _disable(_orig_persistent(self, *args, **kwargs))

    BrowserType.launch_persistent_context = launch_persistent_context

    _orig_launch = BrowserType.launch

    def launch(self, *args, **kwargs):
        # Parallel SAP workers: bundled Chromium (no channel) so many windows open.
        # Sticky MFA: keepalive Chrome + CDP so closing the script does not wipe SSO.
        profile = (os.environ.get("RPA_CHROME_PROFILE") or "").strip()
        sticky = bool(profile) and _use_system_chrome()
        if sticky and profile:
            os.makedirs(profile, exist_ok=True)
            browser, ctx = _connect_sticky_chrome(self, profile)
            return _StickyBrowser(ctx, browser=browser, keepalive=True)
        kwargs, port = _chromium_launch_args(kwargs, allow_system_chrome=False)
        if profile:
            os.makedirs(profile, exist_ok=True)
            kwargs.setdefault("accept_downloads", True)
            _log_profile_debug(profile, channel="chromium")
            _log(
                f"Playwright sticky launch profile={profile} "
                f"channel=chromium port={port or '-'}"
            )
            ctx = _disable(_orig_persistent(self, profile, **kwargs))
            return _StickyBrowser(ctx)
        _log(f"Playwright Chromium launch (not installed Chrome) port={port or '-'}")
        return _orig_launch(self, *args, **kwargs)

    BrowserType.launch = launch
    _PLAYWRIGHT_PATCHED = True


def _fill_is_secret(locator, value) -> bool:
    loc = str(locator)
    if re.search(r"Password|User Account", loc, re.I):
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


def launch_recorder(
    rpa_id: str,
    start_url: str,
    *,
    user_data_dir: Optional[str] = None,
) -> str:
    _validate_rpa_id(rpa_id)
    url = (start_url or "").strip()
    if not url:
        raise ValueError("Start URL is required to record.")

    path = script_path(rpa_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    profile = (user_data_dir or "").strip()
    if profile:
        os.makedirs(profile, exist_ok=True)
        _log_profile_debug(profile, channel="chrome")
        _log(f"Recorder sticky profile: {profile}")
        _log("Sign in HERE — Run will reopen this exact folder with channel=chrome")

    cmd = [
        sys.executable, "-m", "playwright", "codegen", url,
        "-o", path, "--target", "python", "-b", "chromium", "--channel", "chrome",
    ]
    if profile:
        cmd.extend(["--user-data-dir", profile])

    if sys.platform == "win32":
        # Quote each arg for `start` so paths with spaces stay intact.
        quoted = " ".join(f'"{c}"' for c in cmd)
        shell_cmd = f'start "Playwright Codegen — {rpa_id}" {quoted}'
        subprocess.Popen(shell_cmd, shell=True, cwd=config.BASE_DIR)
    else:
        subprocess.Popen(cmd, cwd=config.BASE_DIR)

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
    start_url: Optional[str] = None,
) -> None:
    os.environ.setdefault("NO_PROXY", "*")
    os.environ.setdefault("no_proxy", "*")

    path = script_path(rpa_id)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"No script for '{rpa_id}'. Open Record in the dashboard and save codegen output."
        )

    os.environ.setdefault("RPA_CHROME_DEBUG_PORT", "9329")

    _log(f"Starting script: {rpa_id}")
    job_start = (start_url or "").strip()
    if not job_start:
        try:
            from rpa.jobs_db import get_rpa_job
            job_start = ((get_rpa_job(rpa_id) or {}).get("start_url") or "").strip()
        except Exception:
            job_start = ""
    if job_start:
        os.environ["RPA_START_URL"] = job_start
        _log(f"Job start_url: {job_start}")
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
            staged = stage_upload_for_script(rpa_id, upload_abs)
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
    source = prepare_script_source(
        raw,
        upload_file=sap_upload or upload_file,
        download_dir=download_dir,
        start_url=job_start,
    )

    from win_file_dialog import dismiss_open_file_dialog, dismiss_save_as_dialog
    from mail.settings_db import get_nerp_env, get_nerp_url, get_sso_password, get_sso_username
    from rpa.excel_meta import detect_sales_org

    nerp_url = get_nerp_url()
    sales_org = detect_sales_org(sap_upload or upload_file)
    _log(f"NERP environment: {get_nerp_env()} → {nerp_url}")
    _log(f"Sales Org from Excel: {sales_org}")

    run_globals = {
        "__name__": "__main__",
        "__file__": path,
        "_rpa_os": os,
        "RPA_UPLOAD_FILE": os.environ.get("RPA_UPLOAD_FILE", ""),
        "RPA_UPLOAD_DIR": upload_dir or "",
        "RPA_DOWNLOAD_DIR": download_dir or "",
        "RPA_NERP_URL": nerp_url,
        "RPA_SALES_ORG": sales_org,
        "RPA_USERNAME": get_sso_username() or config.NERP_USERNAME,
        "RPA_PASSWORD": get_sso_password() or config.NERP_PASSWORD,
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
