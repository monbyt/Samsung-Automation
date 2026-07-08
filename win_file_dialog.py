"""
Windows native file dialogs — Open and Save As (used by mail + RPA).

RPA scripts can call:
  win_open_file(RPA_UPLOAD_DIR, filename)   # Open / upload dialog
  win_save_as(RPA_DOWNLOAD_DIR)             # Save As dialog
"""
import os
import sys
import time
from typing import Optional

from win_save_as import _navigate_and_save, _ps_escape, _run_powershell, dismiss_save_as_dialog

__all__ = [
    "dismiss_open_file_dialog",
    "dismiss_save_as_dialog",
    "win_open_file",
    "win_save_as",
]


def _find_dialog_titles(keywords):
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
        title = buf.value.strip().lower()
        if title and any(k in title for k in keywords):
            titles.append(buf.value.strip())
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return titles


def _open_via_powershell(title: str, full_path: str) -> bool:
  safe_title = _ps_escape(title)
  safe_path = _ps_escape(os.path.normpath(full_path))
  ps = f"""
$w = New-Object -ComObject WScript.Shell
if (-not $w.AppActivate('{safe_title}')) {{ exit 1 }}
Start-Sleep -Milliseconds 800
Set-Clipboard -Value '{safe_path}'
$w.SendKeys('^a')
$w.SendKeys('^v')
Start-Sleep -Milliseconds 400
$w.SendKeys('{{ENTER}}')
Start-Sleep -Milliseconds 400
$w.SendKeys('{{ENTER}}')
exit 0
"""
  return _run_powershell(ps)


def dismiss_open_file_dialog(
    directory: Optional[str] = None,
    filename: Optional[str] = None,
    timeout: int = 120,
) -> bool:
    """
    Handle Windows Open / Choose File dialog.
    Pass directory + filename, or a full path via directory alone.
    """
    if sys.platform != "win32":
        print("Open-file helper only runs on Windows.")
        return False

    if directory and filename:
        full_path = os.path.join(directory, filename)
    elif directory and os.path.isfile(directory):
        full_path = directory
        directory = os.path.dirname(full_path)
        filename = os.path.basename(full_path)
    elif filename:
        full_path = filename
    else:
        full_path = directory
        directory = os.path.dirname(full_path) if full_path else None
        filename = os.path.basename(full_path) if full_path else None

    if not full_path:
        print("Open-file helper: no file path provided.")
        return False

    os.makedirs(directory or os.path.dirname(full_path) or ".", exist_ok=True)
    print(f"Looking for Open dialog → {full_path}")

    keywords = ("open", "choose file", "file upload", "select file", "browse")
    deadline = time.time() + timeout

    while time.time() < deadline:
        titles = _find_dialog_titles(keywords)
        if titles:
            title = titles[0]
            print(f"Found Open dialog: '{title}'")
            if _open_via_powershell(title, full_path):
                print("Open dialog confirmed.")
                return True
            print("Open dialog confirm failed, retrying...")
        time.sleep(0.25)

    print("Open dialog not found — is it still open on screen?")
    return False


def win_open_file(directory: Optional[str] = None, filename: Optional[str] = None, **kwargs) -> bool:
    """Alias for RPA scripts — upload from a Windows folder."""
    return dismiss_open_file_dialog(directory=directory, filename=filename, **kwargs)


def win_save_as(directory: Optional[str] = None, **kwargs) -> bool:
    """Alias for RPA scripts — save to a Windows folder via Save As."""
    return dismiss_save_as_dialog(directory=directory, **kwargs)
