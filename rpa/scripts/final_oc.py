import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from playwright.sync_api import sync_playwright

_SHELL = "iframe[name=\"application-Shell-startGUI-iframe\"]"
_DL_DIR = os.environ.get("RPA_DOWNLOAD_DIR") or config.DOWNLOAD_DIR


def _shell(page):
    return page.locator(_SHELL).content_frame


with sync_playwright() as pw:
    context = pw.chromium.launch_persistent_context(
        config.NERP_PROFILE_DIR,
        channel="chrome",
        headless=config.NERP_HEADLESS,
        accept_downloads=True,
        args=["--disable-popup-blocking", "--no-first-run"],
    )
    page = context.pages[0] if context.pages else context.new_page()

    # Navigate — persistent profile handles session; login if redirected
    page.goto("https://nerpsr.sec.samsung.net/sap/bc/ui2/flp#Utility-home")
    if "sts.secsso.net" in page.url or "loginapp" in page.url:
        page.get_by_role("textbox", name="User Account").fill("m.tasoglu")
        page.get_by_role("textbox", name="Password").fill("Pass2002?")
        page.get_by_role("button", name="Login").click()
        page.goto("https://nerpsr.sec.samsung.net/sap/bc/ui2/flp#Utility-home")

    page.get_by_role("textbox", name="Search Program").click()
    page.get_by_role("textbox", name="Search Program").fill("ZLSDF50270")
    page.get_by_role("button", name="Go").click()
    _shell(page).get_by_role("textbox", name="Sales Org.").click()
    _shell(page).get_by_role("textbox", name="Sales Org.").fill("7101")
    _shell(page).get_by_role("textbox", name="Sales Org.").click()
    _shell(page).get_by_role("textbox", name="Upload file Required").click()
    _shell(page).locator("#ls-inputfieldhelpbutton").click()
    _shell(page).get_by_role("button", name="OK").click()
    _shell(page).locator("#webgui_filebrowser_file_upload").set_input_files("ZLSDF50270LAYOUT.XLSX")
    _shell(page).get_by_role("button", name="Execute  Emphasized").click()
    _shell(page).get_by_role("button", name="Create Sales Order").click()
    with page.expect_download() as _dl1_info:
        _shell(page).get_by_role("button", name="Yes").click()
    os.makedirs(_DL_DIR, exist_ok=True)
    _dl1 = _dl1_info.value
    _dl1.save_as(os.path.join(_DL_DIR, _dl1.suggested_filename))
    print(f"[RPA] Download 1 saved: {_dl1.suggested_filename}")

    # Capture SO number before navigating away
    _so_cell = _shell(page).locator("#C111-mrss-cont-none-Row-0").get_by_text(re.compile(r"\d{10,}"))
    _so_number = _so_cell.inner_text().strip()
    print(f"[RPA] Captured SO number: {_so_number}")

    # Navigate to ZSDM31520
    page.goto("https://nerpsr.sec.samsung.net/sap/bc/ui2/flp#Utility-home")
    page.get_by_role("textbox", name="Search Program").wait_for(state="visible")
    page.get_by_role("textbox", name="Search Program").click()
    page.get_by_role("textbox", name="Search Program").fill("ZSDM31520")
    page.get_by_role("button", name="Go").click()
    _shell(page).get_by_role("radio", name="Document select").wait_for(state="visible")
    _shell(page).get_by_role("radio", name="Document select").click()
    _shell(page).get_by_role("textbox", name="Sales Document", exact=True).click()
    _shell(page).get_by_role("textbox", name="Sales Document", exact=True).fill(_so_number)
    _shell(page).get_by_role("button", name="Execute  Emphasized").click()
    _shell(page).get_by_role("gridcell", name="To select a row, press the").click()
    _shell(page).get_by_role("button", name="Create P/I").click()
    _shell(page).get_by_role("button", name="Print P/I").click()
    _shell(page).get_by_role("textbox", name="Output Device Required").click()
    _shell(page).get_by_role("textbox", name="Output Device Required").fill("zpdf")
    _shell(page).get_by_role("textbox", name="Output Device Required").press("Enter")
    page.wait_for_timeout(1000)

    # F8 = Print preview — PDF viewer loads in current page iframes (no popup)
    print("[RPA] Pressing F8 for Print preview...")
    page.keyboard.press("F8")

    # itshtmlvwr iframe contains the PDF viewer; inner iframe has a random hex name
    _pdf_frame = page.frame_locator('iframe[name^="itshtmlvwr"]').frame_locator("iframe")
    print("[RPA] Waiting for PDF download button...")
    _pdf_frame.locator("#save").wait_for(state="visible")
    with page.expect_download() as _dl2_info:
        _pdf_frame.locator("#save").click()
    _dl2 = _dl2_info.value
    _dl2.save_as(os.path.join(_DL_DIR, _dl2.suggested_filename))
    print(f"[RPA] PDF saved: {_dl2.suggested_filename}")

    context.close()
