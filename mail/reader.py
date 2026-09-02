"""
W1 mail reader — navigate mailboxes, click unread emails, download attachments.

Find subject rows with the original Playwright locator, then click only
rows that are unread (bold / unread class). Read mail is skipped.
"""
import json
import os
import re
import time
from datetime import datetime

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

import config

MAIL_IFRAME = 'iframe[title="Mail"]'
MAX_MAILS_PER_TICK = 20
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_CHROME_BTN = re.compile(
    r"^(OC|Mail|New Mail|New|Compose|Write|Write Mail|Filter|Unread|Download|Close|"
    r"Reply|Reply All|Forward|Delete|Print|Move|Search|Inbox|OK|Cancel|Yes|No|"
    r"Send|Attach|Extract|Product Extract)$",
    re.I,
)
_VIEW_USER_INFO = re.compile(r"View User Info", re.I)
_SKIP_BTN_SUBSTR = (
    "new mail", "compose", "write mail", "download", "filter", "unread",
    "mailbox",
)


def _configure_downloads(profile_dir, download_dir):
    default_dir = os.path.join(profile_dir, "Default")
    os.makedirs(default_dir, exist_ok=True)
    prefs_path = os.path.join(default_dir, "Preferences")

    prefs = {}
    if os.path.exists(prefs_path):
        try:
            with open(prefs_path, "r", encoding="utf-8") as f:
                prefs = json.load(f)
        except Exception:
            prefs = {}

    prefs.setdefault("download", {})
    prefs["download"]["prompt_for_download"] = False
    prefs["download"]["default_directory"] = download_dir
    prefs["download"]["directory_upgrade"] = True

    with open(prefs_path, "w", encoding="utf-8") as f:
        json.dump(prefs, f)


def _set_cdp_download(page, download_dir):
    try:
        cdp = page.context.new_cdp_session(page)
        cdp.send("Browser.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": download_dir,
            "eventsEnabled": True,
        })
    except Exception:
        pass


def _click_ok_popups(page):
    for _ in range(4):
        try:
            page.get_by_role("button", name="OK", exact=True).first.click(timeout=1_000)
        except PlaywrightTimeout:
            break


def _mail(page):
    return page.locator(MAIL_IFRAME).content_frame


def _maybe_sso_login(page):
    """If W1 shows a Samsung SSO login form, fill credentials from Settings."""
    try:
        user_box = page.get_by_role("textbox", name="User Account")
        if user_box.count() == 0:
            return
        from mail.settings_db import get_sso_password, get_sso_username
        username = get_sso_username() or config.NERP_USERNAME
        password = get_sso_password() or config.NERP_PASSWORD
        if not username or not password:
            print("  SSO login form detected but username/password not set in Settings.")
            return
        print(f"  SSO login as {username}…")
        user_box.first.click(timeout=3_000)
        user_box.first.fill(username)
        pw = page.get_by_role("textbox", name="Password")
        pw.first.click(timeout=3_000)
        pw.first.fill(password)
        pw.first.press("Enter")
        page.wait_for_timeout(2_000)
        _click_ok_popups(page)
    except Exception as e:
        print(f"  SSO auto-login skipped: {e}")


def _goto_w1(page):
    if "abnormal-logout" in page.url or "loginapp" in page.url:
        _click_ok_popups(page)
    page.goto(config.W1_URL)
    _click_ok_popups(page)
    _maybe_sso_login(page)


def _open_mail(page):
    _goto_w1(page)
    page.get_by_role("button", name="Mail", exact=True).click()


def _subject_pattern(subject: str):
    """Case-insensitive substring match against the W1 subject line.

    The mail-job field is the text to find inside the title, not an exact
    title and not a regex. 'Order Creation' matches 'FW: Order Creation — AE'.
    """
    text = (subject or "").strip()
    if not text:
        return re.compile(r"(?!)")
    return re.compile(re.escape(text), re.IGNORECASE)


