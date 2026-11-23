"""
Tableau Traffic Raw download (d2cbi).

Credentials come from Settings (NERP/RPA login + MFA OTP). Never hardcode
passwords or one-time codes here — Run injects RPA_USERNAME / RPA_PASSWORD /
RPA_OTP() at fill time.
"""
import re

from playwright.sync_api import Playwright, sync_playwright

TRAFFIC_VIEW = (
    "https://d2cbi.sec.samsung.net/#/views/03_ALLDIV/03-7_TrafficRaw"
    "?:embed=n&:iid=1&:origin=card_share_link"
)


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(channel="chrome", headless=False)
    context = browser.new_context()
    page = context.new_page()

    # Start at the Tableau view — SSO/MFA redirects here when needed.
    # Do NOT use a one-time mfa-d2cbi .../auth?state=... URL from codegen.
    page.goto(TRAFFIC_VIEW)

    page.get_by_role("textbox", name="Username").click()
    page.get_by_role("textbox", name="Username").fill(RPA_USERNAME)
    page.get_by_role("textbox", name="Password").click()
    page.get_by_role("textbox", name="Password").fill(RPA_PASSWORD)
    page.get_by_role("button", name="Sign In").click()

    # Fresh TOTP from Settings → MFA OTP (same secret as Google Auth).
    page.get_by_role("textbox", name="One-time code").fill(RPA_OTP())
    page.get_by_role("button", name="Sign In").click()

    page.wait_for_url(re.compile(r"d2cbi\.sec\.samsung\.net"), timeout=120_000)
    page.goto(TRAFFIC_VIEW)

    viz = page.locator('iframe[title="Data Visualization"]').content_frame
    viz.get_by_role("button", name="Download").click()
    viz.get_by_text("Crosstab").click()
    viz.locator("label").filter(has_text="CSV").click()
    with page.expect_download() as download_info:
        viz.get_by_label("Download Crosstab").get_by_role(
            "button", name="Download"
        ).click()
    download = download_info.value

    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
