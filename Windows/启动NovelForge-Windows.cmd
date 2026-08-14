@echo off
setlocal
cd /d "%~dp0"

set "NOVELFORGE_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%NOVELFORGE_PYTHON%" (
    if defined NOVELFORGE_WINDOWS_PYTHON (
        "%NOVELFORGE_WINDOWS_PYTHON%" -m venv .venv
    ) else (
        where py >nul 2>nul
        if not errorlevel 1 (
            py -3.11 -m venv .venv
        ) else (
            where python >nul 2>nul
            if errorlevel 1 (
                echo Python 3.11 or newer was not found.
                pause
                exit /b 1
            )
            python -m venv .venv
        )
    )
    if errorlevel 1 goto :failed
    "%NOVELFORGE_PYTHON%" -m pip install --upgrade pip
    if errorlevel 1 goto :failed
    "%NOVELFORGE_PYTHON%" -m pip install -e .
    if errorlevel 1 goto :failed
)

"%NOVELFORGE_PYTHON%" -m novelforge_windows
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo NovelForge Windows failed to start.
pause
exit /b 1
