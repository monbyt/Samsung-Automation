# Order Extract + Mail Monitor

Automated W1 mail monitoring → encrypted Excel decrypt → SQL database → LAN dashboard.

## Quick start (always-on Windows PC)

```bat
pip install -r requirements.txt
python -m playwright install chrome
python download.py          REM first-time W1 login only
run_dashboard.bat           REM dashboard + cron scheduler (one window)
```

Open **http://\<pc-ip\>:5000/jobs** to manage mail cron jobs.

## Mail jobs (dashboard)

Go to **Mail Jobs** in the dashboard:

| Action | What it does |
|--------|----------------|
| **+ New mail job** | Add a mailbox + subject filter → SQL table |
| **Run now** | Download mail + decrypt + parse immediately |
| **Edit** | Change interval, subject, table, enable/disable |
| **Manual parse to SQL** | Parse an existing Excel file without checking mail |

- Jobs **do NOT run on startup** — they run when their scheduled time is due.
- Set **check every X minutes** per job.
- Use Playwright codegen to discover mailbox/subject values:
  ```bat
  python -m playwright codegen --channel chrome http://w1.samsung.net
  ```

## Project layout
| Path | What it does |
|------|--------------|
| `config.py` | Settings — download folder, DB, dashboard port |
| `dashboard.py` | LAN dashboard + embedded cron scheduler |
| `mail/cron.py` | Scheduler (optional standalone via `run_monitor.bat`) |
| `mail/jobs_db.py` | Mail job definitions in SQL |
| `mail/reader.py` | W1 mail download automation |
| `excel_decrypt.py` | Decrypt Excel via Excel COM (Windows) |
| `parse_to_db.py` | Load Excel into SQL tables |
| `nerp/rpa.py` | NERP RPA workflow |

## Dashboard pages
- **Overview** — stats
- **Mail Jobs** — cron management, run now, manual parse
- **Data Explorer** — browse tables
- **SQL Query** — read-only SELECT for LAN colleagues

API: `/api/tables`, `/api/table/orders`, `/api/ingestions`

## NERP RPA
```bat
set NERP_PASSWORD=your-password
python run_nerp.py
```

## Network tips
- Dashboard binds to `0.0.0.0:5000` — share `http://<ip>:5000` on LAN
- Allow port 5000 through Windows Firewall
- Disable PC sleep on AC power
- Microsoft Excel required for encrypted attachments
