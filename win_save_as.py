"""
Windows-only helper: navigate the native Save As dialog and confirm save.

Uses clipboard paste into the folder bar (Alt+D) or file name field, then Enter.
"""
import os
import shutil
import sys
import time
from typing import Optional


def _extra_watch_dirs():
    home = os.environ.get("USERPROFILE", "")
    if not home:
        return []
    return [
        os.path.join(home, "Documents"),
        os.path.join(home, "Desktop"),
        os.path.join(home, "Downloads"),
    ]


def _ps_escape(s: str) -> str:
    return s.replace("'", "''")


def _enum_visible_window_titles(match_fn) -> list[str]:
    """Enumerate top-level visible windows; match_fn(title) -> bool."""
    if sys.platform != "win32":
        return []

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL

    titles: list[str] = []

    @WNDENUMPROC
    def callback(hwnd, _lparam):
        if not hwnd:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd) + 1
        if length <= 1:
            return True
        buf = ctypes.create_unicode_buffer(length)
        user32.GetWindowTextW(hwnd, buf, length)
        title = buf.value.strip()
        if title and match_fn(title):
            titles.append(title)
        return True

    user32.EnumWindows(callback, 0)
    return titles


def _find_save_as_titles():
    return _enum_visible_window_titles(lambda t: "save as" in t.lower())


def _run_powershell(ps: str) -> bool:
    import subprocess

    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0 and result.stderr:
        print(f"  Save As helper: {result.stderr.strip()[:200]}")
    return result.returncode == 0


def _navigate_and_save(title: str, directory: Optional[str]) -> bool:
    safe_title = _ps_escape(title)
    lines = [
        "$w = New-Object -ComObject WScript.Shell",
        f"if (-not $w.AppActivate('{safe_title}')) {{ exit 1 }}",
        "Start-Sleep -Milliseconds 800",
    ]

    if directory:
        folder = _ps_escape(os.path.normpath(directory))
        os.makedirs(directory, exist_ok=True)
        lines += [
            f"Set-Clipboard -Value '{folder}'",
            # Address bar (modern Windows file dialog)
            "$w.SendKeys('%d')",
            "Start-Sleep -Milliseconds 500",
            "$w.SendKeys('^a')",
            "$w.SendKeys('^v')",
            "$w.SendKeys('{ENTER}')",
            "Start-Sleep -Milliseconds 900",
        ]

    lines += [
        "$w.SendKeys('%s')",  # Alt+S = Save button (more reliable than Enter)
        "exit 0",
    ]
    if _run_powershell("\n".join(lines)):
        return True

    if not directory:
        return False

    # Fallback: paste folder path into the file name field (classic dialogs).
    folder = _ps_escape(os.path.normpath(directory))
    fallback = f"""
$w = New-Object -ComObject WScript.Shell
if (-not $w.AppActivate('{safe_title}')) {{ exit 1 }}
Start-Sleep -Milliseconds 800
Set-Clipboard -Value '{folder}'
$w.SendKeys('^a')
$w.SendKeys('^v')
$w.SendKeys('{{ENTER}}')
Start-Sleep -Milliseconds 900
$w.SendKeys('%s')
exit 0
"""
    return _run_powershell(fallback)


def dismiss_save_as_dialog(timeout=60, directory=None):
    """Find Save As, navigate to *directory*, then confirm save."""
    if sys.platform != "win32":
        print("Save As helper only runs on Windows.")
        return False

    folder_msg = f" → {directory}" if directory else ""
    print(f"Looking for Save As dialog{folder_msg}...")
    deadline = time.time() + timeout

    while time.time() < deadline:
        titles = _find_save_as_titles()
        if titles:
            title = titles[0]
            print(f"Found: '{title}'")
            if _navigate_and_save(title, directory):
                print("Save As confirmed.")
                return True
            print("Save As confirm failed, retrying...")

        time.sleep(0.25)

    print("Save As dialog not found — is it still open on screen?")
    return False


def wait_for_new_file(directory, timeout=90, extensions=(".xlsx", ".xls", ".csv", ".zip")):
    """Wait for a new file in *directory* (or move from a fallback folder)."""
    os.makedirs(directory, exist_ok=True)
    watch = [directory] + [d for d in _extra_watch_dirs() if d and os.path.isdir(d)]

    before = {}
    for folder in watch:
        before[folder] = {
            f for f in os.listdir(folder)
            if os.path.isfile(os.path.join(folder, f))
        }

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        for folder in watch:
            for name in os.listdir(folder):
                if name in before.get(folder, set()):
                    continue
                path = os.path.join(folder, name)
                if not os.path.isfile(path):
                    continue
                if not name.lower().endswith(extensions):
                    continue

                dest = os.path.join(directory, name)
                if folder != directory:
                    print(f"Found in {folder}, moving to {directory}")
                    shutil.move(path, dest)
                    return dest
                return path

    raise TimeoutError(f"No new file in {watch} within {timeout}s")