def _download_attachment(page, mail, download_dir):
    """Click Download → handle Save As dialog if it appears → locate file."""
    from win_save_as import dismiss_save_as_dialog, snapshot_folder, wait_for_new_file

    before = snapshot_folder(download_dir)
    started = time.time()

    print("  Clicking Download...")
    mail.get_by_role("button", name="Download").first.click(timeout=10_000)
    time.sleep(1.5)

    print(f"  Handling Save As dialog → {download_dir}")
    dismiss_save_as_dialog(timeout=20, directory=download_dir)

    try:
        mail.get_by_role("button", name="OK").first.click(timeout=3_000)
    except Exception:
        pass

    save_path = wait_for_new_file(
        download_dir, timeout=20, before=before, started_ts=started,
    )
    return save_path


def _open_mailbox(mail, mailbox):
    """Navigate to the given mailbox inside the mail iframe.

    After a mail is opened, W1 adds a tab whose aria-label is the mailbox name,
    so get_by_role("button", name=mailbox) can match both the sidebar and the tab.
    Prefer the sidebar entry (exclude tab-links).
    """
    candidates = mail.get_by_role("button", name=mailbox, exact=True)
    count = candidates.count()
    target = None
    for i in range(count):
        btn = candidates.nth(i)
        cls = (btn.get_attribute("class") or "").lower()
        if "tab-link" in cls:
            continue
        target = btn
        break
    if target is None:
        target = candidates.first
    target.click()
    time.sleep(1.5)


def _apply_unread_filter(mail) -> None:
    """Open the mailbox Filter popover and select Unread (W1 codegen)."""
    try:
        mail.get_by_role("button", name="Filter").click(timeout=8_000)
        time.sleep(0.4)
        mail.locator("#FilterPopover").get_by_role(
            "button", name="Unread"
        ).click(timeout=8_000)
        time.sleep(1.0)
        print("  Filter → Unread applied")
    except Exception as e:
        print(f"  Filter → Unread skipped ({e})")


_IS_UNREAD_JS = """
(el) => {
  const nodes = [el, ...el.querySelectorAll('a, span, div, b, strong, td, li')];
  let fw = '';
  let cls = (el.className || '').toString().slice(0, 80);
  for (const n of nodes) {
    const c = (n.className || '').toString().toLowerCase();
    if (/\\bunread\\b|\\bnot-read\\b|\\bis-unread\\b|\\bmail-unread\\b/.test(c)) {
      return { unread: true, fw: getComputedStyle(n).fontWeight, cls };
    }
    const w = getComputedStyle(n).fontWeight;
    if (!fw) fw = w;
    if (w === 'bold' || w === 'bolder' || parseInt(w, 10) >= 600) {
      return { unread: true, fw: w, cls };
    }
  }
  return { unread: false, fw, cls };
}
"""


def _click_unread_email(mail, subject: str) -> bool:
    """Click the first unread row whose subject contains *subject*."""
    pattern = _subject_pattern(subject)
    rows = mail.get_by_text(pattern)
    n = rows.count()
    print(f"  Subject rows containing {subject!r}: {n}")
    if n == 0:
        rows = mail.locator("div").filter(has_text=pattern)
        n = rows.count()
        print(f"  div rows containing {subject!r}: {n}")

    limit = min(n, MAX_MAILS_PER_TICK)
    for i in range(limit):
        row = rows.nth(i)
        try:
            info = row.evaluate(_IS_UNREAD_JS)
        except Exception as e:
            print(f"    [{i}] unread check failed: {e}")
            continue
        if not isinstance(info, dict):
            info = {"unread": bool(info)}
        flag = "UNREAD" if info.get("unread") else "read"
        print(
            f"    [{i}] [{flag}] fw={info.get('fw')!r} cls={info.get('cls')!r}"
        )
        if not info.get("unread"):
            continue
        row.scroll_into_view_if_needed()
        row.click(timeout=10_000)
        print(f"  Clicked unread email containing {subject!r}")
        return True

    print("  No unread email whose subject contains that text.")
    return False


