@echo off
set NO_PROXY=*
set no_proxy=*
echo Optional: run scheduler without dashboard (normally not needed).
python mail\cron.py
pause
