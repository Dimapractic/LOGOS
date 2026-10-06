@echo off
setlocal
cd /d "%~dp0"
if not exist "runtime\python\python.exe" goto missing
"runtime\python\python.exe" "project\offline_check.py"
if errorlevel 1 goto fail
"runtime\python\python.exe" "project\run.py"
if errorlevel 1 goto fail
exit /b 0
:missing
echo Portable Python was not found. Extract the complete ZIP first.
pause
exit /b 1
:fail
echo.
echo Startup failed. Read the error above and check project\data\logs.
pause
exit /b 1
