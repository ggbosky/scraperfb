import asyncio
import json
from datetime import datetime, timezone

import httpx

import main
import web_sources
from web_sources import WebSources, parse_freelancer, parse_reddit_atom

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <author><name>/u/bakery_owner</name><uri>https://www.reddit.com/user/bakery_owner</uri></author>
    <category term="forhire" label="r/forhire"/>
    <content type="html">&lt;!-- SC_OFF --&gt;&lt;div class="md"&gt;&lt;p&gt;Need a 5 page site for my bakery in Ohio.&lt;/p&gt;
&lt;p&gt;Budget $1,500 &amp;amp; DM me.&lt;/p&gt;&lt;/div&gt;&lt;!-- SC_ON --&gt; &amp;#32; submitted by &amp;#32;
&lt;a href="https://www.reddit.com/user/bakery_owner"&gt; /u/bakery_owner &lt;/a&gt;</content>
    <id>t3_abc123</id>
    <link href="https://www.reddit.com/r/forhire/comments/abc123/hiring_web_designer/"/>
    <published>2026-10-11T10:00:00+00:00</published>
    <title>[Hiring] Web designer for bakery website</title>
  </entry>
  <entry>
    <author><name>/u/someone</name></author>
    <content type="html">comment</content>
    <id>t1_zzz</id>
    <title>a comment</title>
  </entry>
