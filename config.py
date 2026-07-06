"""
Central configuration for Samsung automation pipelines.
Change values here and every script picks them up.
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── W1 / Order Extract ─────────────────────────────────────────
# Downloaded Excel files land here
DOWNLOAD_DIR = r"C:\Users\m.tasoglu\Desktop\Order-Extract"

# Chrome profile that stays logged into W1 (created on first run)
PROFILE_DIR = os.path.join(BASE_DIR, "chrome-profile")

# W1 portal + the mailbox / subject to grab
W1_URL = "http://w1.samsung.net"
MAILBOX = "Extract"
MAIL_SUBJECT = "Order Extract - AE/GCC"

# Set True to hide the browser window on scheduled W1 runs.
# Keep False the first time so you can log in + clear Knox popups.
HEADLESS = False

# ── NERP RPA ───────────────────────────────────────────────────
NERP_SSO_URL = "https://sts.secsso.net/adfs/ls/"
NERP_PROFILE_DIR = os.path.join(BASE_DIR, "chrome-profile-nerp")
NERP_HEADLESS = False

# Credentials — set here or via environment variables (env wins).
NERP_USERNAME = os.environ.get("NERP_USERNAME", "m.tasoglu")
NERP_PASSWORD = os.environ.get("NERP_PASSWORD", "")

# Excel file to upload in ZLSDF50270
NERP_UPLOAD_FILE = os.path.join(BASE_DIR, "data", "Book1.xlsx")

# SAP program codes
NERP_PROGRAM_UPLOAD = "ZLSDF50270"
NERP_PROGRAM_PI = "ZSDM31520"

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
