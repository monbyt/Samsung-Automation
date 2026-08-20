"""
final_oc_multi — copy of final_oc that processes EVERY sales order
created on the ZLSDF50270 result grid (not just row 0).

final_oc.py is left untouched.
"""
import os
import re
from playwright.sync_api import Playwright, sync_playwright

SHELL_IFRAME = 'iframe[name="application-Shell-startGUI-iframe"]'
SO_RE = re.compile(r"\d{10,}")

# sha256 digests of PDFs saved in this browser run — same bytes twice = wrong print reused
_PDF_HASHES_THIS_RUN: list[str] = []


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
    # Live NERP Shell-home
    page.goto("https://nerps.sec.samsung.net/sap/bc/ui2/flp#Shell-home")
    page.get_by_role("textbox", name="Search Program").wait_for(state="visible")
    _search = page.get_by_role("textbox", name="Search Program")
    _search.click()
    _search.fill("ZSDM31520")
    page.get_by_role("button", name="Go").click()


def _save_playwright_download(dl, dest_dir: str, filename: str | None = None) -> str:
    """Save a Playwright download to dest_dir. Returns the path written."""
    if not dest_dir:
        raise RuntimeError("No destination folder for download")
    os.makedirs(dest_dir, exist_ok=True)
    name = filename or dl.suggested_filename or "download.bin"
    path = os.path.join(dest_dir, name)
    if os.path.exists(path):
        stem, ext = os.path.splitext(name)
        path = os.path.join(dest_dir, f"{stem}_{os.getpid()}{ext}")
    dl.save_as(path)
    print(f"[RPA] Saved download: {path}")
    return path


def _excel_dir() -> str:
    """Where Create Sales Order .xlsx results go (Upload folder — NOT the PDF folder)."""
    return (
        os.environ.get("RPA_UPLOAD_DIR")
        or os.environ.get("RPA_DOWNLOAD_DIR")
        or os.getcwd()
    )


def _pdf_dir() -> str:
    """Where P/I PDFs go (Download folder — email attach folder)."""
    return os.environ.get("RPA_DOWNLOAD_DIR") or ""


