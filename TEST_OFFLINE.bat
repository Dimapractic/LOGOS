@echo off
setlocal
cd /d "%~dp0project"
"..\runtime\python\python.exe" -m unittest discover -s tests -v
set "LOGOS_TEST_CODE=%errorlevel%"
echo.
if "%LOGOS_TEST_CODE%"=="0" (echo All tests passed.) else (echo Tests failed. Read the output above.)
pause
exit /b %LOGOS_TEST_CODE%
