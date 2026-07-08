"""
SAP WebGUI helpers for recorded RPA scripts — reliable upload with .first locators.
"""
import os

SHELL_IFRAME = 'iframe[name="application-Shell-startGUI-iframe"]'


def sap_upload_file(page, upload_path: str) -> None:
    """
    Upload via SAP in-page file browser (not the Windows native dialog).
    Opens the webgui file picker, then set_input_files on the hidden input.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    path = upload_path or os.environ.get("RPA_UPLOAD_FILE", "")
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(f"Upload file not found: {path!r}")

    print(f"[RPA] SAP upload starting: {path}")
    shell = page.frame_locator(SHELL_IFRAME)

    shell.get_by_role("textbox", name="Upload file Required").first.click()
    shell.locator("#ls-inputfieldhelpbutton").first.click()
    shell.get_by_role("button", name="OK").first.click()
    try:
        shell.get_by_role("button", name="OK").first.click(timeout=5_000)
    except PlaywrightTimeout:
        pass

    shell.locator("#webgui_filebrowser_file_upload").set_input_files(path)
    print("[RPA] SAP upload done")
