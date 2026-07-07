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


def _click_first(locator, timeout=2_000):
    try:
        locator.first.click(timeout=timeout)
        return True
    except Exception:
        return False


def _page_text(page) -> str:
    try:
        return page.inner_text("body", timeout=5_000).lower()
    except Exception:
        return ""


def _has_expired_session(page) -> bool:
    text = _page_text(page)
    return "expired session" in text or "session expired" in text or "session has expired" in text


def _dismiss_ok_popups(page, rounds=6):
    """Click OK / Allow on Knox-style modals — skip when session-expired (needs re-login)."""
    for _ in range(rounds):
        if _has_expired_session(page):
            return
        clicked = False
        for label in ("OK", "Ok", "Allow", "Accept", "Confirm", "Close", "Yes"):
            if _click_first(page.get_by_role("button", name=label, exact=True)):
                clicked = True
                time.sleep(0.4)
        try:
            page.get_by_role("dialog").get_by_role("button", name="OK").click(timeout=800)
            clicked = True
            time.sleep(0.4)
        except Exception:
            pass
        try:
            page.locator('[role="alertdialog"]').get_by_role("button", name="OK").click(timeout=800)
            clicked = True
            time.sleep(0.4)
        except Exception:
            pass
        if not clicked:
            break


def _acknowledge_expired_session(page):
    """Dismiss the expired-session dialog so the login form appears."""
    if not _has_expired_session(page):
        return False
    print("  W1 session expired — acknowledging dialog...")
    for locator in (
        page.get_by_role("dialog").get_by_role("button", name="OK"),
        page.locator('[role="alertdialog"]').get_by_role("button", name="OK"),
        page.get_by_role("button", name="OK", exact=True),
    ):
        if _click_first(locator):
            time.sleep(1)
            return True
    return False


def _mail_button_visible(page) -> bool:
    try:
        page.get_by_role("button", name="Mail", exact=True).wait_for(
            state="visible", timeout=4_000
        )
        return True
    except PlaywrightTimeout:
        return False


def _needs_w1_login(page) -> bool:
    if _mail_button_visible(page):
        return False
    text = _page_text(page)
    url = page.url.lower()
    if any(k in url for k in ("login", "adfs", "sso", "sts.", "signin")):
        return True
    if any(k in text for k in ("sign in", "log in", "user account", "password")):
        return True
    try:
        page.locator('input[type="password"]').first.wait_for(state="visible", timeout=2_000)
        return True
    except PlaywrightTimeout:
        pass
    return not _mail_button_visible(page)


def _sso_form_visible(page) -> bool:
    try:
        page.get_by_role("textbox", name="User Account").wait_for(
            state="visible", timeout=3_000
        )
        return True
    except PlaywrightTimeout:
        return False


def _w1_login(page):
    user = config.W1_USERNAME
    pwd = config.W1_PASSWORD
    if user and pwd:
        print(f"  W1 auto-login enabled for user '{user}'")
    else:
        print(
            f"  W1 auto-login OFF (no password set) — sign in manually in Chrome "
            f"(waiting up to {config.W1_LOGIN_WAIT_SECONDS}s).\n"
            f"  Add NERP_PASSWORD=... to a .env file in the project folder."
        )

    # Portal may show Sign in before redirecting to Samsung SSO.
    for label in ("Sign in", "Sign In", "Log in", "Login"):
        _click_first(page.get_by_role("button", name=label))
        _click_first(page.get_by_role("link", name=label))
        time.sleep(0.5)

    if user and pwd:
        # Wait for SSO form (same page NERP uses: sts.secsso.net).
        deadline = time.time() + 20
        while time.time() < deadline and not _sso_form_visible(page):
            time.sleep(0.5)

        if _sso_form_visible(page):
            print("  Filling Samsung SSO form...")
            page.get_by_role("textbox", name="User Account").fill(user)
            page.get_by_role("textbox", name="Password").fill(pwd)
            page.get_by_role("textbox", name="Password").press("Enter")
            page.wait_for_load_state("domcontentloaded")
            time.sleep(2)
        else:
            print(f"  SSO form not found (url: {page.url}) — trying generic fields...")
            for name in ("User Account", "User ID", "Username", "Email"):
                try:
                    page.get_by_role("textbox", name=name).fill(user, timeout=2_000)
                    break
                except PlaywrightTimeout:
                    continue
            try:
                page.get_by_role("textbox", name="Password").fill(pwd, timeout=2_000)
            except PlaywrightTimeout:
                page.locator('input[type="password"]').first.fill(pwd, timeout=2_000)
            for label in ("Sign in", "Sign In", "Log in", "Login", "Submit"):
                if _click_first(page.get_by_role("button", name=label), timeout=2_000):
                    break
            else:
                page.locator('input[type="password"]').press("Enter")

    page.get_by_role("button", name="Mail", exact=True).wait_for(
        state="visible", timeout=config.W1_LOGIN_WAIT_SECONDS * 1_000
    )
    print("  W1 login OK — Mail button visible.")


