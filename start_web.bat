@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>&1
if not errorlevel 1 (
    set "PYTHON=py"
    set "PYTHON_ARGS=-3"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo Python 3 was not found. Install Python 3.11+ and try again.
        pause
        exit /b 1
    )
    set "PYTHON=python"
    set "PYTHON_ARGS="
)

echo Starting AudioVTTForge web service...
echo Browser: http://127.0.0.1:8765/
start "AudioVTTForge Browser" powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8765/'"
%PYTHON% %PYTHON_ARGS% -m audiovttforge.web --host 127.0.0.1 --port 8765

if errorlevel 1 (
    echo.
    echo AudioVTTForge stopped with an error.
    pause
)
