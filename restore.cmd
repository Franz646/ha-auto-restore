@echo off
rem ha-auto-restore launcher for Windows. No argument: "auto" (diagnose, then the steps in order).
if "%~1"=="" (python "%~dp0ha_restore.py" auto) else (python "%~dp0ha_restore.py" %*)
pause
