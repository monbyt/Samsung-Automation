"""
Windows-only helper: auto-confirm the native 'Save As' dialog.

When W1/Chrome can't hand the download to Playwright, Office or the OS
shows this dialog. We watch for it in a background thread, point the
filename at DOWNLOAD_DIR, and press Enter (Save is the default button).
"""
import os
import sys
import threading
import time


def _confirm_save_as_pywinauto(download_dir, timeout):
    from pywinauto import Application

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            app = Application(backend="uia").connect(title="Save As", timeout=0.5)
            dlg = app.window(title="Save As")
            dlg.set_focus()

            edits = [c for c in dlg.descendants(control_type="Edit")]
            if not edits:
                dlg.type_keys("{ENTER}")
                return True

            name = (edits[0].get_value() or "").strip()
            if not name:
                name = "order_extract.xlsx"
            full_path = os.path.join(download_dir, os.path.basename(name))
            edits[0].set_edit_text(full_path)
            dlg.type_keys("{ENTER}")
            return True
        except Exception:
            time.sleep(0.3)
    return False


def _confirm_save_as_powershell(download_dir, timeout):
    """Fallback when pywinauto isn't installed — uses WScript.Shell SendKeys."""
    import subprocess

    download_dir = download_dir.replace("'", "''")
    ps = rf"""
$deadline = (Get-Date).AddSeconds({int(timeout)})
while ((Get-Date) -lt $deadline) {{
    $p = Get-Process | Where-Object {{ $_.MainWindowTitle -eq 'Save As' }} | Select-Object -First 1
    if ($p) {{
        $w = New-Object -ComObject WScript.Shell
        $null = $w.AppActivate($p.Id)
        Start-Sleep -Milliseconds 250
        $w.SendKeys('^a')
        Start-Sleep -Milliseconds 100
        $w.SendKeys('{download_dir}\')
        Start-Sleep -Milliseconds 100
        $w.SendKeys('{{ENTER}}')
        exit 0
    }}
    Start-Sleep -Milliseconds 300
}}
exit 1
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        timeout=timeout + 10,
    )
    return result.returncode == 0


def _confirm_save_as(download_dir, timeout):
    try:
        return _confirm_save_as_pywinauto(download_dir, timeout)
    except ImportError:
        print("Note: pywinauto not installed — using PowerShell fallback for Save As.")
        return _confirm_save_as_powershell(download_dir, timeout)


def start_save_as_watcher(download_dir, timeout=90):
    """
    Start a daemon thread that confirms 'Save As' if it appears.
    Returns the Thread (or None on non-Windows).
    """
    if sys.platform != "win32":
        return None

    os.makedirs(download_dir, exist_ok=True)

    def _run():
        if _confirm_save_as(download_dir, timeout):
            print(f"Auto-confirmed Save As → {download_dir}")

    thread = threading.Thread(target=_run, daemon=True, name="save-as-watcher")
    thread.start()
    return thread


def wait_for_new_file(directory, timeout=90, extensions=(".xlsx", ".xls", ".csv")):
    """Return the newest file in *directory* that appeared during the wait."""
    os.makedirs(directory, exist_ok=True)
    before = {
        f for f in os.listdir(directory)
        if os.path.isfile(os.path.join(directory, f))
    }
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1)
        candidates = []
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            if name in before:
                continue
            if name.lower().endswith(extensions):
                candidates.append(path)
        if candidates:
            return max(candidates, key=os.path.getmtime)
    raise TimeoutError(f"No new spreadsheet in {directory} within {timeout}s")
