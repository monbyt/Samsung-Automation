"""
Windows-only helper: navigate the native Save As dialog and confirm save.

W1/Office opens a modal Save As that Playwright can't see. We activate it,
jump to the job folder (Alt+D → path → Enter), then press Enter to save.
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


def _find_save_as_titles():
    if sys.platform != "win32":
        return []

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    titles = []

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd) + 1
        if length <= 1:
            return True
        buf = ctypes.create_unicode_buffer(length)
        user32.GetWindowTextW(hwnd, buf, length)
        title = buf.value.strip()
        if title and "save as" in title.lower():
            titles.append(title)
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return titles


def _ps_escape(s: str) -> str:
    return s.replace("'", "''")


def _confirm_save_as_wscript(title: str, directory: Optional[str]) -> bool:
    import subprocess

    safe_title = _ps_escape(title)
    lines = [
        "$w = New-Object -ComObject WScript.Shell",
        f"if (-not $w.AppActivate('{safe_title}')) {{ exit 1 }}",
        "Start-Sleep -Milliseconds 500",
    ]

    if directory:
        folder = _ps_escape(os.path.normpath(directory))
        os.makedirs(directory, exist_ok=True)
        lines += [
            # Address bar in the Windows file dialog
            "$w.SendKeys('%d')",
            "Start-Sleep -Milliseconds 400",
            f"$w.SendKeys('{folder}')",
            "$w.SendKeys('{ENTER}')",
            "Start-Sleep -Milliseconds 600",
        ]

    lines += [
        "$w.SendKeys('{ENTER}')",
        "exit 0",
    ]

    ps = "\n".join(lines)
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.returncode == 0


def dismiss_save_as_dialog(timeout=60, directory=None):
    """
    Find Save As, optionally navigate to *directory*, then confirm save.
    """
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

            if _confirm_save_as_wscript(title, directory):
                print("Save As confirmed.")
                return True
            print("Save As confirm failed, retrying...")

        time.sleep(0.25)

    print("Save As dialog not found — is it still open on screen?")
    return False


def wait_for_new_file(directory, timeout=90, extensions=(".xlsx", ".xls", ".csv", ".zip")):
    """Wait for a new spreadsheet in *directory* (or move from a fallback folder)."""
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

    raise TimeoutError(f"No new spreadsheet in {watch} within {timeout}s")
