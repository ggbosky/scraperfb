@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
title FB Lead Hunter - nastaveni
echo.
echo  =====================================================
echo    FB Lead Hunter - instalace a nastaveni
echo  =====================================================
echo.

rem --- 0) Jsme ve slozce s aplikaci? -------------------------------------
if not exist requirements.txt (
  echo  Vedle setup.bat chybi soubory aplikace.
  echo  Nejspis ho spoustis primo ze ZIP souboru - Windows pak rozbali jen tento jeden soubor.
  echo.
  echo  Co udelat:
  echo    1. Zavri toto okno.
  echo    2. Na stazeny ZIP klikni pravym tlacitkem a zvol "Extrahovat vse..." - potvrd Extrahovat.
  echo    3. Otevri rozbalenou slozku scraperfb-claude-kind-sagan-gxqs98
  echo       a teprve tam dvakrat klikni na setup.bat
  echo.
  echo  Aktualni slozka: %~dp0
  pause
  exit /b 1
)

rem --- 1) Python ---------------------------------------------------------
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=python"
if not defined PY (
  echo  Python 3.10+ neni nainstalovany. Zkusim ho nainstalovat automaticky...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  if errorlevel 1 (
    echo.
    echo  Automaticka instalace se nepovedla.
    echo  Stahni Python z https://www.python.org/downloads/ a pri instalaci zaskrtni
    echo  "Add python.exe to PATH". Pak spust setup.bat znovu.
    start https://www.python.org/downloads/
    pause
    exit /b 1
  )
  echo.
  echo  Python je nainstalovany. ZAVRI toto okno a spust setup.bat znovu.
  pause
  exit /b 0
)
echo  [ok] Python nalezen

rem --- 2) Knihovny a prohlizec -----------------------------------------
if not exist .venv\Scripts\python.exe (
  echo  Vytvarim prostredi aplikace...
  %PY% -m venv .venv || (echo  Nepodarilo se vytvorit .venv & pause & exit /b 1)
)
echo  Instaluji knihovny (prvne to trva par minut)...
.venv\Scripts\python -m pip install --upgrade pip -q
.venv\Scripts\python -m pip install -r requirements.txt -q || (echo  Instalace knihoven selhala - zkontroluj pripojeni k internetu. & pause & exit /b 1)
echo  Stahuji prohlizec pro aplikaci...
.venv\Scripts\python -m playwright install chromium || (echo  Stazeni prohlizece selhalo. & pause & exit /b 1)
if not exist .env copy .env.example .env >nul
echo  [ok] Instalace hotova

rem --- 3) Pruvodce nastavenim ------------------------------------------
.venv\Scripts\python setup_wizard.py
set RC=%errorlevel%
if "%RC%"=="0" (
  call start.bat
  exit /b 0
)
echo.
echo  Aplikaci kdykoli spustis dvojklikem na start.bat
pause
