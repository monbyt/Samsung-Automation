"""
final_oc_multi — copy of final_oc that processes EVERY sales order
created on the ZLSDF50270 result grid (not just row 0).

final_oc.py is left untouched.
"""
import re
from playwright.sync_api import Playwright, sync_playwright

SHELL_IFRAME = 'iframe[name="application-Shell-startGUI-iframe"]'
SO_RE = re.compile(r"\d{10,}")


# SAP WebGUI cells look like:
#   grid#C111#0,1          ← real cell (most common in the DOM dump)
#   grid#C111#25,8@if-r    ← input overlay variant
# NOT helper nodes like:
#   grid#C111#0,1-ROOTCNT / -CONTENT-1 / -SELCOLTOGGLE / #cp1
# Column for SO is auto-detected (whichever has 10+ digit values).
# Many SO-column cells are EMPTY (line-item padding) — skip blanks.
GRID_CELL_ID_RE = re.compile(r"^grid#C\d+#(\d+),(\d+)(?:@[\w-]+)?$")


def _shell(page):
    return page.locator(SHELL_IFRAME).content_frame


def _read_grid_cells(shell) -> list[tuple[str, str]]:
    """Return [(id, text), ...] for every grid# node in the shell document.

    One evaluate call — do not nth() thousands of Playwright locators.
    """
    # No object-literal braces; returns a list of [id, text] pairs.
    return shell.locator(":root").evaluate(
        "() => Array.from(document.querySelectorAll('[id*=\"grid#\"]'))"
        ".map(n => [n.id, (n.innerText || n.textContent || '').trim()])"
    ) or []


def _parse_row_col(cid: str):
    m = GRID_CELL_ID_RE.match(cid or "")
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _detect_so_column(cells: list[tuple[str, str]]) -> int | None:
    """Pick the column that contains the most 10+ digit SO-looking values."""
    hits: dict[int, int] = {}
    examples: dict[int, list[str]] = {}
    parsed = 0
    nonempty_cols: dict[int, int] = {}
    for cid, text in cells:
        row, col = _parse_row_col(cid)
        if col is None:
            continue
        parsed += 1
        if text:
            nonempty_cols[col] = nonempty_cols.get(col, 0) + 1
        so_m = SO_RE.search(text or "")
        if not so_m:
            continue
        hits[col] = hits.get(col, 0) + 1
        examples.setdefault(col, [])
        if len(examples[col]) < 3:
            examples[col].append(so_m.group(0))
    print(f"[RPA] Parseable data cells: {parsed}")
    if nonempty_cols:
        print(f"[RPA] Non-empty cells by column: {dict(sorted(nonempty_cols.items()))}")
    if not hits:
        return None
    best = max(hits, key=hits.get)
    print(f"[RPA] SO hits by column: {dict(sorted(hits.items()))}")
    print(f"[RPA] Using column {best} (examples: {examples.get(best)})")
    return best


def _capture_all_so_numbers(page) -> list[str]:
    """Scan the SO column (auto-detected) including empty padding cells."""
    shell = _shell(page)
    page.wait_for_timeout(2000)

    # Focus grid for keyboard scroll
    try:
        shell.locator('xpath=//*[contains(@id,"grid#")]').first.click(force=True)
    except Exception as e:
        print(f"[RPA] Grid focus skipped: {e}")

    seen_ids: set[str] = set()
    found: list[tuple[int, str]] = []  # (row, so)
    so_col: int | None = None
    stagnant = 0

    for pass_num in range(40):
        cells = _read_grid_cells(shell)
        print(f"[RPA] Pass {pass_num + 1}: {len(cells)} grid# nodes")

        if pass_num == 0 and cells:
            print(f"[RPA] Sample ids: {[c[0] for c in cells[:12]]}")

        if so_col is None:
            so_col = _detect_so_column(cells)
            if so_col is None:
                print("[RPA] No SO-looking values yet; scrolling…")
            else:
                print(f"[RPA] Locked onto SO column {so_col}")

        if so_col is None:
            # Can't filter a column yet — scroll and try again
            try:
                page.keyboard.press("PageDown")
                page.wait_for_timeout(400)
            except Exception as e:
                print(f"[RPA] Grid scroll skipped: {e}")
                break
            stagnant += 1
            if stagnant >= 6:
                print("[RPA] Gave up scrolling with no SO column detected")
                break
            continue

        new_ids = 0
        for cid, text in cells:
            if not cid or cid in seen_ids:
                continue
            row, col = _parse_row_col(cid)
            if col is None or col != so_col:
                continue
            seen_ids.add(cid)
            new_ids += 1

            if not text:
                continue  # empty padding between SOs
            so_m = SO_RE.search(text)
            if not so_m:
                continue
            so = so_m.group(0)
            if so not in {s for _, s in found}:
                found.append((row if row is not None else 0, so))
                print(f"[RPA] Cell {cid} → SO {so}")

        try:
            page.keyboard.press("PageDown")
            page.wait_for_timeout(400)
        except Exception as e:
            print(f"[RPA] Grid scroll skipped: {e}")
            break

        if new_ids == 0:
            stagnant += 1
        else:
            stagnant = 0
        if stagnant >= 3:
            print("[RPA] No new cells after scrolling — done scanning")
            break

    found.sort(key=lambda t: t[0])
    so_numbers: list[str] = []
    for _, so in found:
        if so not in so_numbers:
            so_numbers.append(so)

    if not so_numbers:
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
    # NERP only — do NOT goto sts.secsso.net
    page.goto("https://nerpsr.sec.samsung.net/sap/bc/ui2/flp#Utility-home")
    page.get_by_role("textbox", name="User Account").click()
    page.get_by_role("textbox", name="User Account").fill("m.tasoglu")
    page.get_by_role("textbox", name="Password").click()
    page.get_by_role("textbox", name="Password").fill("Pass2002?")
    page.get_by_role("button", name="Login").click()
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
