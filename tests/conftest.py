import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    from config import load_settings
    return load_settings()


@pytest.fixture
def db(settings):
    from database import Database
    database = Database(settings.db_path)
    database.seed_defaults()
    return database
