@echo off
chcp 65001 >nul
call .venv\Scripts\activate.bat
python scraper.py login
pause
