import pytest

import setup_wizard as wiz


def test_update_env_replaces_uncomments_and_appends(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# LLM\nLLM_PROVIDER=groq\n# LLM_BASE_URL=http://localhost:11434\n# Prázdné = výchozí\n")
    monkeypatch.setattr(wiz.os, "environ", {})
    wiz.update_env(env, {"LLM_PROVIDER": "gemini", "LLM_BASE_URL": "https://x", "LLM_API_KEY": "AIza1"})
    assert env.read_text().splitlines() == [
        "# LLM", "LLM_PROVIDER=gemini", "LLM_BASE_URL=https://x", "# Prázdné = výchozí", "LLM_API_KEY=AIza1"]
    assert wiz.read_env(env)["LLM_API_KEY"] == "AIza1"


def test_parse_selection():
    assert wiz.parse_selection("1, 3,5-7", 8) == [0, 2, 4, 5, 6]
    assert wiz.parse_selection("v", 3) == [0, 1, 2]
    assert wiz.parse_selection("2 2 1", 3) == [1, 0]
    with pytest.raises(ValueError):
        wiz.parse_selection("9", 3)
    with pytest.raises(ValueError):
        wiz.parse_selection("abc", 3)


def test_pick_gemini_models_prefers_newest_stable_flash():
    gen = ["generateContent"]
    models = [
        {"name": "models/gemini-2.0-flash", "supportedGenerationMethods": gen},
        {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": gen},
        {"name": "models/gemini-2.5-flash-lite", "supportedGenerationMethods": gen},
        {"name": "models/gemini-3.0-flash-preview", "supportedGenerationMethods": gen},
        {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": gen},
        {"name": "models/gemini-2.0-flash-001", "supportedGenerationMethods": gen},
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
    ]
    assert wiz.pick_gemini_models(models) == ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-001"]


def _answers(monkeypatch, *answers):
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))
    monkeypatch.setattr(wiz.webbrowser, "open", lambda url: True)


def test_step_llm_retries_bad_key_then_saves_working_model(tmp_path, monkeypatch, settings):
    from analyzer import LLMError
    env = tmp_path / ".env"
    env.write_text("LLM_PROVIDER=groq\n")
    monkeypatch.setattr(wiz, "ENV_PATH", env)

    def fake_list(key):
        if key == "bad":
            raise ValueError("Google klíč odmítl.")
        return ["gemini-9.0-flash", "gemini-2.5-flash"]

    def fake_test(key, model):
        if model == "gemini-9.0-flash":
            raise LLMError("HTTP 429 quota")
        return {"intent": "poptavajici", "relevance_score": 10, "summary": "Web pro pekárnu."}

    monkeypatch.setattr(wiz, "_list_gemini_models", fake_list)
    monkeypatch.setattr(wiz, "_test_model", fake_test)
    _answers(monkeypatch, "bad", "AIzaGOOD")
    wiz.step_llm()
    env_values = wiz.read_env(env)
    assert env_values["LLM_PROVIDER"] == "gemini" and env_values["LLM_API_KEY"] == "AIzaGOOD"
    assert env_values["LLM_MODEL"] == "gemini-2.5-flash"


def test_step_groups_adds_selected_and_manual_groups(monkeypatch, settings):
    import scraper
    from database import Database

    class FakeScraper:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def joined_groups(self):
            return [{"name": "Small Business USA", "url": "https://www.facebook.com/groups/sb/"},
                    {"name": "Web Designers Club", "url": "https://www.facebook.com/groups/wd/"},
                    {"name": "Shopify Owners", "url": "https://www.facebook.com/groups/shop/"}]

    monkeypatch.setattr(scraper, "FacebookScraper", FakeScraper)
    _answers(monkeypatch, "n", "1,3", "https://www.facebook.com/groups/realtors/", "")
    wiz.step_groups("international")
    db = Database(settings.db_path)
    assert sorted(s["url"] for s in db.list_sources()) == [
        "https://www.facebook.com/groups/realtors/", "https://www.facebook.com/groups/sb/",
        "https://www.facebook.com/groups/shop/"]
    assert db.get_keywords("search")[0] == "looking for web designer"
