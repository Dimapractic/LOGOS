@echo off
cd /d "%~dp0"
"runtime\python\python.exe" "project\verify_bundle.py"
pause
