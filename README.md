# Order Extract Pipeline

Automated download → parse → SQL → dashboard for the W1 "Order Extract - AE/GCC" mail.

## Files
| File | What it does |
|------|--------------|
| `config.py` | All settings (folders, DB, schedule interval). Edit this. |
| `download.py` | Downloads the latest Order Extract file into `Order Extract/`. |
| `parse_to_db.py` | Loads the newest Excel file into the SQL database. |
| `scheduler.py` | The cron job — runs download + parse every N hours. |
| `dashboard.py` | Web dashboard at http://127.0.0.1:5000 |

## One-time setup
```
set NO_PROXY=*
pip install -r requirements.txt
python -m playwright install chrome
```

## First run (do this once to log in)
```
python download.py
```
A Chrome window opens. Log into w1.samsung.net, dismiss the Knox tray
popup and any "allow access" prompt. After this it stays logged in.

## Running it
- **Start the cron job:** double-click `run_scheduler.bat` (leave it open)
- **Open the dashboard:** double-click `run_dashboard.bat`, then go to
  http://127.0.0.1:5000 in Chrome

## Change the schedule
Edit `INTERVAL_HOURS` in `config.py`.

## Switch to Azure MySQL later
Change `DB_URL` in `config.py` to your MySQL connection string —
everything else stays the same.