def _emails_from_dom(mail) -> list[str]:
    """Collect addresses already visible in the Mail iframe (no extra clicks)."""
    try:
        found = mail.locator(":root").evaluate(
            """() => {
              const re = /[A-Za-z0-9._%+\\-]+@[A-Za-z0-9.\\-]+\\.[A-Za-z]{2,}/g;
              const seen = new Set();
              const out = [];
              const add = (s) => {
                if (!s) return;
                const t = String(s);
                const m = t.match(/mailto:([^?\\s]+)/i);
                if (m) {
                  try {
                    const e = decodeURIComponent(m[1]).trim().toLowerCase();
                    if (e && !seen.has(e)) { seen.add(e); out.push(e); }
                  } catch (err) {}
                }
                const hits = t.match(re) || [];
                for (const e of hits) {
                  const k = e.toLowerCase();
                  if (!seen.has(k)) { seen.add(k); out.push(k); }
                }
              };
              const nodes = document.querySelectorAll(
                'a[href], button, [title], [aria-label], .user-info, .mail-read, .read-area'
              );
              for (const el of nodes) {
                add(el.getAttribute('href'));
                add(el.getAttribute('title'));
                add(el.getAttribute('aria-label'));
                add(el.innerText);
              }
              return out;
            }"""
        )
    except Exception:
        found = []
    out = []
    for raw in found or []:
        m = _EMAIL_RE.search(str(raw) or "")
        if m:
            e = m.group(0).lower()
            if e not in out:
                out.append(e)
    return out


def _emails_from_user_info_links(mail) -> list[str]:
    """Addresses shown as links on the View User Info card — do not click them."""
    out = []
    try:
        links = mail.locator("a").filter(has_text=_EMAIL_RE)
        n = min(links.count(), 10)
    except Exception:
        n = 0
    for i in range(n):
        try:
            text = (links.nth(i).inner_text(timeout=800) or "").strip()
        except Exception:
            continue
        m = _EMAIL_RE.search(text)
        if m:
            e = m.group(0).lower()
            if e not in out:
                out.append(e)
    return out


def _is_chrome_or_compose(label: str) -> bool:
    t = " ".join((label or "").split())
    if not t:
        return True
    if _CHROME_BTN.match(t):
        return True
    low = t.lower()
    return any(s in low for s in _SKIP_BTN_SUBSTR)


def _close_compose_if_open(mail, page) -> None:
    """New Mail / compose covering the message — close that dialog only."""
    try:
        dlg = mail.get_by_role("dialog")
        if dlg.count() == 0:
            return
        for name in ("New Mail", "Compose", "Write Mail"):
            try:
                if dlg.get_by_text(name, exact=False).count() == 0:
                    continue
                dlg.get_by_role("button", name="Close").first.click(timeout=1200)
                print("  Closed compose/New Mail dialog")
                time.sleep(0.3)
                return
            except Exception:
                continue
    except Exception:
        pass


def _dismiss_user_info(mail, page) -> None:
    """Close the View User Info card only. Never click the mailbox toolbar (New Mail)."""
    try:
        page.keyboard.press("Escape")
        time.sleep(0.2)
    except Exception:
        pass
    try:
        card = mail.locator("a").filter(has_text=_VIEW_USER_INFO)
        if card.count() == 0:
            return
        mail.get_by_role("dialog").get_by_role("button", name="Close").first.click(timeout=800)
        time.sleep(0.2)
    except Exception:
        pass


