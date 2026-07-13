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
from win_save_as import dismiss_save_as_dialog, wait_for_new_file

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


def _download_attachment(mail, download_dir):
    """Download once → Save As in mail UI → confirm Windows Save As dialog."""
    print("  Clicking Download...")
    mail.get_by_role("button", name="Download").first.click(timeout=10_000)

    print("  Clicking Save As...")
    mail.get_by_role("button", name="Save As", exact=True).first.click(timeout=10_000)
    time.sleep(1.5)

    print(f"  Confirming Windows Save As → {download_dir}")
    dismiss_save_as_dialog(timeout=60, directory=download_dir)
    save_path = wait_for_new_file(download_dir, timeout=60)

    try:
        mail.get_by_role("button", name="OK").first.click(timeout=3_000)
    except PlaywrightTimeout:
        pass

    return save_path


def check_filter(page, mail_filter, processed_subjects):
    """Open mailbox, click matching email, download attachment."""
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

    key = f"{filter_id}::{subject}"
    if key in processed_subjects:
        print(f"[{filter_id}] Already handled this session")
        return downloaded

    mail = _mail(page)
    mail.get_by_role("button", name=mailbox, exact=True).click()
    mail.locator("div").filter(has_text=_subject_pattern(subject)).first.click()

    save_path = _download_attachment(mail, download_dir)

    processed_subjects.add(key)
    downloaded.append({
        "path": save_path,
        "filter_id": filter_id,
        "table": mail_filter["table"],
        "subject": subject,
        "ingest_mode": mail_filter.get("ingest_mode", "replace"),
        "extract_zip": mail_filter.get("extract_zip", False),
    })
    print(f"[{filter_id}] Saved to {save_path}")
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
                items = check_filter(page, mail_filter, processed_subjects)
                for item in items:
                    summary["downloads"].append(item)
                    if on_download:
                        on_download(item)
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
