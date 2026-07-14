@echo off
chcp 65001 >nul
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0token_stats.py"
) else (
    start "" pythonw "%~dp0token_stats.py"
)
