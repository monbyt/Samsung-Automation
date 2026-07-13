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

    folder = _ps_escape(os.path.normpath(directory) + "\\") if directory else ""

    # Use UI Automation to set the filename field directly — no keyboard shortcuts needed
    ps = f"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms

$targetPath = '{folder}'
$root = [System.Windows.Automation.AutomationElement]::RootElement

# Find Save As dialog by title
$dialog = $null
for ($i = 0; $i -lt 40; $i++) {{
    $wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($w in $wins) {{
        if ($w.Current.Name -match '(?i)save as') {{
            $dialog = $w; break
        }}
    }}
    if ($dialog) {{ break }}
    Start-Sleep -Milliseconds 250
}}
if (-not $dialog) {{ Write-Host 'Save As dialog not found'; exit 1 }}
Write-Host "Found dialog: $($dialog.Current.Name)"

if ($targetPath -ne '') {{
    # Find the filename Edit field and set its value directly
    $editCond = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Edit
    )
    $edits = $dialog.FindAll([System.Windows.Automation.TreeScope]::Descendants, $editCond)
    $fnEdit = $null
    foreach ($e in $edits) {{ $fnEdit = $e }}
    if ($fnEdit) {{
        $vp = $fnEdit.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        $vp.SetValue($targetPath)
        $fnEdit.SetFocus()
        Write-Host "Set filename field to: $targetPath"
        Start-Sleep -Milliseconds 500
        # Press Enter to navigate to the folder
        [System.Windows.Forms.SendKeys]::SendWait('{{ENTER}}')
        Start-Sleep -Milliseconds 1500
    }}
}}

# Click Save button via UI Automation (no keyboard needed)
$btnCond = [System.Windows.Automation.AndCondition]::new(
    [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button
    ),
    [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::NameProperty, 'Save'
    )
)
$saveBtn = $dialog.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $btnCond)
if ($saveBtn) {{
    $ip = $saveBtn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    $ip.Invoke()
    Write-Host 'Clicked Save button via UIA'
    exit 0
}}
Write-Host 'Save button not found'
exit 1
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
