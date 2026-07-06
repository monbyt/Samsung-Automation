# Order Extract + Mail Monitor

Automated W1 mail monitoring → encrypted Excel decrypt → SQL database → LAN dashboard.

## Project layout
| Path | What it does |
|------|--------------|
| `config.py` | All settings — mail filters, DB, dashboard, NERP. **Edit this.** |
| `mail/reader.py` | W1 mail navigation + Excel download |
| `mail/monitor.py` | **Always-on mail monitor** (checks every N minutes) |
| `excel_decrypt.py` | Decrypts password-protected Excel via Excel COM |
| `parse_to_db.py` | Loads decrypted Excel into SQL tables |
| `db.py` | Database schema + monitor status |
| `dashboard.py` | **LAN dashboard** — overview, mail status, data explorer, SQL |
| `download.py` | One-shot download (legacy) |
| `orchestrator.py` | One-shot download + parse |
| `nerp/rpa.py` | NERP RPA workflow |

## One-time setup (Windows PC with Excel installed)
```bat
set NO_PROXY=*
pip install -r requirements.txt
python -m playwright install chrome
```

## Configure mail filters
Edit `MAIL_FILTERS` in `config.py` — one entry per email type:
```python
MAIL_FILTERS = [
    {
        "id": "order_extract",
        "mailbox": "Extract",
        "subject": r"Order Extract - AE/GCC",   # regex
        "table": "orders",
    },
]
```

## Always-on monitoring (recommended)
On the PC that stays on 24/7, open **two** windows (or use `run_services.bat`):

**1. Mail monitor** — checks W1 mail every 10 min, downloads + decrypts + loads DB:
```bat
python mail\monitor.py
```
Or double-click `run_monitor.bat`.

**2. Dashboard** — colleagues on the LAN can view/query data:
```bat
python dashboard.py
```
Or double-click `run_dashboard.bat`.

Open from any PC on the network: **http://\<monitor-pc-ip\>:5000**

Dashboard pages:
- **Overview** — ingestion stats
- **Mail Monitor** — check history + active filters
- **Data Explorer** — browse tables
- **SQL Query** — read-only SELECT for ad-hoc queries

JSON API (for integrations):
- `GET /api/health`
- `GET /api/tables`
- `GET /api/table/orders?limit=100&offset=0`
- `GET /api/ingestions`

## First W1 login
```bat
python download.py
```
Log into w1.samsung.net, dismiss Knox popups. Session saved in `chrome-profile/`.

## NERP RPA
```bat
set NERP_PASSWORD=your-password
python run_nerp.py
```

## Legacy scheduler (optional)
`run_scheduler.bat` still works — runs download+parse every 2 hours.
The mail monitor is better for continuous watching.

## Network / production tips
- Keep the monitor PC awake (disable sleep on AC power).
- Allow port **5000** through Windows Firewall for LAN dashboard access.
- For heavy concurrent querying, switch `DB_URL` in `config.py` to MySQL.
