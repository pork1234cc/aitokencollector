@echo off
chcp 65001 >nul
start "" pythonw "%~dp0token_stats.py"
