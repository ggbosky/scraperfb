import dataclasses
import json

import httpx

import analyzer
from analyzer import LLMClassifier
from notifier import Notifier

LLM_JSON = {"intent": "poptavajici", "is_active": True, "relevance_score": 9, "project_type": "eshop",
            "platform": "Shoptet", "budget": "30 tis.", "deadline": None, "urgency": "stredni",
            "location": None, "summary": "E-shop na Shoptetu", "reasoning": "Jasná poptávka."}


def test_openai_compatible_provider_with_retry(settings, monkeypatch):
    monkeypatch.setattr(analyzer.time, "sleep", lambda s: None)
    s = dataclasses.replace(settings, llm_provider="groq", llm_api_key="KEY",
                            llm_base_url="https://api.groq.com/openai/v1")
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "1"})
        body = json.loads(req.content)
        assert body["response_format"] == {"type": "json_object"}
        assert body["messages"][0]["role"] == "system"
        assert "Shoptet" in body["messages"][1]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(LLM_JSON)}}]})

    clf = LLMClassifier(s)
    clf._client = httpx.Client(transport=httpx.MockTransport(handler))
    out = clf.classify("Poptávám e-shop na Shoptetu, rozpočet 30 tis.")
    assert out["intent"] == "poptavajici" and out["relevance_score"] == 9
    assert calls[-1].headers["authorization"] == "Bearer KEY"
    assert str(calls[-1].url) == "https://api.groq.com/openai/v1/chat/completions"


def test_ollama_provider_and_json_repair(settings, monkeypatch):
    monkeypatch.setattr(analyzer.time, "sleep", lambda s: None)
    s = dataclasses.replace(settings, llm_provider="ollama", llm_base_url="http://localhost:11434")
    replies = iter(["Jasně! Tady je analýza bez JSONu.", json.dumps(LLM_JSON)])

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/chat"
        assert json.loads(req.content)["format"] == "json"
        return httpx.Response(200, json={"message": {"content": next(replies)}})

    clf = LLMClassifier(s)
    clf._client = httpx.Client(transport=httpx.MockTransport(handler))
    assert clf.classify("Sháním webaře")["platform"] == "Shoptet"


def test_telegram_message_is_escaped(settings):
    s = dataclasses.replace(settings, telegram_bot_token="T", telegram_chat_id="42")
    sent = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})

    n = Notifier(s)
    n._client = httpx.Client(transport=httpx.MockTransport(handler))
    ok = n.send_lead({"id": 1, "score": 10, "source_name": "Skupina <A&B>", "author": "Jana",
                      "posted_at": "2026-10-11T10:00:00+00:00", "summary": "Web <b>",
                      "text": "Poptávám web pro <firmu> & spol.", "url": "https://www.facebook.com/groups/1/posts/2/",
                      "budget": "20k", "urgency": "vysoka"})
    assert ok
    msg = sent[0]
    assert msg["chat_id"] == "42" and msg["parse_mode"] == "HTML"
    assert "Skupina &lt;A&amp;B&gt;" in msg["text"] and "&lt;firmu&gt;" in msg["text"]
    assert msg["reply_markup"]["inline_keyboard"][0][0]["url"].endswith("/posts/2/")
