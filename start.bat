@echo off
cd /d "%~dp0"

echo Installing / checking dependencies...
python -m pip install flask flask-sock paramiko

echo Checking for existing instance on port 5555...
netstat -an | findstr ":5555 " | findstr "LISTENING" >nul 2>&1
if %errorlevel% equ 0 (
    echo Dashboard already running — opening browser.
) else (
    echo Starting Tailnet Dashboard...
    start "Tailnet Dashboard" /min cmd /c "python dashboard.py > dashboard.log 2>&1"
    timeout /t 2 /nobreak > nul
    echo Dashboard started. Log: %~dp0dashboard.log
)

start http://localhost:5555