</feed>"""


def test_parse_reddit_atom():
    posts = parse_reddit_atom(ATOM, "r/forhire")
    assert len(posts) == 1
    p = posts[0]
    assert p.post_key == "reddit:t3_abc123"
    assert p.text.startswith("[Hiring] Web designer for bakery website\n\nNeed a 5 page site")
    assert "Budget $1,500 & DM me." in p.text and "submitted by" not in p.text
    assert p.author == "bakery_owner"
    assert p.message_url == "https://www.reddit.com/message/compose/?to=bakery_owner"
    assert p.posted_at == datetime(2026, 10, 11, 10, 0, tzinfo=timezone.utc)
    assert p.source_type == "reddit" and p.source_name == "r/forhire"


FREELANCER = {"result": {"projects": [
    {"id": 1, "title": "Shopify store redesign", "description": "Redesign our Shopify theme.",
     "seo_url": "shopify/Shopify-store-redesign", "time_submitted": 1791720000, "type": "fixed",
     "budget": {"minimum": 250.0, "maximum": 750.0}, "currency": {"code": "USD", "exchange_rate": 1.0}},
    {"id": 2, "title": "Tiny fix", "description": "Fix a button.", "seo_url": "html/Tiny-fix",
     "time_submitted": 1791720000, "type": "fixed",
     "budget": {"minimum": 10.0, "maximum": 30.0}, "currency": {"code": "USD", "exchange_rate": 1.0}},
    {"id": 3, "title": "WordPress site", "description": "Build site.", "seo_url": "wordpress/WordPress-site",
     "time_submitted": 1791720000, "type": "fixed",
     "budget": {"minimum": 600.0, "maximum": 1500.0}, "currency": {"code": "INR", "exchange_rate": 0.012}},
    {"id": 4, "title": "Ongoing web dev", "description": "Hourly help.", "seo_url": "web/Ongoing-web-dev",
     "time_submitted": 1791720000, "type": "hourly",
     "budget": {"minimum": 15.0, "maximum": 25.0}, "currency": {"code": "USD", "exchange_rate": 1.0}},
]}}


def test_parse_freelancer_filters_cheap_projects():
    posts = parse_freelancer(FREELANCER, min_budget_usd=150, min_hourly_usd=15)
    assert [p.post_key for p in posts] == ["freelancer:1", "freelancer:4"]
    assert [p.post_key for p in parse_freelancer(FREELANCER, 150, min_hourly_usd=30)] == ["freelancer:1"]
    p = posts[0]
    assert p.url == "https://www.freelancer.com/projects/shopify/Shopify-store-redesign"
    assert p.message_url == p.url and p.source_type == "freelancer"
    assert p.text.endswith("Rozpočet: 250–750 USD")
    assert posts[1].text.endswith("Rozpočet: 15–25 USD / hod")


def test_reddit_stops_on_rate_limit(settings, monkeypatch):
    monkeypatch.setattr(web_sources.time, "sleep", lambda s: None)
    calls = []

    def handler(req):
        calls.append(str(req.url))
        if len(calls) == 1:
            return httpx.Response(200, text=ATOM)
        return httpx.Response(429)

    web = WebSources(settings)
    web.client = httpx.Client(transport=httpx.MockTransport(handler))
    results = web.reddit(["forhire", "hiring", "smallbusiness"], ["looking for web designer"])
    assert [name for name, _ in results] == ["r/forhire"]
    assert len(calls) == 2  # po 429 už další dotazy neposílá
    assert calls[0].startswith("https://www.reddit.com/r/forhire/new/.rss")


def test_freelancer_request_uses_web_categories(settings):
    seen = {}

    def handler(req):
        seen["url"] = req.url
        return httpx.Response(200, content=json.dumps(FREELANCER))

    web = WebSources(settings)
    web.client = httpx.Client(transport=httpx.MockTransport(handler))
    [(name, posts)] = web.freelancer()
    assert name == "Freelancer.com" and len(posts) == 2
    jobs = seen["url"].params.get_list("jobs[]")
    assert "17" in jobs and "502" in jobs  # Website Design, Shopify


class FakeClassifier:
    model = "fake"

    def classify(self, text, **kw):
        from analyzer import normalize_analysis
        return normalize_analysis({"intent": "poptavajici", "is_active": True, "relevance_score": 10,
                                   "project_type": "novy_web", "summary": "Web pro pekárnu"})

    def close(self):
        pass


class FakeWeb:
    def __init__(self, settings):
        self.errors = {}

    def freelancer(self):
        return []

    def reddit(self, subreddits, queries):
        post = parse_reddit_atom(ATOM, "r/forhire")[0]
        post.posted_at = datetime.now(timezone.utc)
        return [("r/forhire", [post])]

    def close(self):
        pass


def _worker(settings, monkeypatch):
    monkeypatch.setattr(main, "WebSources", FakeWeb)
    w = main.Worker(settings)
    w.pipeline.classifier = FakeClassifier()
    return w


def test_cycle_without_facebook_login_still_finds_reddit_leads(settings, monkeypatch):
    w = _worker(settings, monkeypatch)
    assert not settings.storage_state_path.exists()
    asyncio.run(w.run_cycle())
    leads = w.db.get_leads()
    assert [l["post_key"] for l in leads] == ["reddit:t3_abc123"]
    assert leads[0]["message_url"].endswith("to=bakery_owner")
    assert w.db.get_kv("paused") is None
    status = json.loads(w.db.get_kv("source_status"))
    assert status["Reddit"]["state"] == "ok" and status["Reddit"]["leads"] == 1
    assert status["Freelancer.com"]["state"] == "ok"
    assert status["Facebook"]["state"] == "off" and "login.bat" in status["Facebook"]["note"]


def test_off_hours_cycle_reads_web_sources_but_skips_facebook(settings, monkeypatch):
    class MustNotRun:
        def __init__(self, *a, **kw):
            raise AssertionError("Facebook nemá mimo aktivní hodiny běžet")

    settings.storage_state_path.write_text('{"cookies": [], "origins": []}')
    monkeypatch.setattr(main, "FacebookScraper", MustNotRun)
    w = _worker(settings, monkeypatch)
    asyncio.run(w.run_cycle(facebook=False))
    assert len(w.db.get_leads()) == 1
    assert json.loads(w.db.get_kv("source_status"))["Facebook"]["state"] == "waiting"


def test_expired_facebook_session_pauses_only_facebook(settings, monkeypatch):
    class ExpiredScraper:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            raise main.SessionExpiredError("přesměrováno na login")

        async def __aexit__(self, *exc):
            pass

    settings.storage_state_path.write_text('{"cookies": [], "origins": []}')
    monkeypatch.setattr(main, "FacebookScraper", ExpiredScraper)
    w = _worker(settings, monkeypatch)
    asyncio.run(w.run_cycle())
    assert w.db.get_kv("fb_paused") == "1" and w.db.get_kv("paused") is None
    assert len(w.db.get_leads()) == 1  # Reddit lead přesto uložen
    asyncio.run(w.run_cycle())  # další cyklus Facebook přeskočí, ale nespadne
    assert w.db.stats()["total_posts"] == 1
