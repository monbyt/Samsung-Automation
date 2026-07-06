"""
Central configuration for Samsung automation pipelines.
Change values here and every script picks them up.
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── W1 / Order Extract ─────────────────────────────────────────
DOWNLOAD_DIR = r"C:\Users\m.tasoglu\Desktop\Order-Extract"

# Chrome profile that stays logged into W1 (created on first run)
PROFILE_DIR = os.path.join(BASE_DIR, "chrome-profile")

W1_URL = "http://w1.samsung.net"

# Set True to hide the browser window on scheduled W1 runs.
HEADLESS = False

# ── Mail monitoring ────────────────────────────────────────────
# How often the mail monitor checks for new emails (minutes).
# Keep the monitor PC on and run: python mail/monitor.py
MONITOR_INTERVAL_MINUTES = 10

# Each filter = one mailbox + subject pattern → one SQL table.
# Add more dicts to monitor additional email types.
MAIL_FILTERS = [
    {
        "id": "order_extract",
        "mailbox": "Extract",
        "subject": r"Order Extract - AE/GCC",   # regex match
        "table": "orders",
    },
    # Example — uncomment and edit to add another feed:
    # {
    #     "id": "sales_report",
    #     "mailbox": "Extract",
    #     "subject": r"Sales Report",
    #     "table": "sales_reports",
    # },
]

# Backward-compatible aliases (first filter)
MAILBOX = MAIL_FILTERS[0]["mailbox"]
MAIL_SUBJECT = MAIL_FILTERS[0]["subject"]

# ── NERP RPA ───────────────────────────────────────────────────
NERP_SSO_URL = "https://sts.secsso.net/adfs/ls/"
NERP_PROFILE_DIR = os.path.join(BASE_DIR, "chrome-profile-nerp")
NERP_HEADLESS = False

NERP_USERNAME = os.environ.get("NERP_USERNAME", "m.tasoglu")
NERP_PASSWORD = os.environ.get("NERP_PASSWORD", "")

NERP_UPLOAD_FILE = os.path.join(BASE_DIR, "data", "Book1.xlsx")
NERP_PROGRAM_UPLOAD = "ZLSDF50270"
NERP_PROGRAM_PI = "ZSDM31520"

# ── Database ───────────────────────────────────────────────────
# SQLite by default (WAL mode enabled for concurrent dashboard reads).
# For heavy LAN querying, switch to MySQL on a small server:
#   DB_URL = "mysql+pymysql://user:password@host:3306/dbname"
DB_URL = f"sqlite:///{os.path.join(BASE_DIR, 'orders.db')}"

ORDERS_TABLE = MAIL_FILTERS[0]["table"]

# ── Dashboard (LAN-accessible) ─────────────────────────────────
# Bind 0.0.0.0 so colleagues on the same network can open the dashboard.
# Example: http://192.168.1.50:5000
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 5000

# ── Legacy scheduler ───────────────────────────────────────────
INTERVAL_HOURS = 2
