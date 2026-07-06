"""
Downloads the latest 'Order Extract - AE/GCC' file from W1 mail
straight into DOWNLOAD_DIR.

We try three layers to avoid the native 'Save As' popup:
  1. Chrome profile prefs (no prompt, fixed folder).
  2. CDP download behaviour at runtime.
  3. Press Enter on the native Save As dialog if layers 1–2 don't catch it.
"""
import os
import re
import json

# Bypass the corporate proxy for everything this script does
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import config
from win_save_as import dismiss_save_as_dialog, wait_for_new_file


def _configure_downloads(profile_dir, download_dir):
    """Write Chrome preferences so it auto-saves without asking."""
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

    try:
        with open(prefs_path, "w", encoding="utf-8") as f:
            json.dump(prefs, f)
    except Exception as e:
        print(f"Warning: couldn't write Chrome prefs ({e})")


def download_latest():
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(config.PROFILE_DIR, exist_ok=True)

    # Kill the save prompt before Chrome even starts
    _configure_downloads(config.PROFILE_DIR, config.DOWNLOAD_DIR)

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            config.PROFILE_DIR,
            channel="chrome",
            headless=config.HEADLESS,
            accept_downloads=True,
            args=["--disable-popup-blocking", "--no-first-run"],
        )

        page = context.pages[0] if context.pages else context.new_page()

        # Backup: force download behaviour over CDP (no prompt)
        try:
            cdp = context.new_cdp_session(page)
            cdp.send("Browser.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": config.DOWNLOAD_DIR,
                "eventsEnabled": True,
            })
        except Exception as e:
            print(f"Note: CDP download hint skipped ({e})")

        page.goto(config.W1_URL)
        page.wait_for_load_state("domcontentloaded")

        # Open Mail
        page.get_by_role("button", name="Mail").click()
        page.get_by_role("button", name="Mail", exact=True).click()

        frame = page.locator('iframe[title="Mail"]').content_frame

        # Open the Extract mailbox
        frame.get_by_role("button", name=config.MAILBOX, exact=True).click()

        # Open the target email
        frame.locator("div").filter(
            has_text=re.compile(rf"^{re.escape(config.MAIL_SUBJECT)}$")
        ).first.click()

        save_path = None
        try:
            with page.expect_download(timeout=8_000) as download_info:
                frame.get_by_role("button", name="Download").click()
            download = download_info.value
            save_path = os.path.join(config.DOWNLOAD_DIR, download.suggested_filename)
            download.save_as(save_path)
        except PlaywrightTimeout:
            print("Browser didn't capture download — pressing Enter on Save As...")
            dismiss_save_as_dialog(timeout=60)
            save_path = wait_for_new_file(config.DOWNLOAD_DIR, timeout=60)

        # Dismiss any confirmation dialog
        try:
            frame.get_by_role("button", name="OK").click(timeout=3000)
        except Exception:
            pass

        context.close()
        return save_path


if __name__ == "__main__":
    path = download_latest()
    print(f"Saved: {path}")
