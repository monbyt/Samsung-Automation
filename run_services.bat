@echo off
set NO_PROXY=*
set no_proxy=*
echo Starting dashboard (includes mail job scheduler)...
python dashboard.py
pause
