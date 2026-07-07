"""
NERP RPA — SSO login, file upload (ZLSDF50270), and P/I workflow (ZSDM31520).

Run:  python run_nerp.py   (or double-click run_nerp.bat)

First run opens Chrome so you can complete SSO login; the session is kept
in chrome-profile-nerp/ for later runs.
"""
import os
import time

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

import config

SHELL_IFRAME = 'iframe[name="application-Shell-startGUI-iframe"]'


def _shell(page):
    return page.locator(SHELL_IFRAME).content_frame


def _live_page(context, page=None):
    """SSO redirects often close the tab Playwright started on — use an open one."""
    if page is not None and not page.is_closed():
        return page
    for candidate in reversed(context.pages):
        if not candidate.is_closed():
            return candidate
    return context.new_page()


def _needs_login(page) -> bool:
    try:
        page.get_by_role("textbox", name="User Account").wait_for(
            state="visible", timeout=5_000
        )
        return True
    except PlaywrightTimeout:
        return False


def _login(page) -> None:
    if not config.NERP_USERNAME or not config.NERP_PASSWORD:
        raise RuntimeError(
            "Set NERP_USERNAME and NERP_PASSWORD in config.py."
        )

    page.get_by_role("textbox", name="User Account").click()
    page.get_by_role("textbox", name="User Account").fill(config.NERP_USERNAME)
    page.get_by_role("textbox", name="Password").click()
    page.get_by_role("textbox", name="Password").fill(config.NERP_PASSWORD)
    page.get_by_role("textbox", name="Password").press("Enter")


def _wait_for_nerp_ready(context, page, timeout_ms: int = 120_000):
    """Wait until Fiori shell is visible after SSO redirect."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        page = _live_page(context, page)
        try:
            page.locator("#canvas").wait_for(state="visible", timeout=2_000)
            return page
        except PlaywrightTimeout:
            pass
        try:
            page.get_by_role("textbox", name="Search Program").wait_for(
                state="visible", timeout=2_000
            )
            return page
        except PlaywrightTimeout:
            pass
        page.wait_for_timeout(500)
    raise RuntimeError("NERP did not load after SSO login (timed out).")


def _open_home(page) -> None:
    page.locator("#canvas").click()
    page.locator('[id="__xmlview0--homeLink-gridContainer-content"]').click()


def _open_program(page, program_code: str) -> None:
    search = page.get_by_role("textbox", name="Search Program")
    search.click()
    search.fill(program_code)
    page.get_by_role("button", name="Go").click()


def _upload_file(shell, upload_path: str) -> None:
    shell.get_by_role("textbox", name="Upload file Required").click()
    shell.locator("#ls-inputfieldhelpbutton").click()
    shell.get_by_role("button", name="OK").click()
    shell.get_by_role("button", name="OK").click()
    shell.locator("#webgui_filebrowser_file_upload").set_input_files(upload_path)
    shell.get_by_role("textbox", name="Uploading Rows Required").click()
    shell.get_by_role("textbox", name="Result file Required").click()
    shell.get_by_role("button", name="Execute  Emphasized").click()


def _create_and_print_pi(shell) -> None:
    shell.get_by_role("textbox", name="Sold-to Party").click()
    shell.get_by_role("button", name="Execute  Emphasized").click()
    shell.get_by_role("radio", name="Document select").click()
    shell.get_by_role("textbox", name="Sales Document", exact=True).click()
    shell.locator("#ls-inputfieldhelpbutton").click()
    shell.get_by_role("button", name="OK  Emphasized").click()
    shell.get_by_role("button", name="Execute  Emphasized").click()
    shell.get_by_role("gridcell", name="To select a row, press the").click()
    shell.get_by_role("button", name="Create P/I").click()
    shell.get_by_role("button", name="Print P/I").click()
    shell.locator("#ls-inputfieldhelpbutton").click()
    shell.get_by_role("button", name="Cancel", exact=True).click()
    shell.get_by_role("button", name="Print", description="Print (Ctrl+P)").click()

    try:
        shell.get_by_role("dialog", name="Error").get_by_label("Close").click(timeout=3_000)
    except PlaywrightTimeout:
        pass
    try:
        shell.get_by_role("button", name="Close").click(timeout=3_000)
    except PlaywrightTimeout:
        pass


def run(upload_file=None) -> None:
    path = upload_file or config.NERP_UPLOAD_FILE
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Upload file not found: {path}")

    os.makedirs(config.NERP_PROFILE_DIR, exist_ok=True)

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            config.NERP_PROFILE_DIR,
            channel="chrome",
            headless=config.NERP_HEADLESS,
            args=["--disable-popup-blocking", "--no-first-run"],
        )

        page = _live_page(context)
        page.goto(config.NERP_SSO_URL, wait_until="domcontentloaded")

        if _needs_login(page):
            print("Logging into NERP SSO...")
            _login(page)
        else:
            print("Using saved NERP session.")

        print("Waiting for NERP to load...")
        page = _wait_for_nerp_ready(context, page)

        _open_home(page)

        print(f"Running upload program {config.NERP_PROGRAM_UPLOAD}...")
        _open_program(page, config.NERP_PROGRAM_UPLOAD)
        _upload_file(_shell(page), path)

        print(f"Running P/I program {config.NERP_PROGRAM_PI}...")
        _open_program(page, config.NERP_PROGRAM_PI)
        _create_and_print_pi(_shell(page))

        context.close()
        print("NERP RPA complete.")


if __name__ == "__main__":
    run()
