"""
final_oc_multi — copy of final_oc that processes EVERY sales order
created on the ZLSDF50270 result grid (not just row 0).

final_oc.py is left untouched.
"""
import re
from playwright.sync_api import Playwright, sync_playwright

SHELL_IFRAME = 'iframe[name="application-Shell-startGUI-iframe"]'
SO_RE = re.compile(r"\d{10,}")


def _shell(page):
    return page.locator(SHELL_IFRAME).content_frame


def _capture_all_so_numbers(page) -> list[str]:
    """Read every 10+ digit SO from the Create Sales Order result grid.

    Does not rely on the hardcoded C111 row id from the original recording —
    SAP regenerates those container ids. Strategy:
      1) any mrss row container
      2) fallback: all 10+ digit numbers visible in the shell iframe
    """
    shell = _shell(page)
    # Give the result grid a moment to finish rendering all rows
    page.wait_for_timeout(1500)

    # Try scrolling the grid so virtualized rows mount in the DOM
    try:
        grid = shell.locator('[id*="mrss-cont"]').first
        grid.evaluate(
            """el => {
                el.scrollTop = el.scrollHeight;
                const p = el.parentElement;
                if (p) p.scrollTop = p.scrollHeight;
            }"""
        )
        page.wait_for_timeout(500)
        grid.evaluate(
            """el => {
                el.scrollTop = 0;
                const p = el.parentElement;
                if (p) p.scrollTop = 0;
            }"""
        )
        page.wait_for_timeout(500)
    except Exception as e:
        print(f"[RPA] Grid scroll skipped: {e}")

    so_numbers: list[str] = []

    # 1) Prefer per-row scrape (any C###-mrss-cont-*-Row-N, not just C111)
    rows = shell.locator('[id*="mrss-cont"][id*="Row-"]')
    row_count = rows.count()
    row_ids = []
    for i in range(row_count):
        rid = rows.nth(i).get_attribute("id") or ""
        row_ids.append(rid)
        text = rows.nth(i).inner_text()
        for m in SO_RE.finditer(text):
            so = m.group(0)
            if so not in so_numbers:
                so_numbers.append(so)

    print(f"[RPA] Grid row elements: {row_count}")
    if row_ids:
        print(f"[RPA] Row ids (first 20): {row_ids[:20]}")

    # 2) Fallback / supplement: every 10+ digit token in the shell text
    try:
        shell_text = shell.locator("body").inner_text(timeout=5_000)
    except Exception:
        shell_text = shell.locator(":root").inner_text(timeout=5_000)
    from_text = SO_RE.findall(shell_text)
    for so in from_text:
        if so not in so_numbers:
            so_numbers.append(so)

    print(f"[RPA] SO candidates from shell text: {from_text}")
    return so_numbers


def _open_zsdm31520(page) -> None:
    # Same Utility-home goto as final_oc.py
    page.goto("https://nerpsr.sec.samsung.net/sap/bc/ui2/flp#Utility-home")
    page.get_by_role("textbox", name="Search Program").wait_for(state="visible")
    _search = page.get_by_role("textbox", name="Search Program")
    _search.click()
    _search.fill("ZSDM31520")
    page.get_by_role("button", name="Go").click()


def _download_pdf(page) -> None:
    """F8 → chrome-extension PDF viewer → Download (two clicks)."""
    page.keyboard.press("F8")
    pdf_frame = None
    for _ in range(20):
        for f in page.frames:
            if f.url.startswith("chrome-extension://"):
                pdf_frame = f
                break
        if pdf_frame:
            break
        page.wait_for_timeout(500)
    if not pdf_frame:
        raise RuntimeError("Chrome PDF viewer frame not found")

    pdf_frame.locator("[aria-label='Download']").wait_for(state="visible")
    pdf_frame.locator("[aria-label='Download']").click()
    page.wait_for_timeout(500)
    with page.expect_download() as download_info:
        pdf_frame.locator("[aria-label='Download']").click()
    _ = download_info.value


def _process_so(page, so_number: str) -> None:
    """ZSDM31520 Document select → fill SO → Create P/I → Print → PDF download."""
    print(f"[RPA] Processing SO {so_number}")
    _open_zsdm31520(page)
    shell = _shell(page)
    shell.get_by_role("radio", name="Document select").wait_for(state="visible")
    shell.get_by_role("radio", name="Document select").click()
    shell.get_by_role("textbox", name="Sales Document", exact=True).click()
    shell.get_by_role("textbox", name="Sales Document", exact=True).fill(so_number)
    shell.get_by_role("button", name="Execute  Emphasized").click()
    shell.get_by_role("gridcell", name="To select a row, press the").click()
    shell.get_by_role("button", name="Create P/I").click()
    shell.get_by_role("button", name="Print P/I").click()
    shell.get_by_role("textbox", name="Output Device Required").click()
    shell.get_by_role("textbox", name="Output Device Required").fill("zpdf")
    shell.get_by_role("textbox", name="Output Device Required").press("Enter")
    page.wait_for_timeout(1000)
    _download_pdf(page)
    print(f"[RPA] Finished SO {so_number}")


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

    # Capture ALL SO numbers from the result grid (not just Row-0)
    so_numbers = _capture_all_so_numbers(page)
    print(f"[RPA] Captured {len(so_numbers)} SO(s): {so_numbers}")
    if not so_numbers:
        raise RuntimeError("No sales order numbers found in the result grid")

    # Process each SO one by one (re-open ZSDM31520 each time for a clean screen)
    for so in so_numbers:
        _process_so(page, so)

    page.close()

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
