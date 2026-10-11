"""Facebook scraper postavený na Playwright + stealth.

Jak získává data (od nejspolehlivějšího):
  1. GraphQL odpovědi – při skrolování si FB dotahuje příspěvky přes
     `/api/graphql/`. Odposloucháváme je a čteme z nich přesný text,
     post_id, URL, autora a hlavně `creation_time` (UNIX čas → spolehlivé
     odfiltrování starých příspěvků).
  2. SSR data – první příspěvky jsou vložené přímo v HTML ve
     `<script type="application/json">`.
  3. DOM fallback – když se změní struktura JSONu, čte se viditelný text.

Ochrana účtu: uložená session (cookies), konzistentní otisk prohlížeče,
lidské skrolování a pauzy, limity načtení stránek za hodinu/den, včasné
zastavení skrolování (známé/staré příspěvky) a okamžité zastavení při
checkpointu nebo dočasném bloku.

CLI:
  python scraper.py login                      # ruční přihlášení v okně prohlížeče → uloží session
  python scraper.py import-cookies cookies.json  # import cookies exportovaných z prohlížeče
  python scraper.py check                      # ověří, že je session platná
  python scraper.py groups                     # vypíše skupiny, ve kterých je účet členem
  python scraper.py test <url_skupiny>         # stáhne jednu skupinu a vypíše příspěvky (bez DB)
  python scraper.py test "hledám webaře" --search
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import random
import re
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import parse_qs, quote, urlencode, urlparse
from zoneinfo import ZoneInfo

from playwright.async_api import (
    BrowserContext,
    Page,
    Response,
    Route,
    async_playwright,
)

from analyzer import content_hash, normalize
from config import Settings, load_settings
from database import Database, utcnow

log = logging.getLogger(__name__)

FB_HOME = "https://www.facebook.com/"


# ==========================================================================
# Výjimky
# ==========================================================================
class ScraperError(RuntimeError):
    pass


class SessionExpiredError(ScraperError):
    """Cookies nejsou platné – je potřeba se znovu přihlásit."""


class CheckpointError(ScraperError):
    """FB chce ověření (checkpoint) – nutný ruční zásah v prohlížeči."""


class BlockedError(ScraperError):
    """FB dočasně omezil účet ("Dočasně jste zablokováni")."""


class RateLimitReached(ScraperError):
    """Vyčerpán vlastní limit načtení stránek za hodinu/den."""


# ==========================================================================
# Datový model
# ==========================================================================
@dataclass
class ScrapedPost:
    text: str
    url: str | None = None
    post_id: str | None = None
    author: str | None = None
    author_url: str | None = None
    author_id: str | None = None
    posted_at: datetime | None = None
    source_type: str = "group"
    source_name: str = ""
    source_url: str = ""
    origin: str = "dom"  # graphql | ssr | dom

    @property
    def post_key(self) -> str:
        if self.post_id:
            return f"fb:{self.post_id}"
        if self.url:
            return f"url:{self.url}"
        return f"hash:{content_hash((self.author or '') + ' ' + self.text)}"

    @property
    def content_hash(self) -> str:
        return content_hash(self.text)


# ==========================================================================
# URL pomocníci
# ==========================================================================
_POST_ID_PATTERNS = [
    re.compile(r"/groups/[^/?#]+/(?:posts|permalink)/(\d+)"),
    re.compile(r"[?&]story_fbid=(pfbid\w+|\d+)"),
    re.compile(r"[?&]multi_permalinks=(\d+)"),
    re.compile(r"/posts/(?:[^/?#]+/)?(pfbid\w+|\d+)"),
    re.compile(r"/permalink/(\d+)"),
]
_POST_URL_RE = re.compile(
    r"/groups/[^/?#]+/(?:posts|permalink)/\d+|story_fbid=|multi_permalinks=|/posts/(?:pfbid|\d)|/permalink/\d+"
)


def extract_post_id(url: str | None) -> str | None:
    if not url:
        return None
    for rx in _POST_ID_PATTERNS:
        m = rx.search(url)
        if m:
            return m.group(1)
    return None


def canonical_post_url(url: str | None) -> str | None:
    """Odstraní sledovací parametry (__cft__, __tn__, ref…) a udělá absolutní URL."""
    if not url:
        return None
    if url.startswith("/"):
        url = "https://www.facebook.com" + url
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    keep = {k: v[0] for k, v in query.items() if k in {"story_fbid", "id", "multi_permalinks"}}
    path = parsed.path if parsed.path.endswith("/") or keep else parsed.path + "/"
    # m.facebook.com / web.facebook.com → jednotná desktopová adresa
    return f"https://www.facebook.com{path}" + (f"?{urlencode(keep)}" if keep else "")


_PROFILE_ID_PATTERNS = [
    re.compile(r"/groups/[^/?#]+/user/(\d+)"),
    re.compile(r"profile\.php\?(?:.*&)?id=(\d+)"),
    re.compile(r"facebook\.com/(\d{6,})/?(?:[?#]|$)"),
]


def extract_profile_id(url: str | None) -> str | None:
    """Číselné ID autora z URL profilu (potřeba pro odkaz do Messengeru)."""
    if not url:
        return None
    for rx in _PROFILE_ID_PATTERNS:
        m = rx.search(url)
        if m:
            return m.group(1)
    return None


def canonical_profile_url(url: str | None) -> str | None:
    """Čistý odkaz na profil autora (bez sledovacích parametrů)."""
    if not url or "facebook.com" not in url and not url.startswith("/"):
        return None
    if url.startswith("/"):
        url = "https://www.facebook.com" + url
    pid = extract_profile_id(url)
    if pid:
        return f"https://www.facebook.com/profile.php?id={pid}"
    path = urlparse(url).path.rstrip("/")
    if not path or path.startswith(("/groups", "/hashtag", "/events", "/photo", "/watch", "/stories")):
        return None
    return f"https://www.facebook.com{path}/"


def group_feed_url(url: str) -> str:
    """URL skupiny se seřazením od nejnovějších příspěvků."""
    url = url.strip()
    if not url.startswith("http"):
        url = f"https://www.facebook.com/groups/{url.strip('/')}/"
    parsed = urlparse(url)
    m = re.match(r"(/groups/[^/?#]+)", parsed.path)
    if not m:
        return url
    return f"https://www.facebook.com{m.group(1)}/?sorting_setting=CHRONOLOGICAL"


def search_posts_url(query: str) -> str:
    """FB Search → Příspěvky, filtr 'Nejnovější příspěvky'."""
    inner = json.dumps({"name": "recent_posts", "args": ""}, separators=(",", ":"))
    filters = base64.b64encode(json.dumps({"recent_posts:0": inner}, separators=(",", ":")).encode()).decode()
    return f"https://www.facebook.com/search/posts/?q={quote(query)}&filters={quote(filters)}"


# ==========================================================================
# Parsování času z DOM ("2 h", "včera v 10:15", "12. října")
# ==========================================================================
_MONTHS = {
    "ledna": 1, "unora": 2, "brezna": 3, "dubna": 4, "kvetna": 5, "cervna": 6, "cervence": 7,
    "srpna": 8, "zari": 9, "rijna": 10, "listopadu": 11, "prosince": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_REL_UNITS = [
    (re.compile(r"^(\d+)\s*(?:s|sec|sek|sekund\w*|seconds?)\b"), "seconds"),
    (re.compile(r"^(\d+)\s*(?:m|min|mins|minut\w*|minutes?)\b"), "minutes"),
    (re.compile(r"^(\d+)\s*(?:h|hod|hodin\w*|hrs?|hours?)\b"), "hours"),
    (re.compile(r"^(\d+)\s*(?:d|dn\w*|den|days?)\b"), "days"),
    (re.compile(r"^(\d+)\s*(?:t|tyd\w*|w|wk|weeks?)\b"), "weeks"),
]


def parse_fb_time(label: str | None, now: datetime | None = None,
                  tz: str = "Europe/Prague") -> datetime | None:
    if not label:
        return None
    now = now or datetime.now(timezone.utc)
    t = normalize(label).replace("pred ", "").strip(" ·.")
    t = re.sub(r"\b(minutami|minutou)\b", "min", t)
    t = re.sub(r"\b(hodinami|hodinou)\b", "h", t)
    t = re.sub(r"\b(dny|dnem)\b", "d", t)
    t = re.sub(r"\b(tydny|tydnem)\b", "t", t)
    if t.startswith(("prave ted", "just now", "nyni", "ted")):
        return now
    for rx, unit in _REL_UNITS:
        m = rx.match(t)
        if m:
            return now - timedelta(**{unit: int(m.group(1))})
    local_now = now.astimezone(ZoneInfo(tz))
    hm = re.search(r"(\d{1,2}):(\d{2})", t)
    if t.startswith(("vcera", "yesterday")):
        d = local_now - timedelta(days=1)
        if hm:
            d = d.replace(hour=int(hm.group(1)), minute=int(hm.group(2)), second=0, microsecond=0)
        return d.astimezone(timezone.utc)
    m = re.search(r"(\d{1,2})\.?\s+([a-z]+)(?:\s+(\d{4}))?", t)
    if m and m.group(2) in _MONTHS:
        year = int(m.group(3)) if m.group(3) else local_now.year
        try:
            d = local_now.replace(year=year, month=_MONTHS[m.group(2)], day=int(m.group(1)),
                                  hour=int(hm.group(1)) if hm else 12,
                                  minute=int(hm.group(2)) if hm else 0, second=0, microsecond=0)
        except ValueError:
            return None
        if d > local_now and not m.group(3):
            d = d.replace(year=year - 1)
        return d.astimezone(timezone.utc)
    return None


# ==========================================================================
# Parsování GraphQL / SSR JSONu
# ==========================================================================
def parse_graphql_payload(text: str) -> list[Any]:
    """FB vrací buď jeden JSON, nebo více JSONů oddělených novým řádkem (streaming)."""
    text = text.strip()
    if text.startswith("for (;;);"):
        text = text[9:]
    try:
        return [json.loads(text)]
    except json.JSONDecodeError:
        pass
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _iter_dicts(obj: Any) -> Iterator[dict]:
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _bfs(root: Any, match: Callable[[str, Any], Any], skip: frozenset[str],
         max_nodes: int = 20000) -> Any:
    """Hledá do šířky (nejmělčí výskyt = vlastní data příspěvku, ne komentáře/sdílení)."""
    queue: deque = deque([root])
    seen = 0
    while queue and seen < max_nodes:
        cur = queue.popleft()
        seen += 1
        if isinstance(cur, dict):
            for k, v in cur.items():
                hit = match(k, v)
                if hit is not None:
                    return hit
            for k, v in cur.items():
                if k not in skip and isinstance(v, (dict, list)):
                    queue.append(v)
        elif isinstance(cur, list):
            queue.extend(x for x in cur if isinstance(x, (dict, list)))
    return None


_SKIP_TEXT = frozenset({"feedback", "comments", "comment_list_renderer", "top_level_comments",
                        "interesting_top_level_comments"})
_SKIP_META = _SKIP_TEXT | {"attached_story", "attachments"}


def _match_message(k: str, v: Any) -> str | None:
    if k == "message" and isinstance(v, dict) and isinstance(v.get("text"), str) and v["text"].strip():
        return v["text"]
    return None


def _match_time(k: str, v: Any) -> int | None:
    if k in ("creation_time", "publish_time") and isinstance(v, int) and v > 1_000_000_000:
        return v
    return None


def _match_url(k: str, v: Any) -> str | None:
    if k in ("url", "permalink_url", "wwwURL") and isinstance(v, str) and _POST_URL_RE.search(v):
        return v
    return None


def _match_author(k: str, v: Any) -> dict | None:
    if k == "actors" and isinstance(v, list) and v and isinstance(v[0], dict):
        actor = v[0]
        name = actor.get("name")
        if not isinstance(name, str):
            return None
        url = actor.get("url") or actor.get("profile_url")
        aid = actor.get("id")
        return {"name": name, "url": url if isinstance(url, str) else None,
                "id": str(aid) if isinstance(aid, (str, int)) and str(aid).isdigit() else None}
    return None


def extract_stories(obj: Any) -> dict[str, dict]:
    """Najde v libovolném FB JSONu uzly typu Story a vytáhne z nich data příspěvku."""
    stories: dict[str, dict] = {}
    for d in _iter_dicts(obj):
        pid = d.get("post_id")
        if not isinstance(pid, (str, int)) or not str(pid).strip():
            continue
        if d.get("__typename") != "Story" and "comet_sections" not in d:
            continue
        pid = str(pid)
        item = {
            "post_id": pid,
            "text": _bfs(d, _match_message, _SKIP_TEXT),
            "creation_time": _bfs(d, _match_time, _SKIP_META),
            "url": _bfs(d, _match_url, _SKIP_META),
            "author": None,
            "author_url": None,
            "author_id": None,
        }
        actor = _bfs(d, _match_author, _SKIP_META)
        if actor:
            item["author"] = actor["name"]
            item["author_url"] = canonical_profile_url(actor["url"]) if actor["url"] else None
            item["author_id"] = actor["id"] or extract_profile_id(actor["url"])
            if not item["author_url"] and item["author_id"]:
                item["author_url"] = f"https://www.facebook.com/profile.php?id={item['author_id']}"
        prev = stories.get(pid)
        if prev:
            for key, value in item.items():
                if prev.get(key) is None and value is not None:
                    prev[key] = value
        else:
            stories[pid] = item
    return stories


# ==========================================================================
# Lidské chování
# ==========================================================================
async def human_sleep(a: float, b: float) -> None:
    await asyncio.sleep(random.uniform(a, b))


async def human_scroll(page: Page, viewport_h: int) -> None:
    """Skrolování kolečkem po malých nerovnoměrných krocích + pauza na "čtení"."""
    vw = page.viewport_size["width"] if page.viewport_size else 1366
    await page.mouse.move(random.randint(int(vw * 0.25), int(vw * 0.7)),
                          random.randint(int(viewport_h * 0.2), int(viewport_h * 0.8)),
                          steps=random.randint(4, 12))
    distance = random.randint(int(viewport_h * 0.7), int(viewport_h * 1.6))
    scrolled = 0
    while scrolled < distance:
        step = random.randint(70, 240)
        await page.mouse.wheel(0, step)
        scrolled += step
        await asyncio.sleep(random.uniform(0.03, 0.16))
    if random.random() < 0.15:  # občas se vrátit kousek zpět, jako když člověk něco dočítá
        await page.mouse.wheel(0, -random.randint(120, 400))
        await human_sleep(0.6, 1.5)
    if random.random() < 0.1:
        await human_sleep(6, 14)  # delší "čtení"
    else:
        await human_sleep(1.8, 5.0)


# ==========================================================================
# DOM extrakce (fallback) – běží v prohlížeči jako jeden JS dotaz
# ==========================================================================
_DOM_EXTRACT_JS = r"""
() => {
  const POST_RE = /\/groups\/[^\/?#]+\/(posts|permalink)\/\d+|story_fbid=|multi_permalinks=|\/posts\/(pfbid|\d)|\/permalink\/\d+/;
  let cands = [...document.querySelectorAll('div[role="feed"] > div')];
  if (cands.length === 0) {
    cands = [...document.querySelectorAll('div[role="article"]')]
      .filter(a => !(a.parentElement && a.parentElement.closest('div[role="article"]')));
  }
  const out = [];
  for (const el of cands) {
    const msg = el.querySelector('[data-ad-rendering-role="story_message"], [data-ad-comet-preview="message"], [data-ad-preview="message"]');
    let text = msg ? msg.innerText : '';
    if (!text) {
      const parts = [];
      for (const b of el.querySelectorAll('div[dir="auto"]')) {
        if (b.closest('[role="article"][aria-label]')) continue;   // komentáře
        if (b.querySelector('div[dir="auto"]')) continue;          // jen listové bloky
        const t = (b.innerText || '').trim();
        if (t.length > 2 && !parts.includes(t)) parts.push(t);
      }
      text = parts.join('\n');
    }
    let url = null, timeLabel = null;
    for (const a of el.querySelectorAll('a[href]')) {
      if (a.closest('[role="article"][aria-label]')) continue;
      if (POST_RE.test(a.href)) {
        url = a.href;
        timeLabel = a.getAttribute('aria-label') || a.innerText || null;
        break;
      }
    }
    const abbr = el.querySelector('abbr[data-utime]');
    const authorEl = el.querySelector('h2 a, h3 a, h4 a, h2 strong, h3 strong, strong a');
    out.push({
      text: (text || '').trim(),
      url,
      timeLabel,
      utime: abbr ? parseInt(abbr.getAttribute('data-utime')) : null,
      author: authorEl ? authorEl.innerText.trim() : null,
      authorHref: authorEl ? (authorEl.closest('a') || authorEl.querySelector('a') || {}).href || null : null,
    });
  }
  return out;
}
"""

_EXPAND_JS = r"""
(labels) => {
  let n = 0;
  for (const b of document.querySelectorAll('div[role="button"]')) {
    const t = (b.innerText || '').trim().toLowerCase();
    if (labels.includes(t) && n < 15) { b.click(); n++; }
  }
  return n;
}
"""

_NOISE_LINES = {
    "líbí se mi", "komentovat", "sdílet", "odeslat", "to se mi líbí", "like", "comment", "share",
    "send", "zobrazit víc", "zobrazit více", "see more", "napsat komentář…", "write a comment…",
    "sponzorováno", "sponsored", "sledovat", "follow", "připojit se", "join", "·",
}

_BLOCK_PHRASES = [
    "dočasně jste zablokováni", "dočasně zablokován", "you're temporarily blocked",
    "you've been temporarily blocked", "tuto funkci teď nemůžete používat",
    "you can't use this feature right now", "your account has been restricted",
    "váš účet je omezený",
]


def clean_dom_text(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln and ln.lower() not in _NOISE_LINES).strip()


# ==========================================================================
# Cookies / session
# ==========================================================================
_SAMESITE = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict"}


def convert_cookies(raw: Any) -> dict:
    """Převede export z Cookie-Editor / EditThisCookie (JSON) na Playwright storage_state."""
    if isinstance(raw, dict) and "cookies" in raw:
        raw = raw["cookies"]  # už je to storage_state nebo podobný formát
    if not isinstance(raw, list):
        raise ValueError("Očekávám JSON pole cookies (export z Cookie-Editor) nebo storage_state.")
    cookies = []
    for c in raw:
        if not c.get("name") or "facebook" not in c.get("domain", ""):
            continue
        secure = bool(c.get("secure", True))
        same_site = _SAMESITE.get(str(c.get("sameSite") or "").lower(), "Lax")
        if same_site == "None" and not secure:
            same_site = "Lax"  # prohlížeč by cookie SameSite=None bez Secure odmítl
        expires = c.get("expires", c.get("expirationDate"))
        if c.get("session") or expires in (None, ""):
            expires = -1
        cookies.append({
            "name": c["name"],
            "value": c.get("value", ""),
            "domain": c["domain"],
            "path": c.get("path", "/"),
            "expires": float(expires),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": secure,
            "sameSite": same_site,
        })
    names = {c["name"] for c in cookies}
    missing = {"c_user", "xs"} - names
    if missing:
        raise ValueError(f"V exportu chybí klíčové FB cookies: {', '.join(sorted(missing))} "
                         "(jsi na facebook.com přihlášený a exportuješ všechny cookies?)")
    return {"cookies": cookies, "origins": []}


def build_user_agent(browser_version: str) -> str:
    """User-Agent odpovídající skutečné verzi prohlížeče a systému (konzistentní otisk)."""
    major = (browser_version or "130").split(".")[0]
    if sys.platform.startswith("win"):
        system = "Windows NT 10.0; Win64; x64"
    elif sys.platform == "darwin":
        system = "Macintosh; Intel Mac OS X 10_15_7"
    else:
        system = "X11; Linux x86_64"
    return (f"Mozilla/5.0 ({system}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Safari/537.36")


_RESERVED_GROUP_SLUGS = {"feed", "discover", "joins", "create", "notifications", "search", "category",
                         "you", "browse", "explore", "learning_content", "suggested"}
_GROUP_LINK_NOISE = {"zobrazit skupinu", "view group", "zobrazit", "join", "připojit se", "join group",
                     "připojit se ke skupině", "visit", "navštívit"}


def parse_group_links(raw: list[dict]) -> list[dict]:
    """Z odkazů na stránce "Vaše skupiny" vybere skupiny (název + URL), bez duplicit."""
    groups: dict[str, dict] = {}
    for item in raw:
        m = re.match(r"^/groups/([^/?#]+)/?$", urlparse(item.get("href") or "").path)
        if not m or m.group(1) in _RESERVED_GROUP_SLUGS:
            continue
        slug = m.group(1)
        lines = [ln.strip() for ln in (item.get("text") or "").splitlines() if ln.strip()]
        name = next((ln for ln in lines if ln.lower() not in _GROUP_LINK_NOISE and len(ln) >= 2), None)
        entry = groups.setdefault(slug, {"name": None, "url": f"https://www.facebook.com/groups/{slug}/"})
        if name and not entry["name"]:
            entry["name"] = name
    return [g for g in groups.values() if g["name"]]


def _platform_for_ua(ua: str) -> str:
    if "Macintosh" in ua:
        return "MacIntel"
    if "Linux" in ua and "Android" not in ua:
        return "Linux x86_64"
    return "Win32"


def _proxy_config(proxy: str) -> dict | None:
    if not proxy:
        return None
    p = urlparse(proxy)
    cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        cfg["username"] = p.username
        cfg["password"] = p.password or ""
    return cfg


async def apply_stealth(context: BrowserContext, settings: Settings, user_agent: str) -> None:
    try:
        from playwright_stealth import Stealth  # playwright-stealth >= 2.0
    except ImportError:
        log.warning("playwright-stealth není nainstalován – běžím bez stealth patchů")
        return
    lang = settings.locale
    stealth = Stealth(
        navigator_languages_override=(lang, lang.split("-")[0]),
        navigator_platform_override=_platform_for_ua(user_agent),
        navigator_user_agent_override=user_agent,
    )
    await stealth.apply_stealth_async(context)


# ==========================================================================
# Scraper
# ==========================================================================
class FacebookScraper:
    def __init__(self, settings: Settings, db: Database | None = None,
                 heartbeat: Callable[[], None] | None = None):
        self.s = settings
        self.db = db
        self.heartbeat = heartbeat or (lambda: None)
        self._pw = None
        self._browser = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self._collecting = False
        self._stories: dict[str, dict] = {}
        self._pending: set[asyncio.Task] = set()
        self._session_ok = True
        self.user_agent = settings.user_agent

    # ---------------------------------------------------------- lifecycle
    async def __aenter__(self) -> "FacebookScraper":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def start(self, headless: bool | None = None, require_session: bool = True) -> None:
        state_path: Path = self.s.storage_state_path
        if require_session and not state_path.exists():
            raise SessionExpiredError(
                f"Chybí uložená session ({state_path}). Spusť `python scraper.py login` "
                "nebo `python scraper.py import-cookies <soubor.json>`.")
        self._pw = await async_playwright().start()
        launch: dict[str, Any] = {
            "headless": self.s.headless if headless is None else headless,
            "args": ["--disable-blink-features=AutomationControlled", "--no-default-browser-check",
                     f"--lang={self.s.locale}"],
        }
        if self.s.browser_executable_path:
            launch["executable_path"] = self.s.browser_executable_path
        elif self.s.browser_channel:
            launch["channel"] = self.s.browser_channel
        if proxy := _proxy_config(self.s.proxy):
            launch["proxy"] = proxy
        self._browser = await self._pw.chromium.launch(**launch)
        self.user_agent = self.s.user_agent or build_user_agent(self._browser.version)
        self._context = await self._browser.new_context(
            storage_state=str(state_path) if state_path.exists() else None,
            user_agent=self.user_agent,
            viewport={"width": self.s.viewport_width, "height": self.s.viewport_height},
            screen={"width": self.s.viewport_width, "height": self.s.viewport_height + 100},
            locale=self.s.locale,
            timezone_id=self.s.timezone,
            color_scheme="light",
            device_scale_factor=1,
            extra_http_headers={"Accept-Language": f"{self.s.locale},{self.s.locale.split('-')[0]};q=0.9,en;q=0.7"},
        )
        await apply_stealth(self._context, self.s, self.user_agent)
        if self.s.block_media:
            await self._context.route("**/*", self._route_filter)
        self._context.on("response", self._on_response)
        self.page = await self._context.new_page()
        self.page.set_default_timeout(45000)

    async def close(self) -> None:
        try:
            if self._pending:
                await asyncio.gather(*self._pending, return_exceptions=True)
            if self._context and self._session_ok:
                # FB průběžně rotuje cookies (xs, fr…) – uložíme čerstvé, ať session vydrží déle.
                await self._context.storage_state(path=str(self.s.storage_state_path))
        except Exception as exc:  # noqa: BLE001
            log.warning("Nepodařilo se uložit session: %s", exc)
        for closer in (self._context, self._browser):
            try:
                if closer:
                    await closer.close()
            except Exception:  # noqa: BLE001
                pass
        if self._pw:
            await self._pw.stop()

    # --------------------------------------------------- network handlers
    @staticmethod
    async def _route_filter(route: Route) -> None:
        if route.request.resource_type == "media":  # videa – zbytečná data navíc
            await route.abort()
        else:
            await route.continue_()

    def _on_response(self, response: Response) -> None:
        if not self._collecting or "/api/graphql" not in response.url:
            return
        task = asyncio.ensure_future(self._consume_graphql(response))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _consume_graphql(self, response: Response) -> None:
        try:
            body = await response.text()
        except Exception:  # noqa: BLE001 – odpověď mohla být mezitím zahozena
            return
        if "post_id" not in body:
            return
        for obj in parse_graphql_payload(body):
            self._merge_stories(extract_stories(obj))

    def _merge_stories(self, new: dict[str, dict]) -> None:
        for pid, item in new.items():
            prev = self._stories.get(pid)
            if prev is None:
                self._stories[pid] = item
            else:
                for k, v in item.items():
                    if prev.get(k) is None and v is not None:
                        prev[k] = v

    async def _collect_ssr(self) -> None:
        """Příspěvky vložené přímo v HTML (první dávka feedu)."""
        assert self.page
        try:
            scripts: list[str] = await self.page.eval_on_selector_all(
                'script[type="application/json"]',
                "els => els.map(e => e.textContent).filter(t => t && t.includes('post_id'))",
            )
        except Exception:  # noqa: BLE001
            return
        for raw in scripts:
            try:
                self._merge_stories(extract_stories(json.loads(raw)))
            except (json.JSONDecodeError, RecursionError):
                continue

    # ----------------------------------------------------- safety checks
    async def _rate_limit_check(self) -> None:
        if self.db is None:
            return
        now = utcnow()
        per_hour = self.db.count_page_loads(now - timedelta(hours=1))
        per_day = self.db.count_page_loads(now - timedelta(days=1))
        if per_hour >= self.s.max_page_loads_per_hour:
            raise RateLimitReached(f"Hodinový limit načtení stránek ({per_hour}) vyčerpán")
        if per_day >= self.s.max_page_loads_per_day:
            raise RateLimitReached(f"Denní limit načtení stránek ({per_day}) vyčerpán")

    async def _check_page_state(self) -> None:
        assert self.page
        url = self.page.url
        if "/checkpoint" in url:
            self._session_ok = False
            raise CheckpointError(f"Facebook vyžaduje ověření účtu (checkpoint): {url}")
        if "/login" in url or "login.php" in url:
            self._session_ok = False
            raise SessionExpiredError("Facebook přesměroval na přihlášení – session vypršela")
        body = (await self.page.evaluate(
            "() => document.body ? document.body.innerText.slice(0, 4000) : ''")).lower()
        if any(p in body for p in _BLOCK_PHRASES):
            raise BlockedError("Facebook hlásí dočasné omezení účtu")
        has_login_form = await self.page.evaluate(
            "() => !!document.querySelector('input[name=\"pass\"]')")
        if has_login_form:
            self._session_ok = False
            raise SessionExpiredError("Na stránce je přihlašovací formulář – session vypršela")

    async def _goto(self, url: str) -> None:
        assert self.page
        await self._rate_limit_check()
        if self.db:
            self.db.record_page_load(url)
        log.info("Otevírám %s", url)
        await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001 – FB nikdy není úplně "idle", to je v pořádku
            pass
        await human_sleep(2.0, 4.5)
        await self._check_page_state()

    async def is_logged_in(self) -> bool:
        assert self._context
        cookies = await self._context.cookies("https://www.facebook.com")
        return any(c["name"] == "c_user" for c in cookies)

    # ------------------------------------------------------------ actions
    async def warm_up(self) -> None:
        """Krátká návštěva hlavního feedu na začátku cyklu – přirozenější vzorec chování."""
        await self._goto(FB_HOME)
        for _ in range(random.randint(1, 3)):
            await human_scroll(self.page, self.s.viewport_height)
        await human_sleep(2, 6)

    async def joined_groups(self, scrolls: int = 6) -> list[dict]:
        """Skupiny, ve kterých je účet členem (stránka "Vaše skupiny")."""
        assert self.page
        await self._goto("https://www.facebook.com/groups/joins/?nav_source=tab")
        for _ in range(scrolls):
            await human_scroll(self.page, self.s.viewport_height)
        raw = await self.page.evaluate(
            "() => [...document.querySelectorAll('a[href*=\"/groups/\"]')]"
            ".map(a => ({href: a.href, text: a.innerText || a.getAttribute('aria-label') || ''}))")
        return parse_group_links(raw)

    async def scrape_source(self, url: str, source_type: str = "group", source_name: str = "",
                            is_known: Callable[[Iterable[str]], set[str]] | None = None,
                            cutoff: datetime | None = None) -> list[ScrapedPost]:
        """Otevře skupinu / vyhledávání, nasbírá příspěvky a vrátí je."""
        assert self.page
        target = group_feed_url(url) if source_type == "group" else url
        self._stories = {}
        self._collecting = True
        try:
            await self._goto(target)
            await self._collect_ssr()
            n_scrolls = random.randint(self.s.scrolls_per_source_min, self.s.scrolls_per_source_max)
            for i in range(n_scrolls):
                self.heartbeat()
                await human_scroll(self.page, self.s.viewport_height)
                if i == 0:
                    await self._check_page_state()
                current = self._build_posts(source_type, source_name, url)
                if is_known:
                    known = is_known(p.post_key for p in current)
                    if len(known) >= self.s.stop_after_known_posts:
                        log.info("  %d již známých příspěvků → končím skrolování", len(known))
                        break
                if cutoff:
                    old = sum(1 for p in current if p.posted_at and p.posted_at < cutoff)
                    if old >= self.s.stop_after_old_posts:
                        log.info("  %d příspěvků starších než limit → končím skrolování", old)
                        break
            if self._pending:
                await asyncio.gather(*list(self._pending), return_exceptions=True)
            dom = await self._extract_dom()
        finally:
            self._collecting = False
        posts = self._build_posts(source_type, source_name, url, dom)
        log.info("  %s: %d příspěvků (%d z GraphQL/SSR)", source_name or url, len(posts),
                 sum(1 for p in posts if p.origin != "dom"))
        return posts

    async def _extract_dom(self) -> list[dict]:
        assert self.page
        try:
            clicked = await self.page.evaluate(
                _EXPAND_JS, ["zobrazit víc", "zobrazit více", "see more"])
            if clicked:
                await human_sleep(0.8, 1.8)
            return await self.page.evaluate(_DOM_EXTRACT_JS)
        except Exception as exc:  # noqa: BLE001
            log.warning("DOM extrakce selhala: %s", exc)
            return []

    def _build_posts(self, source_type: str, source_name: str, source_url: str,
                     dom: list[dict] | None = None) -> list[ScrapedPost]:
        posts: dict[str, ScrapedPost] = {}
        for st in self._stories.values():
            if not st.get("text"):
                continue
            url = canonical_post_url(st.get("url"))
            ts = st.get("creation_time")
            p = ScrapedPost(
                text=st["text"].strip(), url=url, post_id=st["post_id"], author=st.get("author"),
                author_url=st.get("author_url"), author_id=st.get("author_id"),
                posted_at=datetime.fromtimestamp(ts, timezone.utc) if ts else None,
                source_type=source_type, source_name=source_name, source_url=source_url,
                origin="graphql",
            )
            posts[p.post_key] = p

        seen_prefixes = {normalize(p.text)[:120] for p in posts.values()}
        for d in dom or []:
            text = clean_dom_text(d.get("text") or "")
            if len(text) < 15:
                continue
            url = canonical_post_url(d.get("url"))
            pid = extract_post_id(url)
            key = f"fb:{pid}" if pid else None
            if key and key in posts:
                existing = posts[key]
                existing.author = existing.author or d.get("author")
                if not existing.author_url and d.get("authorHref"):
                    existing.author_url = canonical_profile_url(d["authorHref"])
                    existing.author_id = existing.author_id or extract_profile_id(d["authorHref"])
                existing.url = existing.url or url
                continue
            prefix = normalize(text)[:120]
            if prefix in seen_prefixes:
                continue
            seen_prefixes.add(prefix)
            posted_at = (datetime.fromtimestamp(d["utime"], timezone.utc) if d.get("utime")
                         else parse_fb_time(d.get("timeLabel"), tz=self.s.timezone))
            href = d.get("authorHref")
            p = ScrapedPost(text=text, url=url, post_id=pid, author=d.get("author"),
                            author_url=canonical_profile_url(href) if href else None,
                            author_id=extract_profile_id(href),
                            posted_at=posted_at, source_type=source_type,
                            source_name=source_name, source_url=source_url, origin="dom")
            posts[p.post_key] = p
        return list(posts.values())


# ==========================================================================
# CLI
# ==========================================================================
def _clear_session_pause(settings: Settings) -> None:
    """Po novém přihlášení zruší pauzu workeru, kterou způsobila vypršelá session."""
    db = Database(settings.db_path)
    if db.get_kv("paused") == "1" and (db.get_kv("pause_reason") or "").startswith("Session"):
        db.set_kv("paused", None)
        db.set_kv("pause_reason", None)
        print("▶  Pauza workeru zrušena – při příštím cyklu poběží s novou session.")


async def cmd_login(settings: Settings) -> bool:
    scraper = FacebookScraper(settings)
    await scraper.start(headless=False, require_session=False)
    try:
        await scraper.page.goto("https://www.facebook.com/login")
        print("\n➡  V otevřeném okně se přihlas na Facebook (včetně 2FA).")
        print("➡  Až uvidíš svůj feed, vrať se sem a stiskni ENTER.\n")
        await asyncio.to_thread(input, "Stiskni ENTER po přihlášení… ")
        if not await scraper.is_logged_in():
            print("❌ Nevidím přihlašovací cookie c_user – přihlášení se nepovedlo.")
            scraper._session_ok = False
            return False
    finally:
        await scraper.close()
    print(f"✅ Session uložena do {settings.storage_state_path}")
    _clear_session_pause(settings)
    return True


def cmd_import_cookies(settings: Settings, path: str) -> None:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    state = convert_cookies(raw)
    settings.storage_state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"✅ Importováno {len(state['cookies'])} cookies → {settings.storage_state_path}")
    print("   Ověř session příkazem: python scraper.py check")
    _clear_session_pause(settings)


async def cmd_check(settings: Settings) -> bool:
    async with FacebookScraper(settings) as fb:
        await fb._goto(FB_HOME)
        ok = await fb.is_logged_in()
        print("✅ Session je platná, jsi přihlášený." if ok else "❌ Session není platná.")
        return ok


async def cmd_test(settings: Settings, target: str, search: bool) -> None:
    url = search_posts_url(target) if search else target
    async with FacebookScraper(settings) as fb:
        posts = await fb.scrape_source(url, "search" if search else "group", target)
    for p in sorted(posts, key=lambda x: x.posted_at or datetime.min.replace(tzinfo=timezone.utc),
                    reverse=True):
        when = p.posted_at.astimezone(ZoneInfo(settings.timezone)).strftime("%d.%m. %H:%M") if p.posted_at else "??"
        print(f"\n[{when}] [{p.origin}] {p.author or '?'} – {p.url or 'bez URL'}")
        print("   " + p.text[:300].replace("\n", " "))
    print(f"\nCelkem {len(posts)} příspěvků.")


async def cmd_groups(settings: Settings) -> None:
    async with FacebookScraper(settings) as fb:
        groups = await fb.joined_groups()
    for i, g in enumerate(groups, 1):
        print(f"{i:>3}. {g['name']}  {g['url']}")
    print(f"\nCelkem {len(groups)} skupin.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="FB Lead Scraper – nástroje pro session a testování")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="přihlásit se ručně v okně prohlížeče a uložit session")
    imp = sub.add_parser("import-cookies", help="importovat cookies exportované z prohlížeče")
    imp.add_argument("file")
    sub.add_parser("check", help="ověřit platnost session")
    sub.add_parser("groups", help="vypsat skupiny, ve kterých je účet členem")
    test = sub.add_parser("test", help="stáhnout jednu skupinu/hledání a vypsat příspěvky")
    test.add_argument("target", help="URL skupiny, nebo hledaný výraz s --search")
    test.add_argument("--search", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    try:
        if args.cmd == "login":
            asyncio.run(cmd_login(settings))
        elif args.cmd == "import-cookies":
            cmd_import_cookies(settings, args.file)
        elif args.cmd == "check":
            asyncio.run(cmd_check(settings))
        elif args.cmd == "groups":
            asyncio.run(cmd_groups(settings))
        elif args.cmd == "test":
            asyncio.run(cmd_test(settings, args.target, args.search))
    except ScraperError as exc:
        print(f"❌ {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
