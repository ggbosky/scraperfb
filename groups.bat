@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
rem Doplneni sledovanych skupin (napr. po schvaleni zadosti o clenstvi)
.venv\Scripts\python setup_wizard.py --groups
pause
