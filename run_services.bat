@echo off
set NO_PROXY=*
set no_proxy=*
echo Starting mail monitor and dashboard...
start "Mail Monitor" cmd /k run_monitor.bat
start "Dashboard" cmd /k run_dashboard.bat
echo Both services launched in separate windows.