def _click_from_chip_in_open_mail(mail) -> bool:
    """Click the sender name on the open message (codegen: From person button, twice)."""
    # Prefer the button sitting next to a From / Sender label — not the folder list.
    for label in ("From", "Sender"):
        try:
            btn = mail.get_by_text(label, exact=True).locator("xpath=following::button[1]")
            if btn.count() == 0 or not btn.first.is_visible():
                continue
            name = (btn.first.get_attribute("aria-label") or btn.first.inner_text(timeout=800) or "")
            name = " ".join(name.split())
            if _is_chrome_or_compose(name):
                print(f"  Skipping chrome button next to {label}: {name!r}")
                continue
            print(f"  Clicking open-mail From chip: {name!r}")
            btn.first.click(timeout=3000)
            time.sleep(0.3)
            btn.first.click(timeout=3000)
            time.sleep(0.4)
            return True
        except Exception as e:
            print(f"  From label {label!r} click skipped: {e}")

    # Fallback: JS From/Sender label → nearby person button (never New Mail).
    try:
        name = mail.locator(":root").evaluate(
            """() => {
              const skip = /new mail|compose|download|filter|unread|^mail$|^oc$|^close$/i;
              const nodes = Array.from(document.querySelectorAll('span, div, label, th, dt, td'));
              for (const el of nodes) {
                const t = (el.innerText || '').trim();
                if (!/^(From|Sender)$/i.test(t)) continue;
                const root = el.closest('tr, li, div, dl') || el.parentElement;
                if (!root) continue;
                const btn = root.querySelector('button, [role="button"]');
                if (!btn) continue;
                const n = (btn.getAttribute('aria-label') || btn.innerText || '').trim();
                if (!n || skip.test(n)) continue;
                btn.click();
                btn.click();
                return n;
              }
              return '';
            }"""
        )
        if name:
            print(f"  Clicked From chip via header scan: {name!r}")
            time.sleep(0.4)
            return True
    except Exception as e:
        print(f"  From header scan skipped: {e}")
    return False


def _open_from_user_info(mail) -> bool:
    """From chip on the open mail → View User Info. Never click New Mail / mailbox."""
    view = mail.locator("a").filter(has_text=_VIEW_USER_INFO)
    try:
        if view.count() > 0 and view.first.is_visible():
            view.first.click(timeout=3000)
            time.sleep(0.5)
            return True
    except Exception:
        pass

    if not _click_from_chip_in_open_mail(mail):
        print("  Could not click From chip on the open message")
        return False

    view = mail.locator("a").filter(has_text=_VIEW_USER_INFO)
    try:
        view.first.wait_for(state="visible", timeout=5000)
        view.first.click(timeout=3000)
        time.sleep(0.5)
        print("  Clicked View User Info")
        return True
    except Exception as e:
        print(f"  View User Info not visible after From click: {e}")
        return False


def _capture_open_mail_sender(mail, page) -> tuple[str, list[str]]:
    """Read From from the already-open message via View User Info.

    Mailbox / unread / subject / download logic is unchanged. Failure here
    must not abort the download. Must never click New Mail / folder buttons.
    """
    _close_compose_if_open(mail, page)

    emails = _emails_from_user_info_links(mail) or _emails_from_dom(mail)
    if len(emails) == 1:
        print(f"  Sender from open mail: {emails[0]}")
        return emails[0], []

    print("  Opening From → View User Info on the open message (not the mailbox list)")
    emails = []
    try:
        if _open_from_user_info(mail):
            emails = _emails_from_user_info_links(mail) or _emails_from_dom(mail)
        else:
            print("  View User Info not found")
    except Exception as e:
        print(f"  Sender capture skipped: {e}")
    finally:
        _dismiss_user_info(mail, page)

    if emails:
        sender, cc = emails[0], emails[1:]
        print(f"  Sender from View User Info: {sender}" + (f" cc={cc}" if cc else ""))
        return sender, cc
    print("  Could not read sender email — Email Job To will be used")
    return "", []


