"""
final_oc_multi — copy of final_oc that processes EVERY sales order
created on the ZLSDF50270 result grid (not just row 0).

final_oc.py is left untouched.
"""
import re
from playwright.sync_api import Playwright, sync_playwright

SHELL_IFRAME = 'iframe[name="application-Shell-startGUI-iframe"]'
SO_RE = re.compile(r"\d{10,}")


# SAP WebGUI SO column cells look like: id="grid#C111#25,8@if-r"
# Row index increases down the column; column ",8@" is the SO number.
# Many rows are EMPTY (line-item padding) — walk the whole column, skip blanks.
SO_CELL_ID_RE = re.compile(r"grid#C\d+#(\d+),8@")


def _shell(page):
    return page.locator(SHELL_IFRAME).content_frame


def _capture_all_so_numbers(page) -> list[str]:
    """Walk the entire SO column (incl. empty cells) and collect unique SOs."""
    shell = _shell(page)
    page.wait_for_timeout(2000)

    # XPath — CSS [id^="grid#..."] is unreliable because '#' is special in CSS.
    col8 = shell.locator(
        'xpath=//*[contains(@id,"grid#") and contains(@id,",8@")]'
    )
    any_grid = shell.locator('xpath=//*[contains(@id,"grid#")]')
    print(f"[RPA] Any grid# nodes: {any_grid.count()}")
    print(f"[RPA] Column-8 nodes: {col8.count()}")
    if any_grid.count() > 0 and col8.count() == 0:
        for i in range(min(20, any_grid.count())):
            print(f"[RPA] sample id[{i}]: {any_grid.nth(i).get_attribute('id')!r}")

    seen_ids: set[str] = set()
    found: list[tuple[int, str]] = []  # (row_index, so_number)
    stagnant = 0

    # Focus the grid so keyboard scrolling works
    try:
        if col8.count() > 0:
            col8.first.click(force=True)
        elif any_grid.count() > 0:
            any_grid.first.click(force=True)
    except Exception as e:
        print(f"[RPA] Grid focus skipped: {e}")

    for pass_num in range(40):
        nodes = col8 if col8.count() > 0 else any_grid
        count = nodes.count()
        new_this_pass = 0
        print(f"[RPA] Column scan pass {pass_num + 1}: {count} nodes visible")

        for i in range(count):
            cell = nodes.nth(i)
            cid = cell.get_attribute("id") or ""
            if not cid or cid in seen_ids:
                continue
            # SO column only (",8@" in id) — skip other columns / empty padding handled below
            if ",8@" not in cid:
                continue
            seen_ids.add(cid)
            new_this_pass += 1

            text = (cell.inner_text() or "").strip()
            # Empty padding cells between SOs — keep walking, just skip
            if not text:
                continue
            so_m = SO_RE.search(text)
            if not so_m:
                continue
            so = so_m.group(0)
            m = SO_CELL_ID_RE.search(cid)
            row_idx = int(m.group(1)) if m else i
            if so not in {s for _, s in found}:
                found.append((row_idx, so))
                print(f"[RPA] Cell {cid} → SO {so}")

        # Scroll further down the column (virtualized rows)
        try:
            if count > 0:
                nodes.last.scroll_into_view_if_needed()
            page.keyboard.press("PageDown")
            page.wait_for_timeout(350)
        except Exception as e:
            print(f"[RPA] Grid scroll skipped: {e}")
            break

        if new_this_pass == 0:
            stagnant += 1
        else:
            stagnant = 0
        if stagnant >= 3:
            print("[RPA] No new column cells after scrolling — done scanning")
            break

    found.sort(key=lambda t: t[0])
    so_numbers: list[str] = []
    for _, so in found:
        if so not in so_numbers:
            so_numbers.append(so)

    if not so_numbers:
        # Fallback: every 10+ digit token visible in the shell (deduped)
        try:
            shell_text = shell.locator("body").inner_text()
        except Exception:
            shell_text = shell.locator(":root").inner_text()
        so_numbers = list(dict.fromkeys(SO_RE.findall(shell_text)))
        print(f"[RPA] Fallback shell-text SOs: {so_numbers}")

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
