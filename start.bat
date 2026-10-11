@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
title FB Lead Hunter - worker (nezavirej)
if not exist .venv\Scripts\python.exe (
  echo Aplikace jeste neni nainstalovana - spust nejdriv setup.bat
  pause
  exit /b 1
)
start "FB Lead Hunter - dashboard" /min .venv\Scripts\python -m streamlit run app.py
timeout /t 6 /nobreak >nul
start "" http://localhost:8501
echo.
echo  Dashboard se za chvili otevre v prohlizeci: http://localhost:8501
echo  Toto okno hleda poptavky - NEZAVIREJ ho (muzes ho minimalizovat).
echo.
.venv\Scripts\python main.py
pause