def _pdf_sha256(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_pdf(page, so_number: str = "") -> None:
    """F8 → chrome-extension PDF viewer → Download → save under PDF (download) folder only."""
    # Close any leftover PDF viewer tabs from a previous SO so Download cannot
    # grab the wrong printout (classic mismatch: SO A processed, PDF B attached).
    try:
        for p in list(page.context.pages):
            if p == page:
                continue
            try:
                p.close()
            except Exception:
                pass
    except Exception as e:
        print(f"[RPA] PDF tab cleanup skipped: {e}")

    page.keyboard.press("F8")
    pdf_frame = None
    for _ in range(30):
        for f in page.frames:
            if (f.url or "").startswith("chrome-extension://"):
                pdf_frame = f
                break
        if pdf_frame:
            break
        page.wait_for_timeout(500)
    if not pdf_frame:
        raise RuntimeError("Chrome PDF viewer frame not found")

    pdf_frame.locator("[aria-label='Download']").wait_for(state="visible")
    # One click only — a second Download was creating duplicate identical PDFs.
    with page.expect_download() as pdf_info:
        pdf_frame.locator("[aria-label='Download']").click()
    pdf_dl = pdf_info.value

    dest_dir = _pdf_dir()
    if not dest_dir:
        print("[RPA] WARNING: RPA_DOWNLOAD_DIR not set — PDF not saved to disk")
        return

    suggested = pdf_dl.suggested_filename or "pi.pdf"
    stem, ext = os.path.splitext(suggested)
    if not ext:
        ext = ".pdf"
    fname = f"{stem}_{so_number}{ext}" if so_number else suggested
    path = _save_playwright_download(pdf_dl, dest_dir, fname)
    digest = _pdf_sha256(path)
    size = os.path.getsize(path)
    print(f"[RPA] PDF for SO {so_number or '?'} → {path} ({size} bytes, sha={digest[:16]})")
    if digest in _PDF_HASHES_THIS_RUN:
        try:
            os.remove(path)
        except OSError:
            pass
        raise RuntimeError(
            f"PDF for SO {so_number} is byte-identical to an earlier print in this run. "
            "SAP/Chrome reused the same document (wrong vendor). Refusing to keep it."
        )
    _PDF_HASHES_THIS_RUN.append(digest)
    try:
        pdf_page = pdf_frame.page
        if pdf_page != page:
            pdf_page.close()
    except Exception:
        pass


def _shell_status_text(shell) -> str:
    """Best-effort SAP status / shell text for error detection."""
    try:
        return (shell.locator("body").inner_text(timeout=2000) or "")[:4000]
    except Exception:
        try:
            return (shell.locator(":root").inner_text(timeout=2000) or "")[:4000]
        except Exception:
            return ""


def _fill_sales_document(shell, page, so_number: str) -> None:
    """Type SO like a human and leave the field so WebGUI commits it before Execute."""
    sales_doc = shell.get_by_role("textbox", name="Sales Document", exact=True)
    sales_doc.wait_for(state="visible")
    sales_doc.click()
    page.wait_for_timeout(300)
    try:
        sales_doc.press("Control+A")
        sales_doc.press("Backspace")
    except Exception:
        pass
    sales_doc.fill("")
    page.wait_for_timeout(200)
    sales_doc.fill(so_number)
    sales_doc.press("Tab")
    page.wait_for_timeout(1000)
    try:
        got = (sales_doc.input_value() or "").strip().replace(" ", "")
    except Exception:
        got = ""
    print(f"[RPA] Sales Document field after commit: {got!r} (wanted {so_number!r})")
    if so_number not in got:
        raise RuntimeError(
            f"Sales Document field did not keep SO {so_number} (got {got!r}). "
            "Aborting before Create P/I so we do not print the wrong vendor."
        )


def _process_so(page, so_number: str) -> None:
    """ZSDM31520 Document select → fill SO → Create P/I → Print → PDF download."""
    print(f"[RPA] Processing SO {so_number}")
    print("[RPA] Waiting 8s for live SO to be available in ZSDM31520…")
    page.wait_for_timeout(8000)
    _open_zsdm31520(page)
    shell = _shell(page)
    shell.get_by_role("radio", name="Document select").wait_for(state="visible")
    shell.get_by_role("radio", name="Document select").click()
    page.wait_for_timeout(500)
    _fill_sales_document(shell, page, so_number)
    shell.get_by_role("button", name="Execute  Emphasized").click()
    print(f"[RPA] Waiting for ZSDM31520 result row after Execute (SO {so_number})")
    row = shell.get_by_role("gridcell", name="To select a row, press the")
    ready = False
    for i in range(60):
        try:
            if row.count() > 0 and row.first.is_visible():
                ready = True
                break
        except Exception:
            pass
        status = _shell_status_text(shell)
        if re.search(r"CHECK\s+input\s+s/?o", status, re.I):
            raise RuntimeError(
                f"SAP rejected SO {so_number} after Execute: CHECK input s/o no."
            )
        if i in (5, 15, 30):
            print(f"[RPA] Still waiting for result row… ({i}s)")
        page.wait_for_timeout(1000)
    if not ready:
        raise RuntimeError(
            f"ZSDM31520 result row not visible within 60s after Execute for SO {so_number}"
        )
    # Row visible = Execute returned a selectable document. Do NOT require the SO
    # digits in body.inner_text() — live WebGUI virtualizes the grid and that
    # check false-failed even when Document select worked.
    page.wait_for_timeout(500)
    print(f"[RPA] Result row visible after Execute for SO {so_number} — Create P/I")
    row.first.click()
    create_pi = shell.get_by_role("button", name="Create P/I")
    for _ in range(30):
        try:
            if create_pi.count() > 0 and create_pi.first.is_visible():
                break
        except Exception:
            pass
        page.wait_for_timeout(1000)
    create_pi.first.click()
    shell.get_by_role("button", name="Print P/I").click()
    shell.get_by_role("textbox", name="Output Device Required").click()
    shell.get_by_role("textbox", name="Output Device Required").fill("zpdf")
    shell.get_by_role("textbox", name="Output Device Required").press("Enter")
    page.wait_for_timeout(1500)
    _download_pdf(page, so_number)
    print(f"[RPA] Finished SO {so_number}")


def run(playwright: Playwright) -> None:
    global _PDF_HASHES_THIS_RUN
    _PDF_HASHES_THIS_RUN = []
    browser = playwright.chromium.launch(channel="chrome", headless=False)
    context = browser.new_context()
    page = context.new_page()
    # NERP only — do NOT goto sts.secsso.net
    page.goto("https://nerps.sec.samsung.net/sap/bc/ui2/flp#Shell-home")
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
    # Excel result → Upload folder (keep PDFs-only Download folder clean for email)
    with page.expect_download() as excel_info:
        page.locator("iframe[name=\"application-Shell-startGUI-iframe\"]").content_frame.get_by_role("button", name="Yes").click()
    excel_dl = excel_info.value
    try:
        _save_playwright_download(
            excel_dl,
            _excel_dir(),
            excel_dl.suggested_filename or "ZLSDF50270RESULT.XLSX",
        )
    except Exception as e:
        print(f"[RPA] Excel result save failed: {e}")

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
