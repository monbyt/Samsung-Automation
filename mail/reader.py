"""
W1 mail reader — navigate mailboxes, find matching emails, download Excel attachments.
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


def _goto_w1(page):
    if "abnormal-logout" in page.url or "loginapp" in page.url:
        _click_ok_popups(page)
    page.goto(config.W1_URL)
    _click_ok_popups(page)


def _open_mail(page):
    _goto_w1(page)
    page.get_by_role("button", name="Mail", exact=True).click()


def _subject_pattern(subject: str):
    return re.compile(rf"^{re.escape(subject)}$")


def _download_attachment(page, mail, download_dir):
    """Click Download → handle Save As dialog if it appears → locate file."""
    from win_save_as import dismiss_save_as_dialog, snapshot_folder, wait_for_new_file

    # Snapshot BEFORE we trigger the download so we can diff even if
    # Chrome auto-saves the file instantly (no Save As dialog).
    before = snapshot_folder(download_dir)
    started = time.time()

    print("  Clicking Download...")
    mail.get_by_role("button", name="Download").first.click(timeout=10_000)
    time.sleep(1.5)

    print(f"  Handling Save As dialog → {download_dir}")
    dismiss_save_as_dialog(timeout=20, directory=download_dir)

    # Dismiss the mail app's "Download complete" popup immediately — do
    # NOT block on file detection first, otherwise the mailbox stays
    # frozen and the next job can't run.
    try:
        mail.get_by_role("button", name="OK").first.click(timeout=3_000)
    except Exception:
        pass

    save_path = wait_for_new_file(
        download_dir, timeout=20, before=before, started_ts=started,
    )
    return save_path


MAX_UNREAD_PER_TICK = 4


def _open_mailbox(mail, mailbox):
    """Navigate to the given mailbox inside the mail iframe.

    After a mail is opened, W1 adds a tab to the top tab bar (class
    contains "tab-link") whose aria-label is the mailbox name — so
    get_by_role("button", name=mailbox) matches BOTH the sidebar link
    and the tab, triggering a strict-mode violation. We pick the
    sidebar entry by excluding tab-links.
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
        # Fallback — first match (may be the tab, but better than crashing).
        target = candidates.first
    target.click()
    time.sleep(1.5)


def _debug_dump_unread(mail):
    """Log every unread subject link we currently see — helps when a
    selector misses."""
    try:
        unread = mail.locator("a.not-open")
        n = unread.count()
        print(f"  DEBUG: found {n} 'a.not-open' element(s) on page.")
        for idx in range(min(n, 15)):
            try:
                txt = unread.nth(idx).inner_text(timeout=1_000).strip()
                print(f"    [{idx}] {txt!r}")
            except Exception as e:
                print(f"    [{idx}] <read failed: {e}>")
    except Exception as e:
        print(f"  DEBUG: could not enumerate unread: {e}")


def _find_first_unread_row(mail, subject: str):
    """Return the first unread row whose subject matches, or None.

    Strategy — walk every `a.not-open` on the page and compare its
    trimmed text to the target subject. This is more forgiving than a
    `:text-is()` locator (which fails on stray whitespace or trailing
    invisible chars).
    """
    try:
        unread = mail.locator("a.not-open")
        unread.first.wait_for(state="visible", timeout=3_000)
    except PlaywrightTimeout:
        return None
    except Exception:
        return None

    target = subject.strip()
    n = unread.count()
    for idx in range(n):
        try:
            txt = unread.nth(idx).inner_text(timeout=1_000).strip()
        except Exception:
            continue
        if txt == target:
            return unread.nth(idx)

    # No exact hit — dump what we saw so we can tune the match.
    print(f"  No unread row exactly matching {target!r}. Enumerating:")
    _debug_dump_unread(mail)
    return None


def check_filter(page, mail_filter, processed_subjects, on_download=None):
    """Process up to MAX_UNREAD_PER_TICK unread mails matching subject.

    For each unread row (oldest first), download → invoke on_download →
    re-scan the mailbox for the next unread. Read (already-processed)
    rows are skipped by the DOM query itself: `a.not-open` only matches
    unread mails.
    """
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

    for i in range(MAX_UNREAD_PER_TICK):
        row = _find_first_unread_row(mail, subject)
        if row is None:
            if i == 0:
                print(f"[{filter_id}] No unread mails matching subject.")
            else:
                print(f"[{filter_id}] No more unread mails.")
            break

        print(f"[{filter_id}] Processing unread mail {i + 1}/{MAX_UNREAD_PER_TICK}")
        row.click()
        time.sleep(1.0)

        save_path = _download_attachment(page, mail, download_dir)

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

        # Return to the mailbox so the next iteration sees the updated list.
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
        context = pw.chromium.launch_persistent_context(
            config.PROFILE_DIR,
            channel="chrome",
            headless=config.HEADLESS,
            accept_downloads=True,
            args=["--disable-popup-blocking", "--no-first-run"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        _open_mail(page)

        for i, mail_filter in enumerate(filters):
            try:
                if i > 0:
                    # Fresh mail view so a previous job's open email doesn't block the next.
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
