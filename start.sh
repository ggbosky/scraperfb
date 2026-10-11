#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
.venv/bin/streamlit run app.py --server.headless true &
DASHBOARD=$!
trap 'kill $DASHBOARD 2>/dev/null' EXIT
echo "Dashboard: http://localhost:8501  (Ctrl+C ukončí worker i dashboard)"
.venv/bin/python main.py
