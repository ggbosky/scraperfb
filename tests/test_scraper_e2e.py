"""End-to-end test scraperu proti lokální napodobenině FB stránky.

Ověřuje skutečný běh Playwrightu: stealth, odchyt GraphQL odpovědí při
skrolování, čtení SSR JSONu, DOM fallback a slučování duplicit.
"""
import asyncio
import dataclasses
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import scraper
from scraper import FacebookScraper


def _story(pid: str, text: str, ts: int) -> dict:
    return {
        "__typename": "Story", "post_id": pid,
        "comet_sections": {
            "content": {"story": {"actors": [{"name": f"Autor {pid}", "id": f"9{pid}",
                                                "url": f"https://www.facebook.com/groups/9/user/9{pid}/"}],
                                    "message": {"text": text}}},
            "context_layout": {"story": {"comet_sections": {"metadata": [{"story": {
                "creation_time": ts, "url": f"https://www.facebook.com/groups/9/posts/{pid}/"}}]}}},
        },
    }


SSR = json.dumps({"require": [["ScheduledServerJS", {"__bbox": {"result": {"data": {
    "node": _story("100", "SSR: poptávám tvorbu webu pro pekárnu", 1_760_000_000)}}}}]]})

PAGE = f"""<!doctype html><html><body>
<script type="application/json">{SSR}</script>
<div role="feed">
  <div><div data-ad-rendering-role="story_message">SSR: poptávám tvorbu webu pro pekárnu</div>
       <a href="/groups/9/posts/100/">2 h</a></div>
  <div><h3><a href="https://www.facebook.com/petr.dom?__cft__[0]=x">Petr Dom</a></h3>
       <div data-ad-rendering-role="story_message">Jen v DOM: sháním webaře na Wix, kdo pomůže?</div>
       <a href="/groups/9/posts/999/?__cft__[0]=x" aria-label="3 h">3 h</a>
       <div role="button" onclick="this.parentElement.querySelector('[data-ad-rendering-role]').innerText += ' (celý text)'">Zobrazit víc</div>
  </div>
</div>
<div style="height:6000px"></div>
<script>
  let n = 0, busy = false;
  window.addEventListener('scroll', () => {{
    if (busy) return; busy = true; n++;
    fetch('/api/graphql/', {{method: 'POST', body: 'n=' + n}}).finally(() => setTimeout(() => busy = false, 200));
  }});
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        Handler.calls += 1
        k = Handler.calls
        lines = [
            json.dumps({"data": {"node": {"edges": [{"node": _story(f"{200 + k}", f"GraphQL příspěvek {k}: hledám webaře", 1_760_000_000 + k)}]}}}),
            json.dumps({"label": "stream", "data": {"node": _story("100", "SSR: poptávám tvorbu webu pro pekárnu", 1_760_000_000)}}),
        ]
        body = ("for (;;);" + "\n".join(lines)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


def test_scrape_source_end_to_end(settings, server, monkeypatch):
    async def fast_sleep(a, b):
        await asyncio.sleep(0.05)

    monkeypatch.setattr(scraper, "human_sleep", fast_sleep)
    settings.storage_state_path.write_text('{"cookies": [], "origins": []}')
    s = dataclasses.replace(settings, scrolls_per_source_min=3, scrolls_per_source_max=3, headless=True)

    async def run():
        fb = FacebookScraper(s)
        try:
            await fb.start()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium nelze spustit: {exc}")
        try:
            webdriver = await fb.page.evaluate("() => navigator.webdriver")
            posts = await fb.scrape_source(server, "search", "Test")
            return webdriver, posts
        finally:
            await fb.close()

    webdriver, posts = asyncio.run(run())
    by_key = {p.post_key: p for p in posts}

    assert not webdriver  # stealth skryl navigator.webdriver
    assert "fb:100" in by_key and by_key["fb:100"].posted_at is not None  # SSR + GraphQL sloučeno
    assert by_key["fb:100"].author == "Autor 100"
    assert by_key["fb:100"].author_id == "9100"
    assert by_key["fb:100"].author_url == "https://www.facebook.com/profile.php?id=9100"
    assert any(k.startswith("fb:20") for k in by_key)  # odchycené GraphQL odpovědi
    dom = by_key["fb:999"]  # jen v DOM
    assert dom.origin == "dom"
    assert dom.url == "https://www.facebook.com/groups/9/posts/999/"
    assert dom.author == "Petr Dom"
    assert dom.author_url == "https://www.facebook.com/petr.dom/"
    assert "(celý text)" in dom.text  # "Zobrazit víc" bylo rozbaleno
    assert dom.posted_at is not None  # čas z aria-label "3 h"
    assert len([p for p in posts if "pekárnu" in p.text]) == 1  # žádné duplicity
