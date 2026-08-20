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
    return re.compile(rf"^{re.escape(subject)}$")


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
    """Click the first unread row with this subject. Read rows are skipped."""
    rows = mail.locator("div").filter(has_text=_subject_pattern(subject))
    n = rows.count()
    print(f"  Subject rows: {n}")
    if n == 0:
        rows = mail.get_by_text(subject, exact=True)
        n = rows.count()
        print(f"  get_by_text rows: {n}")

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
        print(f"  Clicked unread email: {subject}")
        return True

    print("  No unread email with that subject.")
    return False


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
    print(f"[{filter_id}] Mailbox '{mailbox}' → subject '{subject}'")
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

        if on_download:
            try:
                on_download(item)
            except Exception as e:
                print(f"[{filter_id}] on_download failed for {save_path}: {e}")

        _open_mailbox(mail, mailbox)

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
