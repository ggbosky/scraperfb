@echo off
chcp 65001 >nul
echo === FB Lead Hunter - instalace ===
where python >nul 2>nul || (echo Python neni nainstalovany. Stahni ho z https://www.python.org/downloads/ ^(zaskrtni "Add python.exe to PATH"^). & pause & exit /b 1)
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt || (pause & exit /b 1)
playwright install chromium || (pause & exit /b 1)
if not exist .env copy .env.example .env >nul
echo.
echo Hotovo. Dalsi kroky:
echo   1. Otevri soubor .env v Poznamkovem bloku a vypln LLM_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
echo   2. Spust login.bat a prihlas se na Facebook
echo   3. Spust start.bat
pause
