@echo off
rem ── AethvionDB launcher ────────────────────────────────────────────────────
rem First run sets up a local virtual environment and installs the package;
rem subsequent runs just start the server and open the explorer.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [AethvionDB] First run - setting up environment...
    python -m venv .venv || (
        echo Could not create a virtual environment. Is Python 3.10+ installed and on PATH?
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -e .
)

echo [AethvionDB] Starting server at http://127.0.0.1:7475 ...
".venv\Scripts\python.exe" -m aethviondb.server
pause
