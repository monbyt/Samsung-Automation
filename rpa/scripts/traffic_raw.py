"""
Tableau Traffic Raw download (d2cbi).

Credentials come from Settings (NERP/RPA login + MFA OTP). Never hardcode
passwords or one-time codes here — Run injects RPA_USERNAME / RPA_PASSWORD /
RPA_OTP() at fill time.

Clones (traffic_sales_raw, traffic_mx, …) reuse this file; only TRAFFIC_VIEW /
start_url is rewritten per job.
"""
import re

from playwright.sync_api import Playwright, sync_playwright

TRAFFIC_VIEW = (
    "https://d2cbi.sec.samsung.net/#/views/03_ALLDIV/03-7_TrafficRaw"
    "?:embed=n&:iid=1&:origin=card_share_link"
)


def _goto(page, url: str, attempts: int = 4) -> None:
    """Navigate with soft wait + retries — d2cbi often hits ERR_CONNECTION_RESET."""
    last = None
    for i in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            return
        except Exception as exc:
            last = exc
            msg = str(exc)
            transient = any(
                h in msg
                for h in (
                    "ERR_CONNECTION_RESET",
                    "ERR_CONNECTION_CLOSED",
                    "ERR_CONNECTION_ABORTED",
                    "ERR_NETWORK_CHANGED",
                    "ERR_TIMED_OUT",
                    "ERR_EMPTY_RESPONSE",
                    "net::ERR_",
                    "Timeout",
                    "Target closed",
                    "has been closed",
                )
            )
            closed = any(
                h in msg
                for h in ("Target closed", "has been closed", "Browser closed")
            )
            if closed or not transient or i >= attempts:
                raise
            wait_ms = min(8000, 1500 * i)
            print(f"[RPA] goto failed {i}/{attempts}: {exc} — retry in {wait_ms}ms", flush=True)
            try:
                page.wait_for_timeout(wait_ms)
            except Exception:
                raise last
    if last:
        raise last


def _login_if_needed(page) -> None:
    """Fill Tableau SSO when the login form is present; skip if already signed in."""
    user = page.get_by_role("textbox", name="Username")
    try:
        user.wait_for(state="visible", timeout=15_000)
    except Exception:
        print("[RPA] No login form — already authenticated (or view loaded)", flush=True)
        return

    pw = page.get_by_role("textbox", name="Password")
    pw.wait_for(state="visible", timeout=30_000)

    if not (RPA_USERNAME or "").strip() or not (RPA_PASSWORD or "").strip():
        raise RuntimeError(
            "RPA username/password empty — set them under "
            "Settings → Tableau / d2cbi login before Run."
        )

    user.click()
    user.fill("")
    user.fill(RPA_USERNAME)
    # Do not press Enter here — wait until password is in.
    page.wait_for_timeout(300)

    pw.click()
    pw.fill("")
    pw.fill(RPA_PASSWORD)
    page.wait_for_timeout(300)

    # Confirm the password box actually has text before submitting.
    pw_len = pw.evaluate("el => (el.value || '').length")
    print(f"[RPA] Login fields ready — user={len(RPA_USERNAME)} chars, password={pw_len} chars")
    if not pw_len:
        raise RuntimeError("Password field is still empty — not clicking Sign In")

    page.get_by_role("button", name="Sign In").click()

    # Fresh TOTP from Settings → MFA OTP (same secret as Google Auth).
    otp = page.get_by_role("textbox", name="One-time code")
    otp.wait_for(state="visible", timeout=60_000)
    otp.fill(RPA_OTP())
    page.get_by_role("button", name="Sign In").click()

    page.wait_for_url(re.compile(r"d2cbi\.sec\.samsung\.net"), timeout=120_000)


def _download_crosstab_csv(page) -> None:
    viz = page.locator('iframe[title="Data Visualization"]').content_frame
    viz.get_by_role("button", name="Download").wait_for(state="visible", timeout=120_000)
    viz.get_by_role("button", name="Download").click()
    viz.get_by_text("Crosstab").click()
    viz.locator("label").filter(has_text="CSV").click()
    with page.expect_download(timeout=180_000) as download_info:
        viz.get_by_label("Download Crosstab").get_by_role(
            "button", name="Download"
        ).click()
    download = download_info.value
    print(f"[RPA] Downloaded: {download.suggested_filename}", flush=True)


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(channel="chrome", headless=False)
    context = browser.new_context()
    page = context.new_page()

    # Start at the Tableau view — SSO/MFA redirects here when needed.
    # Do NOT use a one-time mfa-d2cbi .../auth?state=... URL from codegen.
    _goto(page, TRAFFIC_VIEW)
    _login_if_needed(page)
    _goto(page, TRAFFIC_VIEW)
    _download_crosstab_csv(page)

    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
