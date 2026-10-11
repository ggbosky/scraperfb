"""Zdroje poptávek mimo Facebook – veřejné, bez přihlášení, bez rizika pro FB účet.

- Reddit: nové příspěvky ve vybraných subredditech (r/forhire, r/smallbusiness…) a
  vyhledávání napříč Redditem ("looking for web designer"). Čte se přes veřejné RSS.
- Freelancer.com: nové zakázky v kategoriích webdesign/vývoj/WordPress přes veřejné API,
  s filtrem minimálního rozpočtu.

Výstupem jsou stejné `ScrapedPost` objekty jako u Facebooku, takže prochází stejnou
filtrací (předfiltr → AI → rozhodnutí) a končí ve stejném dashboardu.
"""
from __future__ import annotations

import html
import logging
import random
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from config import Settings
from scraper import ScrapedPost

log = logging.getLogger(__name__)

USER_AGENT = "windows:fb-lead-hunter:1.1 (personal lead finder; low volume)"
ATOM = {"a": "http://www.w3.org/2005/Atom"}

# Kategorie Freelancer.com (ověřeno přes /jobs API): Website Design, Web Development, WordPress,
# Website Development, Web Design, HTML, eCommerce, Landing Pages, Shopify, WooCommerce, Wix,
# Squarespace, Webflow, Shopify Development
FREELANCER_JOB_IDS = [17, 1031, 69, 2839, 2701, 335, 137, 482, 502, 788, 873, 875, 1595, 1686]


def _strip_html(fragment: str) -> str:
    text = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", fragment, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Reddit (RSS / Atom)
# --------------------------------------------------------------------------
def parse_reddit_atom(xml_text: str, source_name: str) -> list[ScrapedPost]:
    root = ET.fromstring(xml_text)
    posts = []
    for entry in root.findall("a:entry", ATOM):
        post_id = (entry.findtext("a:id", "", ATOM) or "").strip()
        if not post_id.startswith("t3_"):  # t3_ = příspěvek (komentáře mají t1_)
            continue
        title = (entry.findtext("a:title", "", ATOM) or "").strip()
        content = entry.findtext("a:content", "", ATOM) or ""
        body_match = re.search(r'<div class="md">(.*?)</div>', content, flags=re.S)
        body = _strip_html(body_match.group(1) if body_match else content.split("submitted by")[0])
        author = (entry.findtext("a:author/a:name", "", ATOM) or "").strip().removeprefix("/u/") or None
        link = entry.find("a:link", ATOM)
        url = link.get("href") if link is not None else None
        posts.append(ScrapedPost(
            text=f"{title}\n\n{body}".strip(),
            url=url,
            post_id=post_id,
            platform="reddit",
            author=author,
            author_url=f"https://www.reddit.com/user/{author}/" if author else None,
            message_url=f"https://www.reddit.com/message/compose/?to={quote(author)}" if author else None,
            posted_at=_parse_dt(entry.findtext("a:published", None, ATOM)
                                or entry.findtext("a:updated", None, ATOM)),
            source_type="reddit",
            source_name=source_name,
            source_url=url or "",
            origin="rss",
        ))
    return posts


# --------------------------------------------------------------------------
# Freelancer.com (veřejné API)
# --------------------------------------------------------------------------
def parse_freelancer(data: dict[str, Any], min_budget_usd: float, min_hourly_usd: float = 0) -> list[ScrapedPost]:
    posts = []
    for p in data.get("result", {}).get("projects", []):
        budget = p.get("budget") or {}
        currency = p.get("currency") or {}
        rate = float(currency.get("exchange_rate") or 1.0)
        maximum = budget.get("maximum") or budget.get("minimum") or 0
        hourly = p.get("type") == "hourly"
        if float(maximum) * rate < (min_hourly_usd if hourly else min_budget_usd):
            continue
        code = currency.get("code", "")
        span = f"{budget.get('minimum', 0):g}–{float(maximum):g} {code}" + (" / hod" if hourly else "")
        description = p.get("description") or p.get("preview_description") or ""
        url = f"https://www.freelancer.com/projects/{p['seo_url']}" if p.get("seo_url") else None
        ts = p.get("time_submitted")
        posts.append(ScrapedPost(
            text=f"{p.get('title', '').strip()}\n\n{description.strip()}\n\nRozpočet: {span}",
            url=url,
            post_id=str(p.get("id")),
            platform="freelancer",
            author=None,
            message_url=url,  # kontakt = podat nabídku (bid) na stránce zakázky
            posted_at=datetime.fromtimestamp(int(ts), timezone.utc) if ts else None,
            source_type="freelancer",
            source_name="Freelancer.com",
            source_url=url or "",
            origin="api",
        ))
    return posts


# --------------------------------------------------------------------------
# Stahování
# --------------------------------------------------------------------------
class WebSources:
    def __init__(self, settings: Settings):
        self.s = settings
        self.client = httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        self.errors: dict[str, str] = {}  # poslední chyba podle zdroje ("Reddit", "Freelancer.com")

    def close(self) -> None:
        self.client.close()

    def _get(self, url: str, **params: Any) -> httpx.Response:
        resp = self.client.get(url, params=params or None)
        resp.raise_for_status()
        return resp

    def reddit(self, subreddits: list[str], queries: list[str]) -> list[tuple[str, list[ScrapedPost]]]:
        """Vrací [(název zdroje, příspěvky)]. Při omezení (HTTP 429) Reddit pro tento cyklus vynechá."""
        targets = [(f"r/{s}", f"https://www.reddit.com/r/{s}/new/.rss", {"limit": 50}) for s in subreddits]
        targets += [(f"Reddit: {q}", "https://www.reddit.com/search.rss",
                     {"q": q, "sort": "new", "t": "week", "limit": 50}) for q in queries]
        results = []
        for i, (name, url, params) in enumerate(targets):
            if i:
                time.sleep(random.uniform(2.5, 5.0))  # slušné tempo, ať Reddit neomezuje
            try:
                results.append((name, parse_reddit_atom(self._get(url, **params).text, name)))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    log.warning("Reddit dočasně omezil dotazy – zbytek Redditu přeskakuji do dalšího cyklu")
                    self.errors["Reddit"] = "Reddit dočasně omezil dotazy (429), zkusí se znovu v dalším kole"
                    break
                log.warning("Reddit %s: HTTP %s", name, exc.response.status_code)
                self.errors["Reddit"] = f"{name}: HTTP {exc.response.status_code}"
            except (httpx.HTTPError, ET.ParseError) as exc:
                log.warning("Reddit %s: %s", name, exc)
                self.errors["Reddit"] = f"{name}: {exc}"
        return results

    def freelancer(self) -> list[tuple[str, list[ScrapedPost]]]:
        params: dict[str, Any] = {"jobs[]": FREELANCER_JOB_IDS, "limit": 100, "full_description": "true",
                                  "sort_field": "time_submitted"}
        try:
            resp = self.client.get("https://www.freelancer.com/api/projects/0.1/projects/active/", params=params)
            resp.raise_for_status()
            return [("Freelancer.com", parse_freelancer(resp.json(), self.s.freelancer_min_budget_usd,
                                                        self.s.freelancer_min_hourly_usd))]
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Freelancer.com: %s", exc)
            self.errors["Freelancer.com"] = str(exc)[:200]
            return []
