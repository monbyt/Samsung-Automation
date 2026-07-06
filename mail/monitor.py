"""
Mail job scheduler process (optional — cron also runs inside dashboard).

Does NOT run any jobs on startup. Jobs run only when their next_run time is due,
or when you click "Run now" in the dashboard.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mail.cron import scheduler_loop

if __name__ == "__main__":
    scheduler_loop()
