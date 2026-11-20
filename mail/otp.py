"""
TOTP (authenticator) helper for MFA.

Store the same base32 secret you put in otp.exe / Google Authenticator.
Then current_code() returns the live 6-digit code for RPA login fills.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

# otpauth://totp/Label?secret=BASE32&issuer=...
_OTPAUTH_RE = re.compile(r"otpauth://totp/[^\s]+", re.I)
_SECRET_KEY_RE = re.compile(
    r"(?:^|[\s;,])(?:secret|otp_secret|totp_secret)\s*[=:]\s*([A-Z2-7=]+)",
    re.I,
)
_BASE32_RE = re.compile(r"^[A-Z2-7=]+$")


def normalize_secret(raw: str) -> str:
    """Accept a raw secret, otpauth URI, or otp_secret.txt contents → base32 secret."""
    text = (raw or "").strip()
    if not text:
        return ""

    m = _OTPAUTH_RE.search(text)
    if m:
        return _secret_from_otpauth(m.group(0))

    if text.lower().startswith("otpauth://"):
        return _secret_from_otpauth(text)

    keyed = _SECRET_KEY_RE.search(text)
    if keyed:
        return _clean_base32(keyed.group(1))

    # First non-empty line that looks like base32 (otp_secret.txt is often one line).
    for line in text.splitlines():
        line = line.strip().strip('"').strip("'")
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("otpauth://"):
            return _secret_from_otpauth(line)
        cleaned = _clean_base32(line)
        if len(cleaned) >= 16 and _BASE32_RE.match(cleaned):
            return cleaned

    cleaned = _clean_base32(text)
    if len(cleaned) >= 16 and _BASE32_RE.match(cleaned):
        return cleaned
    raise ValueError(
        "Could not find a TOTP secret. Paste the base32 key, an otpauth:// URI, "
        "or the contents of otp_secret.txt."
    )


def _clean_base32(value: str) -> str:
    return re.sub(r"[\s\-]", "", (value or "").upper())


def _secret_from_otpauth(uri: str) -> str:
    parsed = urlparse(uri.strip())
    if parsed.scheme.lower() != "otpauth":
        raise ValueError("Not an otpauth:// URI")
    qs = parse_qs(parsed.query)
    secret = (qs.get("secret") or [""])[0]
    secret = _clean_base32(unquote(secret))
    if not secret:
        raise ValueError("otpauth URI has no secret=")
    return secret


def get_stored_secret() -> str:
    from mail.settings_db import get_otp_secret
    return get_otp_secret()


def save_secret(raw: str) -> str:
    """Normalize and persist. Returns the cleaned secret."""
    from mail.settings_db import save_otp_secret
    secret = normalize_secret(raw)
    save_otp_secret(secret)
    return secret


def clear_secret() -> None:
    from mail.settings_db import clear_otp_secret
    clear_otp_secret()


def current_code(secret: Optional[str] = None) -> str:
    """Live 6-digit TOTP. Empty string if no secret is configured."""
    try:
        import pyotp
    except ImportError as e:
        raise RuntimeError(
            "pyotp is not installed. Run: pip install pyotp"
        ) from e

    sec = _clean_base32(secret or get_stored_secret())
    if not sec:
        return ""
    return pyotp.TOTP(sec).now()


def code_status(secret: Optional[str] = None) -> dict:
    """Code + seconds remaining in this 30s window (for Settings UI)."""
    try:
        import pyotp
    except ImportError:
        return {
            "configured": False,
            "code": "",
            "remaining": 0,
            "error": "pyotp is not installed (pip install pyotp)",
        }

    sec = _clean_base32(secret or get_stored_secret())
    if not sec:
        return {"configured": False, "code": "", "remaining": 0, "error": ""}

    totp = pyotp.TOTP(sec)
    import time
    remaining = int(totp.interval - (time.time() % totp.interval))
    try:
        code = totp.now()
    except Exception as e:
        return {"configured": True, "code": "", "remaining": 0, "error": str(e)}
    return {"configured": True, "code": code, "remaining": remaining, "error": ""}


if __name__ == "__main__":
    status = code_status()
    if status.get("error"):
        print(status["error"])
        raise SystemExit(1)
    if not status["configured"]:
        print("No OTP secret in Settings yet.")
        raise SystemExit(1)
    print(f"{status['code']}  ({status['remaining']}s left)")
