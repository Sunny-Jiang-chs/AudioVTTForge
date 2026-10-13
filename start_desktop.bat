@echo off
setlocal
cd /d "%~dp0"

rem Override with a full path to python.exe when using another environment.
if defined AUDIOVTTFORGE_PYTHON (
    set "AVF_PYTHON=%AUDIOVTTFORGE_PYTHON%"
    goto :runtime_ready
)
if exist "D:\tools\audiovttforge-desktop\Scripts\python.exe" (
    set "AVF_PYTHON=D:\tools\audiovttforge-desktop\Scripts\python.exe"
    goto :runtime_ready
)
for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "AVF_PYTHON=%%P"
if defined AVF_PYTHON goto :runtime_ready
for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "AVF_PYTHON=%%P"
if defined AVF_PYTHON goto :runtime_ready
echo Python 3.11+ was not found. Install the dependencies and try again.
pause
exit /b 1

:runtime_ready
if not exist "%AVF_PYTHON%" (
    echo Python was not found at the configured path:
    echo "%AVF_PYTHON%"
    pause
    exit /b 1
)
for %%P in ("%AVF_PYTHON%") do set "AVF_PYTHONW=%%~dpPpythonw.exe"
if exist "%AVF_PYTHONW%" (
    start "" "%AVF_PYTHONW%" "%~dp0desktop_entry.py" %*
) else (
    rem Console-only Python distributions remain usable for troubleshooting.
    "%AVF_PYTHON%" "%~dp0desktop_entry.py" %*
    if errorlevel 1 pause
)
exit /b
