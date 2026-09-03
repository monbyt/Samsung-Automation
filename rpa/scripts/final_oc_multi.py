"""
final_oc_multi — copy of final_oc that processes EVERY sales order
created on the ZLSDF50270 result grid (not just row 0).

final_oc.py is left untouched.
"""
import os
import re
from playwright.sync_api import Playwright, sync_playwright

from mail.settings_db import get_nerp_url

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


def _find_search_program(page):
    """Locate the FLP Search Program box (name varies slightly by theme/home)."""
    for name in ("Search Program", "Search", "Search Transactions"):
        loc = page.get_by_role("textbox", name=name)
        try:
            if loc.count() > 0 and loc.first.is_visible():
                return loc.first
        except Exception:
            continue
    return None


def _open_tcode(page, tcode: str, *, force_home: bool = True) -> None:
    """Leave current GUI app, open FLP home, type T-code into Search Program → Go.

    On test (Utility-home) Search is often missing until the shell settles; a bare
    goto + immediate fill was skipping the second T-code (ZSDM31520).
    After login, pass force_home=False if Search is already visible.
    """
    url = get_nerp_url()
    print(f"[RPA] Opening T-code {tcode} via {url} (force_home={force_home})")

    search = None if force_home else _find_search_program(page)
    if not search:
        page.goto(url)
        try:
            page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        page.wait_for_timeout(1500)

        for i in range(30):
            search = _find_search_program(page)
            if search:
                break
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            try:
                canvas = page.locator("#canvas")
                if canvas.count() > 0:
                    canvas.first.click(timeout=1000)
            except Exception:
                pass
            if i in (5, 15, 25):
                print(f"[RPA] Still waiting for Search Program… ({i}s) before {tcode}")
                try:
                    page.goto(url)
                except Exception:
                    pass
            page.wait_for_timeout(1000)

    if not search:
        raise RuntimeError(
            f"FLP Search Program not found — cannot open T-code {tcode}. "
            f"Check NERP env URL ({url})."
        )

    search.click()
    page.wait_for_timeout(200)
    try:
        search.fill("")
    except Exception:
        pass
    search.fill(tcode)
    print(f"[RPA] Typed T-code into Search: {tcode}")
    go = page.get_by_role("button", name="Go")
    try:
        if go.count() > 0 and go.first.is_visible():
            go.first.click()
        else:
            search.press("Enter")
    except Exception:
        search.press("Enter")
    print(f"[RPA] Submitted T-code {tcode}")
    page.wait_for_timeout(2000)


def _open_zsdm31520(page) -> None:
    _open_tcode(page, "ZSDM31520")
    # Confirm the P/I selection screen actually opened (not still on 50270).
    shell = _shell(page)
    try:
        shell.get_by_role("radio", name="Document select").wait_for(
            state="visible", timeout=60_000
        )
        print("[RPA] ZSDM31520 ready (Document select visible)")
    except Exception as e:
        raise RuntimeError(
            "ZSDM31520 did not open — Document select not visible after Search. "
            f"({e})"
        ) from e


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
    """F8 → chrome-extension PDF viewer → Download → save under PDF (download) folder only.

    Critical: never click Download on a leftover chrome-extension iframe from a
    previous SO (that was the …5107 wrong-content bug). Dismiss old viewers,
    F8, then only use a frame that was not open before F8.
    """
    def _pdf_urls() -> set[str]:
        return {
            (f.url or "")
            for f in page.frames
            if (f.url or "").startswith("chrome-extension://")
        }

    # Close popup tabs + dismiss embedded viewer from prior SO.
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
    for _ in range(6):
        if not _pdf_urls():
            break
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        page.wait_for_timeout(250)

    before = _pdf_urls()
    if before:
        print(f"[RPA] WARNING: {len(before)} PDF frame(s) still open before F8")

    page.keyboard.press("F8")
    pdf_frame = None
    for _ in range(30):
        for f in page.frames:
            u = f.url or ""
            if not u.startswith("chrome-extension://"):
                continue
            # Prefer a brand-new viewer; if none existed before, first one is OK.
            if u not in before or not before:
                pdf_frame = f
                break
        if pdf_frame:
            break
        page.wait_for_timeout(500)
    if not pdf_frame:
        raise RuntimeError("Chrome PDF viewer frame not found (no new frame after F8)")

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
        else:
            page.keyboard.press("Escape")
    except Exception:
        pass