def check_filter(page, mail_filter, processed_subjects, on_download=None):
    """Open the mailbox and download unread matching emails only."""
    filter_id = mail_filter["id"]
    mailbox = mail_filter["mailbox"]
    subject = mail_filter["subject"]
    download_dir = mail_filter.get("download_dir") or config.DOWNLOAD_DIR

    os.makedirs(download_dir, exist_ok=True)
    _configure_downloads(config.PROFILE_DIR, download_dir)
    _set_cdp_download(page, download_dir)

    downloaded = []
    print(f"[{filter_id}] Mailbox '{mailbox}' → subject contains {subject!r}")
    print(f"[{filter_id}] Saving to: {download_dir}")

    mail = _mail(page)
    _open_mailbox(mail, mailbox)
    _apply_unread_filter(mail)

    for i in range(MAX_MAILS_PER_TICK):
        if not _click_unread_email(mail, subject):
            if i == 0:
                print(f"[{filter_id}] No unread mail — skipping download.")
            else:
                print(f"[{filter_id}] No more unread mail.")
            break

        print(f"[{filter_id}] Processing unread mail {i + 1}/{MAX_MAILS_PER_TICK}")
        time.sleep(1.0)

        from_email, cc_emails = "", []
        try:
            from_email, cc_emails = _capture_open_mail_sender(mail, page)
        except Exception as e:
            print(f"[{filter_id}] Sender capture failed (download continues): {e}")

        save_path = _download_attachment(page, mail, download_dir)

        try:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            base, ext = os.path.splitext(os.path.basename(save_path))
            unique_name = f"{base}_{stamp}_{i}{ext}"
            unique_path = os.path.join(os.path.dirname(save_path), unique_name)
            os.rename(save_path, unique_path)
            save_path = unique_path
            print(f"[{filter_id}] Renamed to {unique_name}")
        except OSError as e:
            print(f"[{filter_id}] Could not rename download uniquely: {e}")

        item = {
            "path": save_path,
            "filter_id": filter_id,
            "table": mail_filter["table"],
            "subject": subject,
            "from_email": from_email,
            "cc_emails": cc_emails,
            "ingest_mode": mail_filter.get("ingest_mode", "replace"),
            "extract_zip": mail_filter.get("extract_zip", False),
        }
        downloaded.append(item)
        print(f"[{filter_id}] Saved to {save_path}")
        if from_email:
            try:
                from mail.mail_meta import write_mail_meta
                write_mail_meta(
                    save_path,
                    from_email=from_email,
                    cc_emails=cc_emails,
                    subject=subject,
                )
                print(f"[{filter_id}] Sender saved: {from_email}")
            except Exception as e:
                print(f"[{filter_id}] Could not write sender sidecar: {e}")

        if on_download:
            try:
                on_download(item)
            except Exception as e:
                print(f"[{filter_id}] on_download failed for {save_path}: {e}")

        # Back to the mailbox list — re-apply Filter → Unread each time,
        # because opening a mail clears the filter in W1.
        _open_mailbox(mail, mailbox)
        _apply_unread_filter(mail)

    return downloaded


def run_mail_check(filters=None, on_download=None):
    if filters is None:
        from mail.jobs_db import list_jobs, job_as_filter
        filters = [job_as_filter(j) for j in list_jobs() if j["enabled"]]
    if not filters:
        return {"checked_at": datetime.now(), "downloads": [], "errors": ["No enabled mail jobs."]}

    os.makedirs(config.PROFILE_DIR, exist_ok=True)

    summary = {
        "checked_at": datetime.now(),
        "downloads": [],
        "errors": [],
    }
    processed_subjects = set()

    with sync_playwright() as pw:
        mail_port = int(getattr(config, "MAIL_CDP_PORT", 9222) or 9222)
        context = pw.chromium.launch_persistent_context(
            config.PROFILE_DIR,
            channel="chrome",
            headless=config.HEADLESS,
            accept_downloads=True,
            args=[
                "--disable-popup-blocking",
                "--no-first-run",
                f"--remote-debugging-port={mail_port}",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()
        _open_mail(page)

        for i, mail_filter in enumerate(filters):
            try:
                if i > 0:
                    _open_mail(page)
                items = check_filter(
                    page, mail_filter, processed_subjects,
                    on_download=on_download,
                )
                summary["downloads"].extend(items)
            except Exception as e:
                msg = f"{mail_filter['id']}: {e}"
                print(f"ERROR {msg}")
                summary["errors"].append(msg)

        context.close()

    return summary


def download_latest():
    result = None
    from mail.jobs_db import list_jobs, job_as_filter
    jobs = list_jobs()
    if not jobs:
        from mail.jobs_db import seed_from_config
        seed_from_config()
        jobs = list_jobs()
    filters = [job_as_filter(jobs[0])] if jobs else []

    def _capture(item):
        nonlocal result
        result = item["path"]

    summary = run_mail_check(filters=filters, on_download=_capture)
    if result:
        return result
    if summary["downloads"]:
        return summary["downloads"][-1]["path"]
    raise RuntimeError("No matching email found to download.")
