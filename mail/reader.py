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
    """If W1 shows a login form, fill W1 credentials from Settings (not NERP)."""
    try:
        user_box = page.get_by_role("textbox", name="User Account")
        if user_box.count() == 0:
            return
        from mail.settings_db import get_w1_password, get_w1_username
        username = get_w1_username() or config.NERP_USERNAME
        password = get_w1_password() or config.NERP_PASSWORD
        if not username or not password:
            print("  W1 login form detected but username/password not set in Settings.")
            return
        print(f"  W1 login as {username}…")
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


def _read_email_from_user_info_modal(mail) -> str:
    """Read <dt>E-mail</dt> → <span class="text"> in the emp-tab-panel. Do not click the <a>."""
    panel = mail.locator("div.emp-tab-panel, #profileDetailTab, div.emp-profile").first
    try:
        panel.wait_for(state="visible", timeout=5000)
    except Exception as e:
        print(f"  User info panel not visible: {e}")
        return ""
    row = mail.locator("dl").filter(
        has=mail.locator("dt", has_text=re.compile(r"^E-mail$", re.I))
    )
    text = ""
    try:
        text = (row.locator("dd span.text").first.inner_text(timeout=3000) or "").strip()
    except Exception:
        try:
            text = (row.locator("dd").first.inner_text(timeout=2000) or "").strip()
        except Exception as e:
            print(f"  E-mail row not read: {e}")
            return ""
    m = _EMAIL_RE.search(text)
    return m.group(0).lower() if m else ""


def _close_user_info_modal(mail, page) -> None:
    """Click the modal X (button.pt-btn with i.ic-close). Not the Mail toolbar."""
    close_btn = mail.locator("div.btn-set.al-right button.pt-btn").filter(
        has=mail.locator("i.ic-close")
    )
    try:
        n = min(close_btn.count(), 8)
    except Exception:
        n = 0
    for i in range(n - 1, -1, -1):
        btn = close_btn.nth(i)
        try:
            if not btn.is_visible():
                continue
            btn.click(timeout=2000)
            print("  Closed user info modal (ic-close)")
            time.sleep(0.3)
            return
        except Exception:
            continue
    try:
        page.keyboard.press("Escape")
        print("  Closed user info with Escape")
        time.sleep(0.3)
    except Exception:
        pass


def _capture_open_mail_sender(mail, page) -> str:
    """From chip → View User Info → read E-mail in the modal → close."""
    chip = mail.locator("div.sender-info button.btn-sender-info").first
    try:
        chip.wait_for(state="visible", timeout=5000)
        name = ""
        try:
            name = (chip.locator(".sender-name").inner_text(timeout=1000) or "").strip()
        except Exception:
            pass
        print(f"  Clicking From chip button.btn-sender-info: {name!r}")
        chip.click(timeout=4000)
        time.sleep(0.4)
    except Exception as e:
        print(f"  From chip button.btn-sender-info not clicked: {e}")
        return ""

    try:
        mail.locator("a").filter(has_text="View User Info").click(timeout=5000)
        print("  Clicked View User Info")
        time.sleep(0.5)
    except Exception as e:
        print(f"  View User Info click failed: {e}")
        _close_user_info_modal(mail, page)
        return ""

    sender = _read_email_from_user_info_modal(mail)
    _close_user_info_modal(mail, page)
    if sender:
        print(f"  Sender from View User Info: {sender}")
    else:
        print("  View User Info had no E-mail row")
    return sender


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

        from_email = ""
        try:
            from_email = _capture_open_mail_sender(mail, page)
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
            "ingest_mode": mail_filter.get("ingest_mode", "replace"),
            "extract_zip": mail_filter.get("extract_zip", False),
        }
        downloaded.append(item)
        print(f"[{filter_id}] Saved to {save_path}")
        if from_email:
            try:
                from mail.mail_meta import write_mail_meta
                write_mail_meta(save_path, from_email=from_email, subject=subject)
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
