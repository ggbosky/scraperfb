#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "=== FB Lead Hunter – instalace ==="
command -v python3 >/dev/null || { echo "Chybí Python 3.10+ (https://www.python.org/downloads/)"; exit 1; }
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
[ -f .env ] || cp .env.example .env
echo
echo "Hotovo. Další kroky:"
echo "  1. Vyplň v souboru .env: LLM_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID"
echo "  2. ./.venv/bin/python scraper.py login   (přihlášení na Facebook)"
echo "  3. ./start.sh"