def _shell_status_text(shell) -> str:
    """Best-effort SAP status line — avoid full-body inner_text (hangs on heavy WebGUI)."""
    for sel in (
        '[id*="msgty"]',
        '[id*="msgtext"]',
        ".lsMessageBar",
        '[role="status"]',
        "#msgarea",
        '[class*="Message"]',
    ):
        try:
            loc = shell.locator(sel)
            if loc.count() == 0:
                continue
            t = (loc.first.inner_text(timeout=800) or "").strip()
            if t:
                return t[:800]
        except Exception:
            continue
    return ""


def _create_pi_button(shell):
    """Exact codegen/SAP label — do NOT put '/' inside a Playwright name regex
    (it terminates the /.../ selector and throws InvalidSelectorError on '?')."""
    return shell.get_by_role("button", name="Create P/I")


def _print_pi_button(shell):
    return shell.get_by_role("button", name="Print P/I")


def _row_select_cells(shell):
    return shell.get_by_role("gridcell", name=re.compile(r"To select a row", re.I))


def _zsdm_result_ready(shell, so_number: str) -> str | None:
    """True when Execute has produced a selectable result (not just the SO input field)."""
    try:
        btn = _create_pi_button(shell)
        if btn.count() > 0 and btn.first.is_visible():
            return "create_pi_visible"
    except Exception:
        pass
    try:
        sel = _row_select_cells(shell)
        if sel.count() > 0 and sel.first.is_visible():
            return "select_cell_visible"
    except Exception:
        pass
    # SO inside a gridcell — NOT the Sales Document textbox (exact get_by_text matched that)
    try:
        cells = shell.get_by_role("gridcell").filter(has_text=so_number)
        if cells.count() > 0:
            return "so_in_gridcell"
    except Exception:
        pass
    return None


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


