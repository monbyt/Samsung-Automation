"""
Downloads the latest matching email attachment from W1 mail.
Thin wrapper around mail.reader for backward compatibility.
"""
import os

os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

from mail.reader import download_latest

if __name__ == "__main__":
    path = download_latest()
    print(f"Saved: {path}")
