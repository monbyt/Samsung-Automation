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
    # SO number changes every run — read from grid before leaving this screen
    _shell = page.locator("iframe[name=\"application-Shell-startGUI-iframe\"]").content_frame
    _so_cell = _shell.locator("#C111-mrss-cont-none-Row-0").get_by_text(re.compile(r"\d{10,}"))
    _so_number = _so_cell.inner_text().strip()
    print(f"[RPA] Captured SO number: {_so_number}")

    # Reset to home so Search Program is fresh (avoids stuck ZLSDF50270)
    page.goto("https://nerpsr.sec.samsung.net/sap/bc/ui2/flp#Utility-home")
    page.get_by_role("textbox", name="Search Program").wait_for(state="visible")
    _search = page.get_by_role("textbox", name="Search Program")
    _search.click()
    _search.fill("ZSDM31520")
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
    page.wait_for_timeout(1000)

    # F8 = Print preview shortcut in SAP — opens the PDF viewer popup
    _pdf_page = None
    with page.expect_popup() as _popup_info:
        page.keyboard.press("F8")
    _pdf_page = _popup_info.value
    _pdf_page.wait_for_load_state("load")
    page.wait_for_timeout(3000)

    # Debug: print all open pages so we can find the real PDF page
    for i, p in enumerate(context.pages):
        print(f"[RPA DEBUG] page[{i}] url={p.url}")
        for j, f in enumerate(p.frames):
            print(f"[RPA DEBUG]   frame[{j}] name={f.name!r} url={f.url}")

    # #save is inside Shadow DOM — use aria-label which Playwright pierces automatically
    _pdf_page.locator("[aria-label='Download']").wait_for(state="visible")
    with _pdf_page.expect_download() as download1_info:
        _pdf_page.locator("[aria-label='Download']").click()
    download1 = download1_info.value
    page.close()

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
