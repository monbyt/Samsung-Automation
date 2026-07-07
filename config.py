"""
Central configuration for Samsung automation pipelines.
Change values here and every script picks them up.
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    """Load .env from project root (optional — for NERP_PASSWORD etc.)."""
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.isfile(path):
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(path)
    except ImportError:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key and val and key not in os.environ:
                    os.environ[key] = val


_load_dotenv()

# ── W1 / Order Extract ─────────────────────────────────────────
DESKTOP_DIR = r"C:\Users\m.tasoglu\Desktop"
DOWNLOAD_DIR = os.path.join(DESKTOP_DIR, "Order-Extract")

# Chrome profile that stays logged into W1 (created on first run)
PROFILE_DIR = os.path.join(BASE_DIR, "chrome-profile")

W1_URL = "http://w1.samsung.net"

# Set True to hide the browser window on scheduled W1 runs.
HEADLESS = False

# ── Mail monitoring ────────────────────────────────────────────
# Scheduler checks every N seconds for jobs whose next_run time has passed.
# Jobs do NOT run immediately on startup — configure them in the dashboard.
SCHEDULER_TICK_SECONDS = 60

# Default interval when seeding jobs from MAIL_FILTERS below (hours).
DEFAULT_JOB_INTERVAL_HOURS = 2

# Each filter = one mailbox + subject pattern → one SQL table.
# Add more dicts to monitor additional email types.
MAIL_FILTERS = [
    {
        "id": "order_extract",
        "mailbox": "Extract",
        "subject": "Order Extract - AE/GCC",
        "table": "orders",
        "folder": "Order-Extract",
    },
    {
        "id": "product_extract",
        "mailbox": "Product Extract",
        "subject": "Product Extract - SGE+GCC",
        "table": "product_orders",
        "folder": "Product-Extract",
    },
]

# Embedded cron inside dashboard.py (set False to use run_monitor.bat separately).
DASHBOARD_RUNS_SCHEDULER = True

# Backward-compatible aliases (first filter)
MAILBOX = MAIL_FILTERS[0]["mailbox"]
MAIL_SUBJECT = MAIL_FILTERS[0]["subject"]
MONITOR_INTERVAL_HOURS = DEFAULT_JOB_INTERVAL_HOURS

# ── NERP RPA ───────────────────────────────────────────────────
NERP_URL = "https://nerpsr.sec.samsung.net/sap/bc/ui2/flp#Utility-home"
NERP_PROFILE_DIR = os.path.join(BASE_DIR, "chrome-profile-nerp")
NERP_HEADLESS = False

# Samsung SSO — edit here. Empty .env values are ignored; non-empty env overrides.
NERP_USERNAME = os.environ.get("NERP_USERNAME") or "m.tasoglu"
NERP_PASSWORD = os.environ.get("NERP_PASSWORD") or "Pass2002?"

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
# Optional API key for LAN access (leave empty to allow all on the network).
# Colleagues pass header:  X-API-Key: your-key-here
API_KEY = os.environ.get("API_KEY", "")

# Bind 0.0.0.0 so colleagues on the same network can open the dashboard.
# Example: http://192.168.1.50:5000
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 5000

# ── Legacy scheduler ───────────────────────────────────────────
INTERVAL_HOURS = 2
