@echo off
chcp 65001 >nul
call .venv\Scripts\activate.bat
start "FB Lead Hunter - dashboard" cmd /k streamlit run app.py
echo Dashboard se otevre v prohlizeci (http://localhost:8501). Worker bezi v tomto okne - nezavirej ho.
python main.py
pause
