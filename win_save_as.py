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


def _enum_visible_windows(match_fn):
    """Enumerate top-level visible windows; returns list of (hwnd, title)."""
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

    results = []

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
            results.append((int(hwnd), title))
        return True

    user32.EnumWindows(callback, 0)
    return results


def _enum_visible_window_titles(match_fn) -> list[str]:
    return [t for _, t in _enum_visible_windows(match_fn)]


def _find_save_as_titles():
    return _enum_visible_window_titles(lambda t: "save as" in t.lower())


def _find_save_as_hwnd():
    matches = _enum_visible_windows(lambda t: "save as" in t.lower())
    return matches[0][0] if matches else None


def _force_foreground(hwnd: int) -> bool:
    """Bring a window to foreground and give it keyboard focus."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)

    fg = user32.GetForegroundWindow()
    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    target_tid = user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None)

    attached_fg = False
    attached_target = False
    if fg_tid and fg_tid != cur_tid:
        attached_fg = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
    if target_tid and target_tid != cur_tid:
        attached_target = bool(user32.AttachThreadInput(cur_tid, target_tid, True))

    ok = bool(user32.SetForegroundWindow(wintypes.HWND(hwnd)))

    if attached_fg:
        user32.AttachThreadInput(cur_tid, fg_tid, False)
    if attached_target:
        user32.AttachThreadInput(cur_tid, target_tid, False)

    return ok


def _run_powershell(ps: str) -> bool:
    import subprocess

    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.stdout.strip():
        print(f"  [SaveAs PS] {result.stdout.strip()[:500]}")
    if result.stderr.strip():
        print(f"  [SaveAs ERR] {result.stderr.strip()[:300]}")
    return result.returncode == 0


def _navigate_and_save(title: str, directory: Optional[str]) -> bool:
    if directory:
        os.makedirs(directory, exist_ok=True)

    hwnd = _find_save_as_hwnd()
    if not hwnd:
        print("  [SaveAs] HWND for 'Save As' not found")
        return False

    if not _force_foreground(hwnd):
        print(f"  [SaveAs] Warning: could not force foreground (hwnd={hwnd})")
    time.sleep(0.3)

    folder = _ps_escape(os.path.normpath(directory)) if directory else ""

    ps = f"""
Add-Type -AssemblyName System.Windows.Forms

$targetPath = '{folder}'

if ($targetPath -ne '') {{
    # Alt+D → address bar
    [System.Windows.Forms.SendKeys]::SendWait('%d')
    Start-Sleep -Milliseconds 400

    # Paste path via clipboard (avoids SendKeys backslash issues)
    [System.Windows.Forms.Clipboard]::SetText($targetPath)
    Start-Sleep -Milliseconds 100
    [System.Windows.Forms.SendKeys]::SendWait('^v')
    Start-Sleep -Milliseconds 300
    Write-Host "Pasted path: $targetPath"

    # Enter → navigate
    [System.Windows.Forms.SendKeys]::SendWait('{{ENTER}}')
    Start-Sleep -Milliseconds 2000
}}

# Alt+S → Save
Write-Host 'Pressing Alt+S...'
[System.Windows.Forms.SendKeys]::SendWait('%s')
Start-Sleep -Milliseconds 500
Write-Host 'Done'
exit 0
"""
    return _run_powershell(ps)


def _dump_all_window_titles():
    """Print every visible window title for debugging."""
    all_titles = _enum_visible_window_titles(lambda t: bool(t))
    print(f"  [SaveAs DEBUG] Visible windows: {all_titles[:20]}")


def dismiss_save_as_dialog(timeout=60, directory=None):
    """Find Save As, navigate to *directory*, then confirm save."""
    if sys.platform != "win32":
        print("Save As helper only runs on Windows.")
        return False

    folder_msg = f" → {directory}" if directory else ""
    print(f"Looking for Save As dialog{folder_msg}...")
    deadline = time.time() + timeout
    dumped = False

    while time.time() < deadline:
        titles = _find_save_as_titles()
        if titles:
            title = titles[0]
            print(f"Found: '{title}'")
            if _navigate_and_save(title, directory):
                print("Save As confirmed.")
                return True
            print("Save As confirm failed, retrying...")
        else:
            if not dumped:
                _dump_all_window_titles()
                dumped = True

        time.sleep(0.25)

    print("Save As dialog not found — is it still open on screen?")
    _dump_all_window_titles()
    return False


_SKIP_EXTS = (".crdownload", ".part", ".tmp", ".partial", ".!ut", ".download")


def wait_for_new_file(directory, timeout=90, extensions=None):
    """Wait for a new file in *directory* (or move from a fallback folder).

    Accepts any extension by default. Pass `extensions` (tuple of lowercase
    suffixes incl. leading dot) to filter. In-progress download temp files
    are always skipped.
    """
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
                lower = name.lower()
                if lower.endswith(_SKIP_EXTS):
                    continue
                if extensions and not lower.endswith(extensions):
                    continue

                dest = os.path.join(directory, name)
                if folder != directory:
                    print(f"Found in {folder}, moving to {directory}")
                    shutil.move(path, dest)
                    return dest
                return path

    raise TimeoutError(f"No new file in {watch} within {timeout}s")
