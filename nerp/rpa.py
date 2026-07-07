"""
NERP RPA — matches Playwright codegen line-for-line (nerpsr URL + config creds).

Run:  python run_nerp.py   (or double-click run_nerp.bat)
"""
import os

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

from playwright.sync_api import sync_playwright

import config

IFRAME = 'iframe[name="application-Shell-startGUI-iframe"]'


def run(upload_file=None) -> None:
    path = upload_file or config.NERP_UPLOAD_FILE
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Upload file not found: {path}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="chrome",
            headless=config.NERP_HEADLESS,
        )
        context = browser.new_context()
        context.set_default_timeout(0)
        context.set_default_navigation_timeout(0)
        page = context.new_page()

        page.goto(config.NERP_URL)

        page.get_by_role("textbox", name="User Account").click()
        page.get_by_role("textbox", name="User Account").fill(config.NERP_USERNAME)
        page.get_by_role("textbox", name="Password").click()
        page.get_by_role("textbox", name="Password").fill(config.NERP_PASSWORD)
        page.get_by_role("textbox", name="Password").press("Enter")

        page.locator("#canvas").click()
        page.locator('[id="__xmlview0--homeLink-gridContainer-content"]').click()

        page.get_by_role("textbox", name="Search Program").click()
        page.get_by_role("textbox", name="Search Program").fill(config.NERP_PROGRAM_UPLOAD)
        page.get_by_role("button", name="Go").click()

        page.locator(IFRAME).content_frame.get_by_role(
            "textbox", name="Upload file Required"
        ).click()
        page.locator(IFRAME).content_frame.locator("#ls-inputfieldhelpbutton").click()
        page.locator(IFRAME).content_frame.get_by_role("button", name="OK").click()
        page.locator(IFRAME).content_frame.get_by_role("button", name="OK").click()
        page.locator(IFRAME).content_frame.locator(
            "#webgui_filebrowser_file_upload"
        ).set_input_files(path)
        page.locator(IFRAME).content_frame.get_by_role(
            "textbox", name="Uploading Rows Required"
        ).click()
        page.locator(IFRAME).content_frame.get_by_role(
            "textbox", name="Result file Required"
        ).click()
        page.locator(IFRAME).content_frame.get_by_role(
            "button", name="Execute  Emphasized"
        ).click()

        page.get_by_role("textbox", name="Search Program").click()
        page.get_by_role("textbox", name="Search Program").fill(config.NERP_PROGRAM_PI)
        page.get_by_role("button", name="Go").click()

        page.locator(IFRAME).content_frame.get_by_role(
            "textbox", name="Sold-to Party"
        ).click()
        page.locator(IFRAME).content_frame.get_by_role(
            "button", name="Execute  Emphasized"
        ).click()
        page.locator(IFRAME).content_frame.get_by_role(
            "radio", name="Document select"
        ).click()
        page.locator(IFRAME).content_frame.get_by_role(
            "textbox", name="Sales Document", exact=True
        ).click()
        page.locator(IFRAME).content_frame.locator("#ls-inputfieldhelpbutton").click()
        page.locator(IFRAME).content_frame.get_by_role(
            "button", name="OK  Emphasized"
        ).click()
        page.locator(IFRAME).content_frame.get_by_role(
            "button", name="Execute  Emphasized"
        ).click()
        page.locator(IFRAME).content_frame.get_by_role(
            "gridcell", name="To select a row, press the"
        ).click()
        page.locator(IFRAME).content_frame.get_by_role("button", name="Create P/I").click()
        page.locator(IFRAME).content_frame.get_by_role("button", name="Print P/I").click()
        page.locator(IFRAME).content_frame.locator("#ls-inputfieldhelpbutton").click()
        page.locator(IFRAME).content_frame.get_by_role(
            "button", name="Cancel", exact=True
        ).click()
        page.locator(IFRAME).content_frame.get_by_role(
            "button", name="Print", description="Print (Ctrl+P)"
        ).click()
        page.locator(IFRAME).content_frame.get_by_role(
            "dialog", name="Error"
        ).get_by_label("Close").click()
        page.locator(IFRAME).content_frame.get_by_role("button", name="Close").click()

        context.close()
        browser.close()


if __name__ == "__main__":
    run()
