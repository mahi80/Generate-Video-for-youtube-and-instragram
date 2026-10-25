@echo off
rem Start the HTMX WebUI (same features as webui.bat, without Streamlit).
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%CD%"

if exist "%CD%\.venv\Scripts\python.exe" (
    "%CD%\.venv\Scripts\python.exe" -m webui_htmx %*
) else if exist "%CD%\lib\python\python.exe" (
    "%CD%\lib\python\python.exe" -m webui_htmx %*
) else (
    where uv >nul 2>nul
    if errorlevel 1 (
        echo ***** Neither the project Python nor uv was found. Run 'uv sync --frozen' first. *****
        pause
        exit /b 1
    )
    uv run python -m webui_htmx %*
)
pause
