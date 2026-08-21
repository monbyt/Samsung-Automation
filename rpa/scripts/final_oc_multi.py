"""
final_oc_multi — copy of final_oc that processes EVERY sales order
created on the ZLSDF50270 result grid (not just row 0).

final_oc.py is left untouched.
"""
import os
import re
import time
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


def _decode_pdf_hex_bytes(data: bytes) -> str:
    """Decode PDF hex-string payload (ASCII or UTF-16)."""
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", errors="ignore")
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le", errors="ignore")
    # UTF-16BE without BOM (null before every ASCII digit is common in SAP PDFs)
    if len(data) >= 4 and data[0] == 0 and data[2] == 0:
        try:
            return data.decode("utf-16-be", errors="ignore")
        except Exception:
            pass
    return data.decode("latin-1", errors="ignore")


def _pdf_text_blob(path: str) -> str:
    """Extract readable strings from a PDF (literals + hex strings). Never scan raw binary.

    SAP P/I PDFs often store digits as hex, e.g. <31333630373035313037> → '1360705107'.
    Searching the raw file for SO digits false-fails and invents garbage 'numbers'.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    parts: list[str] = []

    for m in re.finditer(rb"<([0-9A-Fa-f\r\n\t ]+)>", raw):
        hx = re.sub(rb"\s+", b"", m.group(1))
        if len(hx) < 8 or len(hx) % 2:
            continue
        try:
            parts.append(_decode_pdf_hex_bytes(bytes.fromhex(hx.decode("ascii"))))
        except ValueError:
            continue

    for m in re.finditer(rb"\((?:\\.|[^\\)]){2,240}\)", raw):
        s = m.group(0)[1:-1].decode("latin-1", errors="ignore")
        s = (
            s.replace(r"\n", "\n")
            .replace(r"\r", "\r")
            .replace(r"\t", "\t")
            .replace(r"\(", "(")
            .replace(r"\)", ")")
            .replace(r"\\", "\\")
        )
        parts.append(s)

    return "\n".join(parts)


def _pdf_long_numbers(path: str) -> list[str]:
    blob = _pdf_text_blob(path)
    # Prefer real document numbers (10–12 digits); drop longer hex leftovers if any.
    nums = []
    for n in SO_RE.findall(blob):
        if 10 <= len(n) <= 12:
            nums.append(n)
    return list(dict.fromkeys(nums))


def _pdf_contains_so(path: str, so_number: str) -> bool:
    if not so_number:
        return True
    blob = _pdf_text_blob(path)
    if so_number in blob:
        return True
    # Digits sometimes split by spaces/newlines in extracted text
    compact = re.sub(r"\D+", "", blob)
    return so_number in compact


def _close_extra_pages(page) -> None:
    """Close leftover tabs so Download cannot hit a previous P/I viewer."""
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


def _chrome_pdf_frame_urls(page) -> set[str]:
    urls: set[str] = set()
    for f in page.frames:
        u = f.url or ""
        if u.startswith("chrome-extension://"):
            urls.add(u)
    return urls


def _wait_new_pdf_frame(page, before_urls: set[str], timeout_s: int = 15):
    """Wait briefly for a chrome-extension PDF frame. Fail fast — do not block the print lock."""
    deadline = max(1, int(timeout_s * 2))  # 500ms steps
    for _ in range(deadline):
        for f in page.frames:
            u = f.url or ""
            if not u.startswith("chrome-extension://"):
                continue
            if u not in before_urls or not before_urls:
                return f
        page.wait_for_timeout(500)
    for f in page.frames:
        if (f.url or "").startswith("chrome-extension://"):
            print("[RPA] WARNING: reusing pre-existing Chrome PDF frame (may be stale)")
            return f
    return None


class _PdfPrintLock:
    """Serialize Print→Download briefly so parallel workers don't share zpdf at once.

    Must fail fast: a stuck PDF viewer must NOT hold this lock for minutes
    (that freezes every other worker).
    """

    def __init__(self, lock_path: str, timeout_s: float = 120.0):
        self.lock_path = lock_path
        self.timeout_s = timeout_s
        self._fd = None

    def __enter__(self):
        parent = os.path.dirname(self.lock_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        start = time.time()
        while True:
            try:
                self._fd = os.open(
                    self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
                os.write(self._fd, f"{os.getpid()}\n".encode())
                print(f"[RPA] Acquired PDF print lock (pid={os.getpid()})")
                return self
            except FileExistsError:
                if time.time() - start > self.timeout_s:
                    raise RuntimeError(
                        f"Timed out waiting for PDF print lock: {self.lock_path}"
                    )
                try:
                    # Stuck viewer used to hold the lock forever; steal after 60s.
                    age = time.time() - os.path.getmtime(self.lock_path)
                    if age > 60:
                        print(f"[RPA] Stealing stale PDF print lock (age={age:.0f}s)")
                        os.remove(self.lock_path)
                        continue
                except OSError:
                    pass
                time.sleep(1.0)

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._fd is not None:
                os.close(self._fd)
        except OSError:
            pass
        self._fd = None
        try:
            os.remove(self.lock_path)
            print("[RPA] Released PDF print lock")
        except OSError:
            pass
        return False


def _pdf_print_lock():
    base = (
        os.environ.get("RPA_DOWNLOAD_DIR")
        or os.environ.get("RPA_UPLOAD_DIR")
        or os.getcwd()
    )
    parent = os.path.dirname(base.rstrip("\\/")) or base
    if os.path.basename(base).startswith("_worker_"):
        lock_dir = parent
    else:
        lock_dir = base
    return _PdfPrintLock(os.path.join(lock_dir, ".pdf_print.lock"))


def _download_pdf(page, so_number: str = "", before_urls: set[str] | None = None) -> None:
    """F8 → Download fast → verify SO. Short timeouts so the print lock is not held forever."""
    dest_dir = _pdf_dir()
    if not dest_dir:
        print("[RPA] WARNING: RPA_DOWNLOAD_DIR not set — PDF not saved to disk")
        return

    before_urls = set(before_urls or ())
    _close_extra_pages(page)

    # Old working style: F8 immediately. Do not sit 12s hoping for auto-open.
    page.keyboard.press("F8")
    pdf_frame = _wait_new_pdf_frame(page, before_urls, timeout_s=15)
    if not pdf_frame:
        raise RuntimeError("Chrome PDF viewer frame not found (15s)")

    pdf_page = pdf_frame.page
    download_btn = pdf_frame.locator("[aria-label='Download'], #save").first

    visible = False
    for i in range(16):  # max ~8s
        try:
            if download_btn.is_visible():
                visible = True
                break
        except Exception as e:
            msg = str(e).lower()
            if "navigation" in msg or "destroyed" in msg or "target closed" in msg:
                raise RuntimeError(
                    f"PDF viewer went away while waiting for Download: {e}"
                ) from e
        page.wait_for_timeout(500)
    if not visible:
        raise RuntimeError("PDF Download button not visible within 8s (viewer error?)")

    with pdf_page.expect_download(timeout=30_000) as pdf_info:
        download_btn.click(force=True, timeout=8_000)
    pdf_dl = pdf_info.value

    suggested = pdf_dl.suggested_filename or "pi.pdf"
    stem, ext = os.path.splitext(suggested)
    if not ext:
        ext = ".pdf"
    fname = f"{stem}_{so_number}{ext}" if so_number else suggested
    path = _save_playwright_download(pdf_dl, dest_dir, fname)
    digest = _pdf_sha256(path)
    size = os.path.getsize(path)
    numbers = _pdf_long_numbers(path)
    print(
        f"[RPA] PDF for SO {so_number or '?'} → {path} "
        f"({size} bytes, sha={digest[:16]}, nums_in_pdf={numbers[:8]})"
    )

    try:
        if pdf_page != page:
            pdf_page.close()
        else:
            page.keyboard.press("Escape")
    except Exception:
        pass
    _close_extra_pages(page)

    if digest in _PDF_HASHES_THIS_RUN:
        try:
            os.remove(path)
        except OSError:
            pass
        raise RuntimeError(
            f"PDF for SO {so_number} is byte-identical to an earlier print in this run. "
            "SAP/Chrome reused the same document (wrong vendor). Refusing to keep it."
        )

    if so_number and not _pdf_contains_so(path, so_number):
        if numbers:
            try:
                os.remove(path)
            except OSError:
                pass
            raise RuntimeError(
                f"PDF content does not contain SO {so_number} "
                f"(found {numbers[:8]}). Refusing mismatched invoice."
            )
        print(
            f"[RPA] WARNING: no extractable numbers in PDF for SO {so_number} "
            "(image-only?). Keeping file."
        )

    _PDF_HASHES_THIS_RUN.append(digest)


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
    print("[RPA] Waiting 3s for live SO to be available in ZSDM31520…")
    page.wait_for_timeout(3000)
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
    # Hold the print lock for ONE attempt only, then release so other workers
    # are not frozen behind a stuck PDF viewer / retry loop.
    last_err: Exception | None = None
    for attempt in range(1, 3):
        try:
            with _pdf_print_lock():
                _close_extra_pages(page)
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                page.wait_for_timeout(300)

                before_urls = _chrome_pdf_frame_urls(page)
                shell.get_by_role("button", name="Print P/I").click()
                shell.get_by_role("textbox", name="Output Device Required").click()
                shell.get_by_role("textbox", name="Output Device Required").fill("zpdf")
                shell.get_by_role("textbox", name="Output Device Required").press("Enter")
                page.wait_for_timeout(1500)
                _download_pdf(page, so_number, before_urls=before_urls)
            last_err = None
            break
        except Exception as e:
            last_err = e
            print(f"[RPA] Print/download attempt {attempt}/2 failed for SO {so_number}: {e}")
            _close_extra_pages(page)
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            # Lock already released — brief pause before retry so others can print.
            page.wait_for_timeout(1500)
    if last_err is not None:
        raise last_err
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