def _ensure_w1_ready(page):
    """Navigate to W1, dismiss Knox popups, and log in if needed."""
    page.goto(config.W1_URL)
    page.wait_for_load_state("domcontentloaded")
    time.sleep(1)

    # Happy path — profile still has a valid session.
    if _mail_button_visible(page):
        _dismiss_ok_popups(page)
        return

    # Session expired or first run — re-authenticate (don't just click OK and stop).
    if _has_expired_session(page):
        print("W1 session expired — signing in again...")
        _acknowledge_expired_session(page)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(1)
    elif _needs_w1_login(page):
        print("W1 not logged in — recovering...")

    if not _mail_button_visible(page):
        _w1_login(page)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(1)

    _dismiss_ok_popups(page)

    if not _mail_button_visible(page):
        raise RuntimeError(
            "W1 is not ready — Mail button not found after login recovery. "
            "Run python download.py once and sign in manually in the Chrome window."
        )


def _mail_frame(page):
    return page.locator('iframe[title="Mail"]').content_frame


def _open_mail_frame(page):
    """Open W1 mail — matches Playwright codegen."""
    _ensure_w1_ready(page)
    page.get_by_role("button", name="Mail", exact=True).click()
    _dismiss_ok_popups(page)
    frame = _mail_frame(page)
    frame.locator("body").wait_for(state="attached", timeout=15_000)
    return frame


def _subject_regex(subject: str):
    return re.compile(rf"^{re.escape(subject)}$")


def _email_row_exact(frame, subject: str):
    return frame.locator("div").filter(has_text=_subject_regex(subject))


def _open_mailbox(frame, mailbox):
    frame.get_by_role("button", name=mailbox).click()
    time.sleep(1.5)


def _peek_subjects(frame, subject: str, limit=8):
    """Log nearby email titles to help debug subject mismatches."""
    hints = []
    try:
        rows = frame.locator("div").filter(has_text=re.compile(re.escape(subject[:12]), re.I))
        for i in range(min(rows.count(), limit)):
            try:
                line = rows.nth(i).inner_text(timeout=1_000).strip().split("\n")[0][:80]
                if line and line not in hints:
                    hints.append(line)
            except Exception:
                continue
    except Exception:
        pass
    return hints


def _click_matching_email(frame, subject: str):
    """
    Click the email row. Tries codegen exact match, then partial contains.
    The /slashes/ in logs are NOT part of your subject — just debug formatting.
    """
    errors = []

    # 1) Codegen: div + anchored regex (Product Extract - SGE+GCC)
    try:
        row = _email_row_exact(frame, subject)
        row.first.wait_for(state="visible", timeout=10_000)
        row.first.scroll_into_view_if_needed()
        row.first.click(timeout=10_000)
        time.sleep(0.5)
        print(f"  Clicked email (exact): {subject}")
        return subject
    except Exception as e:
        errors.append(f"exact: {e}")

    # 2) Partial — row contains subject text (extra date/sender text on the div)
    try:
        row = frame.locator("div").filter(has_text=subject)
        row.first.wait_for(state="visible", timeout=10_000)
        row.first.scroll_into_view_if_needed()
        row.first.click(timeout=10_000)
        time.sleep(0.5)
        print(f"  Clicked email (contains): {subject}")
        return subject
    except Exception as e:
        errors.append(f"contains: {e}")

    # 3) get_by_text fallback
    try:
        row = frame.get_by_text(subject, exact=True)
        row.first.click(timeout=10_000)
        time.sleep(0.5)
        print(f"  Clicked email (get_by_text): {subject}")
        return subject
    except Exception as e:
        errors.append(f"text: {e}")

    hints = _peek_subjects(frame, subject)
    hint_txt = f" Visible nearby: {hints}" if hints else ""
    raise RuntimeError(
        f"Could not click email with subject '{subject}'.{hint_txt} "
        f"Check Mail Jobs — subject must match the email title exactly. ({errors[-1]})"
    )


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

    # Back to mailbox list (previous job may have left us inside an open email)
    try:
        frame.get_by_role("button", name=mailbox).click(timeout=3_000)
        time.sleep(0.5)
    except Exception:
        frame = _open_mail_frame(page)

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
                err = str(e).lower()
                if "expired" in err or "session" in err or "not logged" in err:
                    try:
                        print(f"Session issue on {mail_filter['id']} — re-authenticating...")
                        frame = _open_mail_frame(page)
                        items = check_filter(page, frame, mail_filter, processed_subjects)
                        for item in items:
                            summary["downloads"].append(item)
                            if on_download:
                                on_download(item)
                        continue
                    except Exception as retry_e:
                        e = retry_e
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
