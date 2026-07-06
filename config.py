"""
Central configuration for the Order Extract pipeline.
Change values here and every script picks them up.
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Folders ────────────────────────────────────────────────────
# Downloaded Excel files land here
DOWNLOAD_DIR = r"C:\Users\m.tasoglu\Desktop\Order-Extract"

# Chrome profile that stays logged into W1 (created on first run)
PROFILE_DIR = os.path.join(BASE_DIR, "chrome-profile")

# ── Database ───────────────────────────────────────────────────
# Default: a local SQLite file (zero setup, works anywhere).
# To use your Azure MySQL later, just swap this line, e.g.:
#   DB_URL = "mysql+pymysql://user:password@host:3306/dbname"
DB_URL = f"sqlite:///{os.path.join(BASE_DIR, 'orders.db')}"

# Table that holds the parsed order rows
ORDERS_TABLE = "orders"

# ── Pipeline behaviour ─────────────────────────────────────────
# How often the scheduler runs (in hours)
INTERVAL_HOURS = 2

# W1 portal + the mailbox / subject to grab
W1_URL = "http://w1.samsung.net"
MAILBOX = "Extract"
MAIL_SUBJECT = "Order Extract - AE/GCC"

# Set True to hide the browser window on scheduled runs.
# Keep False the first time so you can log in + clear Knox popups.
HEADLESS = False
