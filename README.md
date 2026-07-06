# Order Extract Pipeline

Automated download → parse → SQL → dashboard for the W1 "Order Extract - AE/GCC" mail,
plus a separate NERP RPA workflow.

## Project layout
| Path | What it does |
|------|--------------|
| `config.py` | All settings (W1, NERP, DB, schedule). Edit this. |
| `download.py` | W1 — downloads the latest Order Extract file. |
| `parse_to_db.py` | Loads the newest Excel file into the SQL database. |
| `orchestrator.py` | W1 pipeline: download + parse (one full cycle). |
| `scheduler.py` | Cron job — runs W1 download + parse every N hours. |
| `dashboard.py` | Web dashboard at http://127.0.0.1:5000 |
| `nerp/rpa.py` | NERP RPA — SSO login, upload, P/I print workflow. |
| `run_nerp.py` | Entry point for the NERP script. |

## One-time setup
```
set NO_PROXY=*
pip install -r requirements.txt
python -m playwright install chrome
```

## W1 — first run (log in once)
```
python download.py
```
A Chrome window opens. Log into w1.samsung.net, dismiss the Knox tray
popup and any "allow access" prompt. After this it stays logged in.

## W1 — running it
- **One full cycle:** `python orchestrator.py` or double-click `run_now.bat`
- **Start the cron job:** double-click `run_scheduler.bat` (leave it open)
- **Open the dashboard:** double-click `run_dashboard.bat`, then go to
  http://127.0.0.1:5000 in Chrome

## NERP RPA
1. Put your upload Excel at `data/Book1.xlsx` (or change `NERP_UPLOAD_FILE` in `config.py`).
2. Set credentials — either in `config.py` or via env vars:
   ```
   set NERP_USERNAME=m.tasoglu
   set NERP_PASSWORD=your-password-here
   ```
3. Run:
   ```
   python run_nerp.py
   ```
   Or double-click `run_nerp.bat`.

First run logs into SSO and saves the session in `chrome-profile-nerp/`.

## Change the schedule
Edit `INTERVAL_HOURS` in `config.py`.

## Switch to Azure MySQL later
Change `DB_URL` in `config.py` to your MySQL connection string —
everything else stays the same.