def _normalize_sap_num(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _sales_org_from_upload_excel() -> str:
    """7101 or 7104 from the mail Excel — must match what SAP will validate on upload."""
    from rpa.excel_meta import detect_sales_org

    path = os.environ.get("RPA_UPLOAD_FILE") or ""
    org = detect_sales_org(path)
    print(f"[RPA] Filling Sales Org. with {org}")
    return org


def _so_numbers_from_result_excel(path: str) -> list[str]:
    """Read SO numbers from the Create Sales Order result Excel 'Sales Order' column."""
    if not path or not os.path.isfile(path):
        print(f"[RPA] Result Excel missing for SO parse: {path!r}")
        return []
    try:
        import pandas as pd
    except Exception as e:
        print(f"[RPA] pandas unavailable for result Excel SO parse: {e}")
        return []

    try:
        sheets = pd.read_excel(path, sheet_name=None, dtype=str, header=None)
    except Exception as e:
        print(f"[RPA] Could not read result Excel for SO: {e}")
        return []

    # Prefer exact "Sales Order"; allow close variants used by SAP exports.
    header_re = re.compile(
        r"^\s*sales[\s_-]*order\s*$|"
        r"^\s*sales[\s_-]*document\s*$|"
        r"^\s*s[\s./_-]*o[\s./_-]*n(?:o|um(?:ber)?)?\s*$|"
        r"^\s*vbeln\s*$",
        re.I,
    )
    found: list[str] = []
    for sheet_name, df in (sheets or {}).items():
        if df is None or df.empty:
            continue
        so_cols: list[int] = []
        header_row = None
        for r in range(min(25, len(df))):
            row_vals = [str(c).strip() if pd.notna(c) else "" for c in df.iloc[r].tolist()]
            hits = [i for i, v in enumerate(row_vals) if header_re.search(v or "")]
            if hits:
                so_cols = hits
                header_row = r
                break
        if header_row is None or not so_cols:
            continue
        for r in range(header_row + 1, len(df)):
            for c in so_cols:
                if c >= len(df.columns):
                    continue
                raw = df.iloc[r, c]
                if pd.isna(raw):
                    continue
                text = str(raw).strip()
                if not text or text.lower() in ("nan", "none"):
                    continue
                for m in SO_RE.finditer(text):
                    so = m.group(0)
                    if so not in found:
                        found.append(so)
        if found:
            print(
                f"[RPA] Sales Order from result Excel sheet={sheet_name!r} "
                f"cols={so_cols} row={header_row}: {found}"
            )
            return found

    print(f"[RPA] No 'Sales Order' column in result Excel: {os.path.basename(path)}")
    return []


def _sold_tos_from_upload_excel() -> list[str]:
    """Pull Sold-to party numbers from the mail Excel (RPA_UPLOAD_FILE)."""
    path = os.environ.get("RPA_UPLOAD_FILE") or ""
    if not path or not os.path.isfile(path):
        print("[RPA] No RPA_UPLOAD_FILE — cannot pre-load Sold-to values")
        return []
    try:
        import pandas as pd
    except Exception as e:
        print(f"[RPA] pandas unavailable for Sold-to parse: {e}")
        return []

    found: list[str] = []
    try:
        sheets = pd.read_excel(path, sheet_name=None, dtype=str, header=None)
    except Exception as e:
        print(f"[RPA] Could not read upload Excel for Sold-to: {e}")
        return []

    header_re = re.compile(r"sold[\s_-]*to|soldto|kunag", re.I)
    for sheet_name, df in (sheets or {}).items():
        if df is None or df.empty:
            continue
        sold_cols: list[int] = []
        header_row = None
        for r in range(min(20, len(df))):
            row_vals = [str(c).strip() if pd.notna(c) else "" for c in df.iloc[r].tolist()]
            hits = [i for i, v in enumerate(row_vals) if header_re.search(v or "")]
            if hits:
                sold_cols = hits
                header_row = r
                break
        if header_row is None or not sold_cols:
            continue
        for r in range(header_row + 1, len(df)):
            for c in sold_cols:
                if c >= len(df.columns):
                    continue
                raw = df.iloc[r, c]
                if pd.isna(raw):
                    continue
                num = _normalize_sap_num(raw)
                if 5 <= len(num) <= 12 and num not in found:
                    found.append(num)
        if found:
            print(
                f"[RPA] Sold-to from Excel sheet={sheet_name!r} "
                f"cols={sold_cols}: {found}"
            )
            return found

    print("[RPA] No Sold-to column found in upload Excel headers")
    return []


def _text_has_sap_num(text: str, num: str) -> bool:
    if not num:
        return False
    raw = text or ""
    if num in raw:
        return True
    compact = _normalize_sap_num(raw)
    return bool(compact) and num in compact


def _grid_rows_by_index(shell) -> dict[int, str]:
    """Group visible grid#C cells by SAP row index — sold-to lives here, not on the select cell."""
    grouped: dict[int, list[str]] = {}
    try:
        cells = _read_grid_cells(shell)
    except Exception as e:
        print(f"[RPA] Grid cell dump failed: {e}")
        return {}
    for cid, text in cells:
        row, col = _parse_row_col(cid)
        if row is None or not (text or "").strip():
            continue
        grouped.setdefault(row, []).append(text.strip())
    out = {r: " | ".join(vals) for r, vals in grouped.items()}
    if out:
        print(f"[RPA] Grid data rows: {len(out)}")
        for r in sorted(out)[:30]:
            compact = " ".join(out[r].split())
            print(f"[RPA]   grid-row#{r}: {compact[:180]!r}")
    else:
        print("[RPA] Grid data rows: 0")
    return out


def _row_text_for_select_cell(cell) -> str:
    """Full row contents next to a 'To select a row' cell.

    SAP WebGUI select-cell ids often contain 'Row-', so closest('[id*=\"Row-\"]')
    used to return the cell itself (only the space-bar hint) and Sold-to matching
    always failed even when NERP showed the vendor on screen.
    """
    try:
        return (
            cell.evaluate(
                r"""(el) => {
                  const cellText = (el.innerText || el.textContent || '').trim();
                  const a11y = el.closest('[role="row"], tr, .lsTable__row');
                  if (a11y && a11y !== el) {
                    const t = (a11y.innerText || a11y.textContent || '').trim();
                    if (t && t !== cellText) return t;
                  }
                  const parse = (id) => {
                    const m = String(id || '').match(/^grid#C\d+#(\d+),(\d+)(?:@[\w-]+)?$/);
                    return m ? {row: m[1], col: m[2]} : null;
                  };
                  const rowFrom = (id) => {
                    const m = String(id || '').match(/grid#C\d+#(\d+),/);
                    return m ? m[1] : null;
                  };
                  let rowIdx = rowFrom(el.id);
                  let n = el;
                  for (let i = 0; i < 8 && n && rowIdx == null; i++) {
                    rowIdx = rowFrom(n.id);
                    if (rowIdx == null && n.querySelector) {
                      const g = n.querySelector('[id*="grid#"]');
                      if (g) rowIdx = rowFrom(g.id);
                    }
                    n = n.parentElement;
                  }
                  if (rowIdx != null) {
                    const texts = [];
                    for (const node of document.querySelectorAll('[id^="grid#"]')) {
                      const hit = parse(node.id);
                      if (!hit || hit.row !== rowIdx) continue;
                      const t = (node.innerText || node.textContent || '').trim();
                      if (t) texts.push(t);
                    }
                    if (texts.length) return texts.join(' | ');
                  }
                  let p = el.parentElement;
                  for (let i = 0; i < 10 && p; i++) {
                    const t = (p.innerText || p.textContent || '').trim();
                    if (t && t !== cellText && t.length > cellText.length + 5) return t;
                    p = p.parentElement;
                  }
                  return cellText;
                }"""
            )
            or ""
        )
    except Exception:
        return ""


def _select_cell_row_index(cell) -> int | None:
    try:
        raw = cell.evaluate(
            r"""(el) => {
              const rowFrom = (id) => {
                const m = String(id || '').match(/grid#C\d+#(\d+),/);
                return m ? Number(m[1]) : null;
              };
              let n = el;
              for (let i = 0; i < 8 && n; i++) {
                const r = rowFrom(n.id);
                if (r != null) return r;
                if (n.querySelector) {
                  const g = n.querySelector('[id*="grid#"]');
                  if (g) {
                    const r2 = rowFrom(g.id);
                    if (r2 != null) return r2;
                  }
                }
                n = n.parentElement;
              }
              return null;
            }"""
        )
        if raw is None:
            return None
        return int(raw)
    except Exception:
        return None


def _save_failure_screenshot(page) -> None:
    try:
        from rpa.hang_alert import save_page_screenshot
        path = save_page_screenshot(page)
        if path:
            print(f"[RPA] Saved error screenshot: {path}")
    except Exception as e:
        print(f"[RPA] Error screenshot failed: {e}")


def _select_so_result_row(
    shell,
    page,
    so_number: str,
    sold_to: str | None = None,
    sold_tos: list[str] | None = None,
) -> None:
    """Select the correct ZSDM31520 result line for Create P/I.

    Core bug: Document select can return multiple P/I / doc-flow rows. Clicking
    .first prints the wrong Sold-to. Prefer the selectable row whose text
    contains the order's Sold-to party number; fall back to SO match, then
    sole select cell.
    """
    candidates = []
    if sold_to:
        candidates.append(_normalize_sap_num(sold_to))
    for s in sold_tos or []:
        n = _normalize_sap_num(s)
        if n and n not in candidates:
            candidates.append(n)

    exact = shell.get_by_role("gridcell", name="To select a row, press the")
    try:
        n_exact = exact.count()
    except Exception:
        n_exact = 0
    print(
        f"[RPA] Selectable rows ('To select a row, press the'): {n_exact} "
        f"(SO={so_number} sold_to_candidates={candidates or ['-']})"
    )

    def _click(cell, why: str) -> None:
        cell.scroll_into_view_if_needed()
        cell.click()
        page.wait_for_timeout(500)
        print(f"[RPA] Selected result row via {why}")

    if n_exact > 0:
        grid_rows = _grid_rows_by_index(shell)
        rows_info: list[tuple[int, str, int | None]] = []
        for i in range(min(n_exact, 30)):
            txt = _row_text_for_select_cell(exact.nth(i))
            grid_i = _select_cell_row_index(exact.nth(i))
            extra = grid_rows.get(grid_i, "") if grid_i is not None else ""
            if extra and extra not in (txt or ""):
                combined = f"{txt} | {extra}".strip(" |")
            else:
                combined = txt or extra
            rows_info.append((i, combined, grid_i))
            compact = " ".join((combined or "").split())
            print(
                f"[RPA]   row#{i} (grid#{grid_i if grid_i is not None else '-'}): "
                f"{compact[:180]!r}"
            )

        so_norm = _normalize_sap_num(so_number)

        def _click_grid_row(ridx: int, why: str) -> bool:
            for i, _txt, g in rows_info:
                if g == ridx:
                    _click(exact.nth(i), why)
                    return True
            return False

        if candidates:
            matched: list[tuple[int, str]] = []
            for i, txt, _g in rows_info:
                for sold in candidates:
                    if _text_has_sap_num(txt, sold):
                        matched.append((i, sold))
                        break
            if len(matched) == 1:
                i, sold = matched[0]
                _click(exact.nth(i), f"Sold-to match {sold} on row#{i}")
                return
            if len(matched) > 1:
                for i, sold in matched:
                    if so_norm and _text_has_sap_num(rows_info[i][1], so_norm):
                        _click(
                            exact.nth(i),
                            f"Sold-to {sold} + SO on row#{i} "
                            f"(from {len(matched)} Sold-to hits)",
                        )
                        return
                i, sold = matched[0]
                _click(
                    exact.nth(i),
                    f"first Sold-to match {sold} on row#{i} "
                    f"(from {len(matched)} Sold-to hits)",
                )
                return

            grid_hits: list[tuple[int, str]] = []
            for ridx, gtxt in grid_rows.items():
                for sold in candidates:
                    if _text_has_sap_num(gtxt, sold):
                        grid_hits.append((ridx, sold))
                        break
            pick = None
            for ridx, sold in grid_hits:
                if so_norm and _text_has_sap_num(grid_rows[ridx], so_norm):
                    pick = (ridx, sold)
                    break
            if pick is None and grid_hits:
                pick = grid_hits[0]
            if pick:
                ridx, sold = pick
                if _click_grid_row(ridx, f"grid-row#{ridx} Sold-to {sold}"):
                    return
                print(
                    f"[RPA] Sold-to {sold} is on grid-row#{ridx} "
                    "but no matching select cell — trying JS click"
                )
                try:
                    clicked = shell.locator(":root").evaluate(
                        r"""(root, rowIdx) => {
                          const want = String(rowIdx);
                          const cells = Array.from(document.querySelectorAll('[role="gridcell"]'));
                          const sel = cells.find(el => {
                            const name = el.getAttribute('aria-label') || el.innerText || '';
                            if (!/To select a row/i.test(name)) return false;
                            const m = String(el.id || '').match(/grid#C\d+#(\d+),/);
                            return m && m[1] === want;
                          });
                          if (!sel) return false;
                          sel.click();
                          return true;
                        }""",
                        ridx,
                    )
                    if clicked:
                        page.wait_for_timeout(500)
                        print(
                            f"[RPA] Selected result row via JS grid-row#{ridx} Sold-to {sold}"
                        )
                        return
                except Exception as e:
                    print(f"[RPA] JS row click failed: {e}")
            print(f"[RPA] No selectable row contained Sold-to candidates {candidates}")

        if so_norm:
            for i, txt, _g in rows_info:
                if _text_has_sap_num(txt, so_norm):
                    _click(exact.nth(i), f"SO match {so_norm} on row#{i}")
                    return
            for ridx, gtxt in grid_rows.items():
                if _text_has_sap_num(gtxt, so_norm):
                    if _click_grid_row(ridx, f"grid-row#{ridx} SO {so_norm}"):
                        return

        if n_exact == 1:
            _click(exact.first, "sole selectable row")
            return

        # Multiple rows, no Sold-to/SO match — do NOT silently take .first
        raise RuntimeError(
            f"ZSDM31520 has {n_exact} selectable rows for SO {so_number} but none "
            f"matched Sold-to={candidates or '(unknown)'}. Refusing Create P/I to avoid "
            "printing the wrong vendor."
        )

    # Fuzzy fallbacks (theme variants / older WebGUI labels)
    select_name = re.compile(r"To select a row", re.I)
    for sold in candidates:
        rows = shell.get_by_role("row").filter(has_text=re.compile(re.escape(sold)))
        if rows.count() > 0:
            sel = rows.first.get_by_role("gridcell", name=select_name)
            if sel.count() > 0:
                _click(sel.first, f"fuzzy Sold-to row {sold}")
                return

    rows = shell.get_by_role("row").filter(has_text=so_number)
    if rows.count() > 0:
        sel = rows.first.get_by_role("gridcell", name=select_name)
        if sel.count() > 0:
            _click(sel.first, f"fuzzy SO row {so_number}")
            return

    all_sel = _row_select_cells(shell)
    n_sel = all_sel.count()
    print(f"[RPA] Fuzzy row-select cells: {n_sel}")
    if n_sel == 1:
        _click(all_sel.first, "sole fuzzy select cell")
        return
    if n_sel > 1:
        raise RuntimeError(
            f"Found {n_sel} fuzzy select cells for SO {so_number} without Sold-to "
            f"match ({candidates or 'unknown'}). Refusing Create P/I."
        )

    raise RuntimeError(
        f"Could not find 'To select a row, press the' for SO {so_number}. "
        "Refusing Create P/I without a selected line."
    )


def _process_so(
    page,
    so_number: str,
    sold_to: str | None = None,
    sold_tos: list[str] | None = None,
) -> None:
    """ZSDM31520 Document select → fill SO → Create P/I → Print → PDF download."""
    print(
        f"[RPA] Processing SO {so_number} "
        f"(Sold-to={sold_to or 'unknown'}; candidates={sold_tos or []})"
    )
    print("[RPA] Waiting 8s for live SO to be available in ZSDM31520…")
    page.wait_for_timeout(8000)
    _open_zsdm31520(page)
    shell = _shell(page)
    # Mode radio on the selection screen (not the result-grid row radio)
    shell.get_by_role("radio", name="Document select").wait_for(state="visible")
    shell.get_by_role("radio", name="Document select").click()
    page.wait_for_timeout(500)
    _fill_sales_document(shell, page, so_number)
    shell.get_by_role("button", name="Execute  Emphasized").click()
    print(f"[RPA] Waiting for ZSDM31520 result UI after Execute (SO {so_number})")
    # Do NOT use get_by_text(SO) — Sales Document input still contains the SO and
    # fooled the old wait into thinking the result grid was ready (or never ready
    # when test WebGUI only exposes the SO inside gridcells / Create P/I).
    ready_reason = None
    for i in range(90):  # test env can be slow after Execute
        ready_reason = _zsdm_result_ready(shell, so_number)
        if ready_reason:
            break
        if i in (5, 15, 30, 60):
            status = _shell_status_text(shell)
            if status:
                print(f"[RPA] Status bar @ {i}s: {status[:200]!r}")
            if re.search(r"CHECK\s+input\s+s/?o", status, re.I):
                raise RuntimeError(
                    f"SAP rejected SO {so_number} after Execute: CHECK input s/o no."
                )
            if re.search(r"no\s+(relevant\s+)?documents?\s+found", status, re.I):
                raise RuntimeError(
                    f"ZSDM31520 found no documents for SO {so_number} after Execute."
                )
            try:
                n_sel = _row_select_cells(shell).count()
                n_pi = _create_pi_button(shell).count()
            except Exception:
                n_sel, n_pi = -1, -1
            print(
                f"[RPA] Still waiting for result UI… ({i}s) "
                f"select_cells={n_sel} create_pi={n_pi}"
            )
        page.wait_for_timeout(1000)
    if not ready_reason:
        status = _shell_status_text(shell)
        raise RuntimeError(
            f"ZSDM31520 result UI not ready within 90s after Execute for SO {so_number}. "
            f"status={status[:300]!r}"
        )
    page.wait_for_timeout(500)
    print(f"[RPA] Result UI ready ({ready_reason}) for SO {so_number} — selecting line")
    _select_so_result_row(
        shell, page, so_number, sold_to=sold_to, sold_tos=sold_tos
    )

    create_pi = _create_pi_button(shell)
    for i in range(30):
        try:
            if create_pi.count() > 0 and create_pi.first.is_visible():
                break
        except Exception:
            pass
        if i in (5, 15):
            print(f"[RPA] Waiting for Create P/I button… ({i}s)")
        page.wait_for_timeout(1000)
    if create_pi.count() == 0:
        raise RuntimeError(f"Create P/I button not found after selecting SO {so_number}")
    print(f"[RPA] Clicking Create P/I for SO {so_number}")
    create_pi.first.click()
    page.wait_for_timeout(800)
    status = _shell_status_text(shell)
    if re.search(r"no line has been selected", status, re.I):
        raise RuntimeError(
            f"Create P/I rejected: no line selected for SO {so_number}."
        )

    print_pi = _print_pi_button(shell)
    for i in range(20):
        try:
            if print_pi.count() > 0 and print_pi.first.is_visible():
                break
        except Exception:
            pass
        page.wait_for_timeout(500)
    if print_pi.count() == 0:
        raise RuntimeError(f"Print P/I button not found after Create P/I for SO {so_number}")
    print(f"[RPA] Clicking Print P/I for SO {so_number}")
    print_pi.first.click()
    out_dev = shell.get_by_role("textbox", name=re.compile(r"Output Device", re.I))
    out_dev.first.click()
    out_dev.first.fill("zpdf")
    print("[RPA] Output device set to zpdf")
    out_dev.first.press("Enter")
    page.wait_for_timeout(1500)
    _download_pdf(page, so_number)
    print(f"[RPA] Finished SO {so_number}")


def run(playwright: Playwright) -> None:
    global _PDF_HASHES_THIS_RUN
    _PDF_HASHES_THIS_RUN = []
    browser = playwright.chromium.launch(channel="chrome", headless=False)
    context = browser.new_context()
    page = context.new_page()
    try:
        _run_after_login(page)
    except Exception:
        _save_failure_screenshot(page)
        raise
    finally:
        try:
            page.close()
        except Exception:
            pass
        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass


def _run_after_login(page) -> None:
    # NERP only — do NOT goto sts.secsso.net
    page.goto(get_nerp_url())
    page.get_by_role("textbox", name="User Account").click()
    page.get_by_role("textbox", name="User Account").fill("m.tasoglu")
    page.get_by_role("textbox", name="Password").click()
    page.get_by_role("textbox", name="Password").fill("Pass2002?")
    page.get_by_role("button", name="Login").click()
    # After login Search is usually already there — don't force another Utility-home load.
    _open_tcode(page, "ZLSDF50270", force_home=False)
    _sales_org = _sales_org_from_upload_excel()
    page.locator("iframe[name=\"application-Shell-startGUI-iframe\"]").content_frame.get_by_role("textbox", name="Sales Org.").click()
    page.locator("iframe[name=\"application-Shell-startGUI-iframe\"]").content_frame.get_by_role("textbox", name="Sales Org.").fill(_sales_org)
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
    result_excel = ""
    try:
        result_excel = _save_playwright_download(
            excel_dl,
            _excel_dir(),
            excel_dl.suggested_filename or "ZLSDF50270RESULT.XLSX",
        )
    except Exception as e:
        print(f"[RPA] Excel result save failed: {e}")

    # Prefer the result Excel "Sales Order" column; grid scrape is fallback only.
    so_numbers = _so_numbers_from_result_excel(result_excel) if result_excel else []
    if not so_numbers:
        print("[RPA] Falling back to SAP result-grid SO scan")
        so_numbers = _capture_all_so_numbers(page)
    print(f"[RPA] Captured {len(so_numbers)} SO(s): {so_numbers}")
    if not so_numbers:
        raise RuntimeError(
            "No sales order numbers found in the result Excel 'Sales Order' column "
            "or the SAP result grid"
        )

    sold_tos = _sold_tos_from_upload_excel()
    # Process each SO one by one (re-open ZSDM31520 each time for a clean screen)
    for so in so_numbers:
        sold = sold_tos[0] if len(sold_tos) == 1 else None
        _process_so(page, so, sold_to=sold, sold_tos=sold_tos)


with sync_playwright() as playwright:
    run(playwright)
