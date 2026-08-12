"""
W1 mail reader — navigate mailboxes, find matching emails, download Excel attachments.

Only unread mails are processed (bold / unread class). If nothing is unread, we download nothing.
`a.not-open` is NOT unread — in W1 that means "not the currently opened tab".
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
MAX_UNREAD_PER_TICK = 20
# Extra screens to scan below the current view. Do NOT walk a huge inbox.
MAX_LIST_SCROLLS = 5

# Click unread matching subjects inside the mail iframe.
# W1 unread = bold / "unread" class. Do NOT use a.not-open — that means
# "not the currently opened tab", so it matches old/read rows too.
# The list is virtualized: rows below the fold are not in the DOM until we scroll.
_CLICK_UNREAD_JS = """
(subject) => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const target = norm(subject);
  if (!target) return { clicked: false, reason: 'empty-subject', hits: 0, unreadHits: 0, debug: [] };

  const isUnread = (el) => {
    let n = el;
    for (let i = 0; i < 6 && n && n !== document.body; i++, n = n.parentElement) {
      const cls = (n.className || '').toString().toLowerCase();
      if (/\\bunread\\b|\\bnot-read\\b|\\bis-unread\\b|\\bmail-unread\\b/.test(cls)) {
        return true;
      }
      const fw = getComputedStyle(n).fontWeight;
      if (fw === 'bold' || fw === 'bolder' || parseInt(fw, 10) >= 600) {
        return true;
      }
    }
    return false;
  };

  const matches = (text) => {
    const t = norm(text);
    if (!t || t.length > 180) return false;
    return t === target || t.startsWith(target) || t.includes(target);
  };

  const nodes = [...document.querySelectorAll('a, span, div, [role="link"]')];
  const debug = [];
  let hits = 0;
  let unreadHits = 0;
  for (const el of nodes) {
    const raw = (el.innerText || '').trim();
    if (!matches(raw)) continue;
    hits += 1;
    const unread = isUnread(el);
    if (unread) unreadHits += 1;
    if (debug.length < 12) {
      debug.push({ text: raw.slice(0, 80), unread, cls: (el.className || '').toString().slice(0, 80) });
    }
    if (!unread) continue;
    el.scrollIntoView({ block: 'center', inline: 'nearest' });
    el.click();
    return { clicked: true, text: raw.slice(0, 120), hits, unreadHits, debug };
  }
  return { clicked: false, reason: 'no-unread-match', hits, unreadHits, debug };
}
"""

_MARK_LIST_SCROLLER_JS = """
() => {
  const prev = document.querySelector('[data-w1-mail-scroller="1"]');
  if (prev) prev.removeAttribute('data-w1-mail-scroller');
  let best = null;
  let bestScore = 0;
  for (const n of document.querySelectorAll('div, ul, table, tbody, section')) {
    const extra = n.scrollHeight - n.clientHeight;
    if (extra < 40 || n.clientHeight < 80 || n.clientWidth < 120) continue;
    const st = getComputedStyle(n);
    if (!/(auto|scroll|overlay|hidden)/.test(st.overflowY)) continue;
    const links = n.querySelectorAll('a, [role="link"], tr, li').length;
    const score = extra + links * 80;
    if (score > bestScore) {
      best = n;
      bestScore = score;
    }
  }
  if (!best) return { found: false };
  best.setAttribute('data-w1-mail-scroller', '1');
  return {
    found: true,
    extra: best.scrollHeight - best.clientHeight,
    clientHeight: best.clientHeight,
    scrollHeight: best.scrollHeight,
  };
}
"""

_SCROLL_LIST_TOP_JS = """
() => {
  const el = document.querySelector('[data-w1-mail-scroller="1"]');
  if (!el) return false;
  el.scrollTop = 0;
  return true;
}
"""

_SCROLL_LIST_DOWN_JS = """
() => {
  const el = document.querySelector('[data-w1-mail-scroller="1"]');
  if (!el) return { moved: false, atEnd: true };
  const before = el.scrollTop;
  const step = Math.max(Math.floor(el.clientHeight * 0.8), 120);
  el.scrollTop = Math.min(el.scrollHeight, el.scrollTop + step);
  const moved = el.scrollTop > before + 1;
  const atEnd = el.scrollTop + el.clientHeight >= el.scrollHeight - 4;
  return { moved, atEnd, scrollTop: el.scrollTop, scrollHeight: el.scrollHeight };
}
"""


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


def _log_unread_debug(result: dict, *, scrolled: int = 0) -> None:
    debug = result.get("debug") or []
    hits = result.get("hits")
    unread_hits = result.get("unreadHits")
    extra = f" scrolled={scrolled}" if scrolled else ""
    if hits is not None:
        print(f"  Subject hits: {hits} ({unread_hits} unread){extra}")
    elif debug:
        print(f"  Subject hits ({len(debug)}){extra}:")
    for row in debug:
        flag = "UNREAD" if row.get("unread") else "read"
        print(f"    [{flag}] {row.get('text')!r}")


def _click_first_unread(mail, subject: str) -> bool:
    """Click an unread row whose subject matches, scrolling the list if needed.

    W1 only keeps visible rows in the DOM, so unread mail a bit further
    down is missed unless we scroll. Caps at MAX_LIST_SCROLLS screens so a
    1000-mail inbox is never fully walked.
    """
    try:
        scroller = mail.evaluate(_MARK_LIST_SCROLLER_JS)
        if isinstance(scroller, dict) and scroller.get("found"):
            mail.evaluate(_SCROLL_LIST_TOP_JS)
            time.sleep(0.25)
        else:
            scroller = {"found": False}
    except Exception as e:
        print(f"  Mail list scroller: {e}")
        scroller = {"found": False}

    last_result: dict = {}
    can_scroll = bool(scroller.get("found"))
    if can_scroll:
        print(f"  Mail list is scrollable — scanning at most {MAX_LIST_SCROLLS} screens down.")
    else:
        print(f"  No mail-list scroller found — at most {MAX_LIST_SCROLLS} PageDowns.")

    for step in range(MAX_LIST_SCROLLS + 1):
        try:
            result = mail.evaluate(_CLICK_UNREAD_JS, subject)
        except Exception as e:
            print(f"  Unread scan failed: {e}")
            return False

        if not isinstance(result, dict):
            return False
        last_result = result

        if result.get("clicked"):
            if step == 0:
                _log_unread_debug(result)
            else:
                _log_unread_debug(result, scrolled=step)
            print(f"  Opened unread: {result.get('text')!r}")
            return True

        if step >= MAX_LIST_SCROLLS:
            break
        advanced = False
        if can_scroll:
            try:
                moved = mail.evaluate(_SCROLL_LIST_DOWN_JS)
                advanced = isinstance(moved, dict) and bool(moved.get("moved"))
            except Exception:
                advanced = False
        else:
            try:
                mail.locator("body").press("PageDown")
                advanced = True
            except Exception:
                advanced = False
        if not advanced:
            break
        time.sleep(0.35)

    _log_unread_debug(last_result)
    print(f"  No unread mail matching {subject!r} ({last_result.get('reason')}).")
    return False


def check_filter(page, mail_filter, processed_subjects, on_download=None):
    """Process up to MAX_UNREAD_PER_TICK unread mails matching subject.

    If there are no unread matches, returns [] — never downloads a read/old mail.
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
        if not _click_first_unread(mail, subject):
            if i == 0:
                print(f"[{filter_id}] No unread mails matching subject — skipping download.")
            else:
                print(f"[{filter_id}] No more unread mails.")
            break

        print(f"[{filter_id}] Processing unread mail {i + 1}/{MAX_UNREAD_PER_TICK}")
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
    raise RuntimeError("No unread matching email found to download.")
