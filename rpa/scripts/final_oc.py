import re
from playwright.sync_api import Playwright, sync_playwright


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(channel="chrome", headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://sts.secsso.net/adfs/ls/")
    page.get_by_role("textbox", name="User Account").click()
    page.get_by_role("textbox", name="User Account").fill("m.tasoglu")
    page.get_by_role("textbox", name="Password").click()
    page.get_by_role("textbox", name="Password").fill("Pass2002?")
    page.get_by_role("button", name="Login").click()
    page.goto("https://nerpsr.sec.samsung.net/sap/bc/ui2/flp#Utility-home")
    page.get_by_role("textbox", name="Search Program").click()
    page.get_by_role("textbox", name="Search Program").fill("ZLSDF50270")
    page.get_by_role("button", name="Go").click()
    page.locator("iframe[name=\"application-Shell-startGUI-iframe\"]").content_frame.get_by_role("textbox", name="Sales Org.").click()
    page.locator("iframe[name=\"application-Shell-startGUI-iframe\"]").content_frame.get_by_role("textbox", name="Sales Org.").fill("7101")
    page.locator("iframe[name=\"application-Shell-startGUI-iframe\"]").content_frame.get_by_role("textbox", name="Sales Org.").click()
    page.locator("iframe[name=\"application-Shell-startGUI-iframe\"]").content_frame.get_by_role("textbox", name="Upload file Required").click()
    page.locator("iframe[name=\"application-Shell-startGUI-iframe\"]").content_frame.locator("#ls-inputfieldhelpbutton").click()
    page.locator("iframe[name=\"application-Shell-startGUI-iframe\"]").content_frame.get_by_role("button", name="OK").click()
    page.locator("iframe[name=\"application-Shell-startGUI-iframe\"]").content_frame.locator("#webgui_filebrowser_file_upload").set_input_files("ZLSDF50270LAYOUT.XLSX")
    page.locator("iframe[name=\"application-Shell-startGUI-iframe\"]").content_frame.get_by_role("button", name="Execute  Emphasized").click()
    page.locator("iframe[name=\"application-Shell-startGUI-iframe\"]").content_frame.get_by_role("button", name="Create Sales Order").click()
    with page.expect_download() as download_info:
        page.locator("iframe[name=\"application-Shell-startGUI-iframe\"]").content_frame.get_by_role("button", name="Yes").click()
    download = download_info.value
    # SO number changes every run — copy from grid, paste into ZSDM31520 Sales Document
    _shell = page.locator("iframe[name=\"application-Shell-startGUI-iframe\"]").content_frame
    _so_cell = _shell.locator("#C111-mrss-cont-none-Row-0").get_by_text(re.compile(r"\d{10,}"))
    _so_number = _so_cell.inner_text().strip()
    _so_cell.click()
    _so_cell.click()
    # Force-clear Search Program so Go opens ZSDM31520, not the previous ZLSDF50270
    _search = page.get_by_role("textbox", name="Search Program")
    _search.click()
    _search.press("ControlOrMeta+A")
    _search.fill("ZSDM31520")
    # Wait until the field actually has the value, then Go twice
    for _ in range(20):
        if _search.input_value().strip() == "ZSDM31520":
            break
        _search.fill("ZSDM31520")
        page.wait_for_timeout(200)
    page.get_by_role("button", name="Go").click()
    page.get_by_role("button", name="Go").click()
    _shell = page.locator("iframe[name=\"application-Shell-startGUI-iframe\"]").content_frame
    _shell.get_by_role("radio", name="Document select").wait_for(state="visible")
    _shell.get_by_role("radio", name="Document select").click()
    _shell.get_by_role("textbox", name="Sales Document", exact=True).click()
    _shell.get_by_role("textbox", name="Sales Document", exact=True).fill(_so_number)
    _shell.get_by_role("button", name="Execute  Emphasized").click()
    _shell.get_by_role("gridcell", name="To select a row, press the").click()
    _shell.get_by_role("button", name="Create P/I").click()
    _shell.get_by_role("button", name="Print P/I").click()
    _shell.get_by_role("textbox", name="Output Device Required").click()
    _shell.get_by_role("textbox", name="Output Device Required").fill("zpdf")
    _shell.get_by_role("textbox", name="Output Device Required").press("Enter")
    _print_preview = _shell.get_by_role("button", name=re.compile(r"Print\s*preview", re.I))
    _print_preview.wait_for(state="visible")
    _print_preview.click()
    # PDF viewer iframes are session-specific — poll every frame for Download
    _dl_btn = None
    for _ in range(60):
        for _frame in page.frames:
            for _sel in (
                _frame.get_by_role("button", name=re.compile(r"Download", re.I)),
                _frame.get_by_text(re.compile(r"^Download$", re.I)),
            ):
                try:
                    if _sel.count() > 0 and _sel.first.is_visible():
                        _dl_btn = _sel.first
                        break
                except Exception:
                    continue
            if _dl_btn:
                break
        if _dl_btn:
            break
        page.wait_for_timeout(500)
    if not _dl_btn:
        raise RuntimeError("PDF Download button not found in any frame")
    with page.expect_download() as download1_info:
        _dl_btn.click(force=True)
    download1 = download1_info.value
    page.close()

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
