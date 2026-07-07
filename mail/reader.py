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
    """Dismiss Notice / Knox OK dialogs."""
    for _ in range(4):
        try:
            page.get_by_role("button", name="OK", exact=True).click(timeout=1_000)
            time.sleep(0.3)
        except PlaywrightTimeout:
            break


def _mail_frame(page):
    return page.locator('iframe[title="Mail"]').content_frame


def _open_w1(page):
    """Go to W1 home — never use /portalapp/home (that can force logout)."""
    if "abnormal-logout" in page.url or "loginapp" in page.url:
        print("  Abnormal logout page — clicking OK and returning to W1 home...")
        _click_ok_popups(page)
    page.goto(config.W1_URL)
    page.wait_for_load_state("domcontentloaded")
    _click_ok_popups(page)


def _open_mail_frame(page):
    """Codegen flow: W1 home → Mail button → mail iframe."""
    _open_w1(page)
    page.get_by_role("button", name="Mail", exact=True).click()
    frame = _mail_frame(page)
    frame.locator("body").wait_for(state="attached", timeout=15_000)
    return frame


def _open_mailbox(frame, mailbox):
    frame.get_by_role("button", name=mailbox, exact=True).click()
    time.sleep(1)


def _click_matching_email(frame, subject):
    """Codegen: div.filter(has_text=re.compile(r'^Subject$')).first.click()"""
    frame.locator("div").filter(
        has_text=re.compile(rf"^{re.escape(subject)}$")
    ).first.click(timeout=15_000)
    time.sleep(0.5)
    print(f"  Clicked email: {subject}")
    return subject


def _download_from_open_email(page, frame, download_dir):
    os.makedirs(download_dir, exist_ok=True)
    try:
        with page.expect_download(timeout=8_000) as download_info:
            frame.get_by_role("button", name="Download").first.click()
        download = download_info.value
        save_path = os.path.join(download_dir, download.suggested_filename)
        download.save_as(save_path)
    except PlaywrightTimeout:
        print("  Save As dialog — pressing Enter...")
        dismiss_save_as_dialog(timeout=60)
        save_path = wait_for_new_file(download_dir, timeout=60)

    try:
        frame.get_by_role("button", name="OK").first.click(timeout=3_000)
    except Exception:
        pass

    return save_path


def check_filter(page, frame, mail_filter, processed_subjects):
    """Check one mail filter — download into that job's own Desktop folder."""
    filter_id = mail_filter["id"]
    mailbox = mail_filter["mailbox"]
    subject = mail_filter["subject"]
    download_dir = mail_filter.get("download_dir") or config.DOWNLOAD_DIR

    os.makedirs(download_dir, exist_ok=True)
    _configure_downloads(config.PROFILE_DIR, download_dir)
    _set_cdp_download(page, download_dir)

    downloaded = []
    print(f"[{filter_id}] Mailbox '{mailbox}' → subject '{subject}' → folder {download_dir}")

    _open_mailbox(frame, mailbox)
    opened_subject = _click_matching_email(frame, subject)

    key = f"{filter_id}::{opened_subject}"
    if key in processed_subjects:
        print(f"[{filter_id}] Already handled this session")
        return downloaded

    path = _download_from_open_email(page, frame, download_dir)
    downloaded.append({
        "path": path,
        "filter_id": filter_id,
        "table": mail_filter["table"],
        "subject": opened_subject,
        "ingest_mode": mail_filter.get("ingest_mode", "replace"),
    })
    processed_subjects.add(key)
    print(f"[{filter_id}] Saved to {path}")
    return downloaded


def run_mail_check(filters=None, on_download=None):
    """One full mail scan across the given filters."""
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
        frame = _open_mail_frame(page)

        for mail_filter in filters:
            try:
                items = check_filter(page, frame, mail_filter, processed_subjects)
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
    """Backward-compatible: download from the first mail filter only."""
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
