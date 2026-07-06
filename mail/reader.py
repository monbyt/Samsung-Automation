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


def _open_mail_frame(page):
    page.goto(config.W1_URL)
    page.wait_for_load_state("domcontentloaded")
    page.get_by_role("button", name="Mail").click()
    page.get_by_role("button", name="Mail", exact=True).click()
    return page.locator('iframe[title="Mail"]').content_frame


def _download_from_open_email(page, frame, download_dir):
    os.makedirs(download_dir, exist_ok=True)
    try:
        with page.expect_download(timeout=8_000) as download_info:
            frame.get_by_role("button", name="Download").click()
        download = download_info.value
        save_path = os.path.join(download_dir, download.suggested_filename)
        download.save_as(save_path)
    except PlaywrightTimeout:
        print("  Save As dialog — pressing Enter...")
        dismiss_save_as_dialog(timeout=60)
        save_path = wait_for_new_file(download_dir, timeout=60)

    try:
        frame.get_by_role("button", name="OK").click(timeout=3_000)
    except Exception:
        pass

    return save_path


def _has_regex_metachars(text: str) -> bool:
    """True if subject_pattern uses regex syntax beyond a plain string."""
    return text != re.escape(text)


def _open_mailbox_and_click_email(frame, mailbox, subject_pattern):
    """
    Open mailbox folder, then click the newest matching email.

    Uses the same locator strategy as the original download.py for plain
    subject strings (exact title match on the email row div).
    """
    frame.get_by_role("button", name=mailbox, exact=True).click()
    time.sleep(1.5)

    pattern = re.compile(subject_pattern, re.IGNORECASE)

    # Plain subject (e.g. "Order Extract - AE/GCC") — exact match, same as download.py
    if not _has_regex_metachars(subject_pattern):
        email = frame.locator("div").filter(
            has_text=re.compile(rf"^{re.escape(subject_pattern)}$", re.IGNORECASE)
        )
        email.first.wait_for(state="visible", timeout=15_000)
        email.first.scroll_into_view_if_needed()
        email.first.click(timeout=10_000)
        time.sleep(0.5)
        return subject_pattern

    # Regex pattern — click the first row whose title line matches
    rows = frame.locator("div").filter(has_text=pattern)
    for i in range(min(rows.count(), 30)):
        row = rows.nth(i)
        try:
            line = row.inner_text(timeout=2_000).strip().split("\n")[0].strip()
            if not pattern.search(line):
                continue
            row.scroll_into_view_if_needed()
            row.click(timeout=10_000)
            time.sleep(0.5)
            return line
        except Exception:
            continue

    # Last resort — first matching div in the list (same list position as before)
    rows.first.wait_for(state="visible", timeout=15_000)
    rows.first.scroll_into_view_if_needed()
    rows.first.click(timeout=10_000)
    time.sleep(0.5)
    return subject_pattern


def _list_matching_subjects(frame, mailbox, subject_pattern):
    """Return subject lines for matching emails (newest first)."""
    frame.get_by_role("button", name=mailbox, exact=True).click()
    time.sleep(1.5)

    pattern = re.compile(subject_pattern, re.IGNORECASE)
    rows = frame.locator("div").filter(has_text=pattern)
    subjects = []
    for i in range(min(rows.count(), 30)):
        try:
            text = rows.nth(i).inner_text(timeout=2_000).strip().split("\n")[0].strip()
            if pattern.search(text):
                subjects.append(text)
        except Exception:
            continue
    seen = set()
    unique = []
    for s in subjects:
        if s not in seen:
            seen.add(s)
            unique.append(s)
    return unique


def check_filter(page, frame, mail_filter, download_dir, processed_subjects):
    """
    Check one mail filter for new emails. Returns list of downloaded file paths.
    """
    filter_id = mail_filter["id"]
    mailbox = mail_filter["mailbox"]
    subject_pattern = mail_filter["subject"]
    downloaded = []

    print(f"[{filter_id}] Scanning mailbox '{mailbox}' for /{subject_pattern}/")

    # Peek at matches for logging, then click using the same locator logic
    subjects = _list_matching_subjects(frame, mailbox, subject_pattern)
    print(f"[{filter_id}] Found {len(subjects)} matching email(s)")
    if not subjects:
        return downloaded

    print(f"[{filter_id}] Opening newest: {subjects[0]}")
    opened_subject = _open_mailbox_and_click_email(frame, mailbox, subject_pattern)

    key = f"{filter_id}::{opened_subject}"
    if key in processed_subjects:
        print(f"[{filter_id}] Already handled this session: {opened_subject}")
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

    return downloaded


def run_mail_check(filters=None, on_download=None):
    """
    One full mail scan across the given filters (or all enabled jobs from DB).
    *on_download* callback: fn(item_dict) called after each successful download.
    Returns summary dict.
    """
    if filters is None:
        from mail.jobs_db import list_jobs, job_as_filter
        filters = [job_as_filter(j) for j in list_jobs() if j["enabled"]]
    if not filters:
        return {"checked_at": datetime.now(), "downloads": [], "errors": ["No enabled mail jobs."]}
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(config.PROFILE_DIR, exist_ok=True)
    _configure_downloads(config.PROFILE_DIR, config.DOWNLOAD_DIR)

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

        try:
            cdp = context.new_cdp_session(page)
            cdp.send("Browser.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": config.DOWNLOAD_DIR,
                "eventsEnabled": True,
            })
        except Exception:
            pass

        frame = _open_mail_frame(page)

        for mail_filter in filters:
            try:
                items = check_filter(
                    page, frame, mail_filter,
                    config.DOWNLOAD_DIR, processed_subjects,
                )
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
