"""
NERP RPA — SSO login, file upload (ZLSDF50270), and P/I workflow (ZSDM31520).

Run:  python run_nerp.py   (or double-click run_nerp.bat)

First run opens Chrome so you can complete SSO login; the session is kept
in chrome-profile-nerp/ for later runs.
"""
import os

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

import config

SHELL_IFRAME = 'iframe[name="application-Shell-startGUI-iframe"]'


def _shell(page):
    return page.locator(SHELL_IFRAME).content_frame


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
            "Set NERP_USERNAME and NERP_PASSWORD in config.py or as env vars."
        )

    page.get_by_role("textbox", name="User Account").fill(config.NERP_USERNAME)
    page.get_by_role("textbox", name="Password").fill(config.NERP_PASSWORD)
    page.get_by_role("textbox", name="Password").press("Enter")


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

    # Dismiss error/close dialogs if they appear
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

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(config.NERP_SSO_URL)
        page.wait_for_load_state("domcontentloaded")

        if _needs_login(page):
            print("Logging into NERP SSO...")
            _login(page)
            page.wait_for_load_state("networkidle")
        else:
            print("Using saved NERP session.")

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
