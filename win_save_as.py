"""
Windows-only helper: auto-confirm the native 'Save As' dialog.

Word/Office opens a modal Save As that Playwright can't see. We find it
by window title and smash Enter (Save is already the default button).
"""
import os
import shutil
import sys
import time


def _documents_dir():
    home = os.environ.get("USERPROFILE", "")
    return os.path.join(home, "Documents") if home else ""


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
    """Return titles of every visible top-level window that looks like Save As."""
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


def _press_enter_ctypes(title):
    """Focus the dialog by title and send a physical Enter key."""
    if sys.platform != "win32":
        return False

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hwnd = None

    def callback(h, _):
        nonlocal hwnd
        if hwnd:
            return True
        if not user32.IsWindowVisible(h):
            return True
        length = user32.GetWindowTextLengthW(h) + 1
        if length <= 1:
            return True
        buf = ctypes.create_unicode_buffer(length)
        user32.GetWindowTextW(h, buf, length)
        if title.lower() in buf.value.lower():
            hwnd = h
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(callback), 0)
    if not hwnd:
        return False

    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    cur_thread = kernel32.GetCurrentThreadId()
    attached = False
    if fg_thread != cur_thread:
        user32.AttachThreadInput(cur_thread, fg_thread, True)
        attached = True

    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    time.sleep(0.3)

    VK_RETURN = 0x0D
    KEYEVENTF_KEYUP = 0x0002
    user32.keybd_event(VK_RETURN, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)

    if attached:
        user32.AttachThreadInput(cur_thread, fg_thread, False)
    return True


def _press_enter_wscript(title):
    """Activate by partial window title and SendKeys Enter."""
    import subprocess

    safe = title.replace("'", "''")
    ps = f"""
$w = New-Object -ComObject WScript.Shell
if ($w.AppActivate('{safe}')) {{
    Start-Sleep -Milliseconds 400
    $w.SendKeys('{{ENTER}}')
    exit 0
}}
exit 1
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode == 0


def dismiss_save_as_dialog(timeout=60):
    """
    Block until we find a Save As dialog and press Enter, or timeout.
    Called from the main thread so Windows actually lets us focus the dialog.
    """
    if sys.platform != "win32":
        print("Save As helper only runs on Windows.")
        return False

    print("Looking for Save As dialog to press Enter...")
    deadline = time.time() + timeout

    while time.time() < deadline:
        titles = _find_save_as_titles()
        if titles:
            title = titles[0]
            print(f"Found: '{title}' — pressing Enter")

            if _press_enter_wscript(title):
                print("Enter sent (WScript).")
                return True
            if _press_enter_ctypes(title):
                print("Enter sent (keyboard).")
                return True
            print("Found dialog but Enter failed, retrying...")

        time.sleep(0.25)

    print("Save As dialog not found — is it still open on screen?")
    return False


def wait_for_new_file(directory, timeout=90, extensions=(".xlsx", ".xls", ".csv")):
    """
    Return the newest spreadsheet that appeared in download_dir or common
  fallback folders (Documents, Desktop, Downloads). Moves it into *directory*.
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
                if not name.lower().endswith(extensions):
                    continue

                dest = os.path.join(directory, name)
                if folder != directory:
                    print(f"Found in {folder}, moving to {directory}")
                    shutil.move(path, dest)
                    return dest
                return path

    raise TimeoutError(f"No new spreadsheet in {watch} within {timeout}s")
