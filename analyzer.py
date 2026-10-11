"""Dvoufázová filtrace příspěvků.

Fáze A – `KeywordPrefilter`: rychlý, offline, zdarma. Propustí jen příspěvky,
         které mluví o webech A zároveň obsahují signál poptávky. Zahodí
         zjevné nabídky služeb a spam ještě před voláním LLM (šetří kvótu).
Fáze B – `LLMClassifier`: LLM (Ollama / Groq / OpenAI-kompatibilní API) určí
         záměr, aktivitu, rozpočet, termín a skóre relevance 1–10.

Finální verdikt (`decide`) je deterministický a v kódu – LLM jen dodává data.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from config import SPAM_PATTERNS, STRONG_DEMAND_KEYWORDS, Settings
from prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE

log = logging.getLogger(__name__)

VALID_INTENTS = {"poptavajici", "nabizejici", "diskuse", "spam", "jine"}
VALID_URGENCY = {"vysoka", "stredni", "nizka", "neuvedeno"}

_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_NON_WORD_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------
# Normalizace textu
# --------------------------------------------------------------------------
def strip_diacritics(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def normalize(text: str) -> str:
    """Malá písmena, bez diakritiky, jednotné mezery (pro porovnávání klíčových slov)."""
    return _WS_RE.sub(" ", strip_diacritics(text.lower())).strip()


def content_hash(text: str) -> str:
    """Otisk obsahu nezávislý na diakritice, URL, emoji a interpunkci.

    Slouží k odhalení stejné poptávky vložené do více skupin (cross-post).
    """
    t = normalize(_URL_RE.sub(" ", text))
    t = _WS_RE.sub(" ", _NON_WORD_RE.sub(" ", t)).strip()
    return hashlib.sha1(t[:1000].encode("utf-8")).hexdigest()


def _compile_keywords(keywords: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    """Klíčové slovo = prefix slova (\"web\" chytí \"webovky\", ale ne \"pavučina\")."""
    out = []
    for kw in keywords:
        nkw = normalize(kw)
        if nkw:
            out.append((kw, re.compile(r"(?<!\w)" + re.escape(nkw))))
    return out


# --------------------------------------------------------------------------
# Kontakty uvedené přímo v textu příspěvku
# --------------------------------------------------------------------------
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.I)
# Telefony: mezinárodní (+1, +44…), americký formát (555) 123-4567 a CZ/SK 9 číslic.
# Pořadí je důležité – delší formáty se zkouší první a jejich úseky se už nepřekrývají.
_PHONE_RES = [
    re.compile(r"(?<![\w+])\+\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,4}){2,4}(?!\d)"),
    re.compile(r"(?<![\d+])\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"),
    re.compile(r"(?<![\d+])(?:00\d{3}[\s-]?)?[2-9]\d{2}[\s-]?\d{3}[\s-]?\d{3}(?!\d)"),
]
_WEB_RE = re.compile(
    r"(?:https?://|www\.)[^\s<>()\"']+|(?<![@\w.-])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:cz|sk|com|eu|net|org|shop|store|info)\b(?:/[^\s<>()\"']*)?",
    re.I)


def extract_contacts(text: str) -> dict[str, list[str]]:
    """E-maily, telefony a weby, které autor sám uvedl v příspěvku."""
    emails = list(dict.fromkeys(m.group(0).rstrip(".").lower() for m in _EMAIL_RE.finditer(text)))
    phones: list[str] = []
    taken: list[tuple[int, int]] = []
    for rx in _PHONE_RES:
        for m in rx.finditer(text):
            if any(m.start() < end and start < m.end() for start, end in taken):
                continue
            if 9 <= len(re.sub(r"\D", "", m.group(0))) <= 15:
                taken.append(m.span())
                phones.append(re.sub(r"\s+", " ", m.group(0).strip()))
    websites = []
    for m in _WEB_RE.finditer(_EMAIL_RE.sub(" ", text)):
        w = m.group(0).rstrip(".,;:!?)")
        if "facebook.com" in w.lower() or "fb.com" in w.lower() or "fb.me" in w.lower():
            continue
        websites.append(w)
    return {"emails": emails, "phones": list(dict.fromkeys(phones)),
            "websites": list(dict.fromkeys(websites))}


# --------------------------------------------------------------------------
# Fáze A – klíčový předfiltr
# --------------------------------------------------------------------------
@dataclass
class PrefilterResult:
    passed: bool
    reason: str
    topic_hits: list[str] = field(default_factory=list)
    demand_hits: list[str] = field(default_factory=list)
    negative_hits: list[str] = field(default_factory=list)


_OFFER_TAG_RE = re.compile(r"^\W*[\[(]\s*(?:for hire|offer|offering|available)\s*[\])]")


class KeywordPrefilter:
    def __init__(self, topic: list[str], demand: list[str], negative: list[str],
                 min_length: int = 20, max_length: int = 6000):
        self.topic = _compile_keywords(topic)
        self.demand = _compile_keywords(demand)
        self.negative = _compile_keywords(negative)
        self.strong_demand = _compile_keywords(STRONG_DEMAND_KEYWORDS)
        self.spam = [re.compile(p) for p in SPAM_PATTERNS]
        self.min_length = min_length
        self.max_length = max_length

    @staticmethod
    def _hits(patterns: list[tuple[str, re.Pattern[str]]], text: str) -> list[str]:
        return [kw for kw, rx in patterns if rx.search(text)]

    def check(self, text: str, marketplace: bool = False) -> PrefilterResult:
        """marketplace=True (Freelancer.com): zakázka je poptávka už z principu, kontroluje se jen téma."""
        if len(text.strip()) < self.min_length:
            return PrefilterResult(False, "too_short")
        if len(text) > self.max_length and not marketplace:
            return PrefilterResult(False, "too_long")  # dlouhé "eseje" bývají reklamy/nabídky

        t = normalize(text)
        if any(rx.search(t) for rx in self.spam):
            return PrefilterResult(False, "spam_pattern")
        if _OFFER_TAG_RE.match(t):  # Reddit: "[For Hire] …" je vždy nabídka služeb
            return PrefilterResult(False, "offer_tag")

        topic = self._hits(self.topic, t)
        if not topic:
            return PrefilterResult(False, "no_topic")

        demand = self._hits(self.demand, t)
        negative = self._hits(self.negative, t)
        # Silná poptávka se hledá až v textu bez "nabízecích" frází, jinak by
        # "hledám práci jako webař" prošlo díky slovu "hledám".
        t_without_offers = t
        for _, rx in self.negative:
            t_without_offers = rx.sub(" ", t_without_offers)
        strong = self._hits(self.strong_demand, t_without_offers)

        if marketplace:
            return PrefilterResult(True, "ok", topic, demand, negative)
        if negative and not strong:
            return PrefilterResult(False, "offer_pattern", topic, demand, negative)
        if not demand and "?" not in text:
            return PrefilterResult(False, "no_demand", topic, demand, negative)
        return PrefilterResult(True, "ok", topic, demand, negative)


# --------------------------------------------------------------------------
# Fáze B – LLM klasifikátor
# --------------------------------------------------------------------------
class LLMError(RuntimeError):
    pass


class _RateLimiter:
    """Jednoduchý limiter požadavků za minutu (free tiery mají přísné limity)."""

    def __init__(self, per_minute: int):
        self.interval = 60.0 / max(per_minute, 1)
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            delay = self._last + self.interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


def extract_json(raw: str) -> dict[str, Any]:
    """Robustně vytáhne JSON objekt z odpovědi LLM (i když ho obalí markdownem)."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(raw[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    raise LLMError(f"LLM nevrátil validní JSON: {raw[:200]!r}")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"true", "1", "yes", "ano"}


def _as_opt_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return None if s.lower() in {"", "null", "none", "neuvedeno", "n/a", "-"} else s[:300]


def _as_slug(value: Any) -> str | None:
    """"Landing page" → "landing_page", "Nový web" → "novy_web"."""
    s = _as_opt_str(value)
    return re.sub(r"[\s-]+", "_", normalize(s)) if s else None


def normalize_analysis(data: dict[str, Any]) -> dict[str, Any]:
    """Validuje a sjednotí výstup LLM – menší modely občas vrací nekonzistentní typy."""
    intent = normalize(str(data.get("intent", "jine"))).replace(" ", "_")
    intent = {"poptavka": "poptavajici", "nabidka": "nabizejici", "demand": "poptavajici",
              "offer": "nabizejici", "discussion": "diskuse"}.get(intent, intent)
    if intent not in VALID_INTENTS:
        intent = "jine"

    try:
        score = int(round(float(data.get("relevance_score", data.get("score", 1)))))
    except (TypeError, ValueError):
        score = 1
    score = min(max(score, 1), 10)

    urgency = normalize(str(data.get("urgency") or "neuvedeno"))
    if urgency not in VALID_URGENCY:
        urgency = "neuvedeno"

    is_active = _as_bool(data.get("is_active", False)) and intent == "poptavajici"

    return {
        "intent": intent,
        "is_active": is_active,
        "relevance_score": score,
        "project_type": _as_slug(data.get("project_type")),
        "platform": _as_opt_str(data.get("platform")),
        "budget": _as_opt_str(data.get("budget")),
        "deadline": _as_opt_str(data.get("deadline")),
        "urgency": urgency,
        "location": _as_opt_str(data.get("location")),
        "summary": _as_opt_str(data.get("summary")) or "",
        "reasoning": _as_opt_str(data.get("reasoning")) or "",
    }


def format_age(posted_at: datetime | None, now: datetime | None = None) -> str:
    if posted_at is None:
        return "neznámé"
    now = now or datetime.now(timezone.utc)
    minutes = max(int((now - posted_at).total_seconds() // 60), 0)
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h"
    return f"{hours // 24} dní"


class LLMClassifier:
    def __init__(self, settings: Settings):
        self.s = settings
        self.model = settings.llm_model
        self._limiter = _RateLimiter(settings.llm_max_requests_per_minute)
        self._client = httpx.Client(timeout=settings.llm_timeout_s)

    def close(self) -> None:
        self._client.close()

    # -- transport --------------------------------------------------------
    def _call_ollama(self, messages: list[dict[str, str]]) -> str:
        resp = self._client.post(
            f"{self.s.llm_base_url}/api/chat",
            json={
                "model": self.model,
                "messages": messages,
                "stream": False,
                "format": "json",
                "options": {"temperature": self.s.llm_temperature, "num_ctx": 8192},
            },
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    def _call_openai_compatible(self, messages: list[dict[str, str]]) -> str:
        headers = {"Content-Type": "application/json"}
        if self.s.llm_api_key:
            headers["Authorization"] = f"Bearer {self.s.llm_api_key}"
        resp = self._client.post(
            f"{self.s.llm_base_url}/chat/completions",
            headers=headers,
            json={
                "model": self.model,
                "messages": messages,
                "temperature": self.s.llm_temperature,
                "response_format": {"type": "json_object"},
                "max_tokens": self.s.llm_max_tokens,
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"].get("content")
        if not content:
            raise LLMError("LLM vrátil prázdnou odpověď (zkus zvýšit LLM_MAX_TOKENS v .env)")
        return content

    def _complete(self, messages: list[dict[str, str]], retries: int = 4) -> str:
        last_error: Exception | None = None
        for attempt in range(retries):
            self._limiter.wait()
            try:
                if self.s.llm_is_ollama:
                    return self._call_ollama(messages)
                return self._call_openai_compatible(messages)
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code
                if status in (429, 500, 502, 503, 504):
                    retry_after = exc.response.headers.get("retry-after")
                    try:
                        delay = float(retry_after) if retry_after else 0.0
                    except ValueError:
                        delay = 0.0
                    delay = max(delay, 2 ** (attempt + 1))
                    log.warning("LLM HTTP %s, opakuji za %.0fs", status, delay)
                    time.sleep(min(delay, 120))
                    continue
                raise LLMError(f"LLM HTTP {status}: {exc.response.text[:300]}") from exc
            except (httpx.TransportError, KeyError, IndexError) as exc:
                last_error = exc
                delay = 2 ** (attempt + 1)
                log.warning("LLM chyba (%s), opakuji za %ss", exc, delay)
                time.sleep(delay)
        raise LLMError(f"LLM selhalo po {retries} pokusech: {last_error}")

    # -- public -----------------------------------------------------------
    def classify(self, text: str, source: str = "", author: str = "",
                 posted_at: datetime | None = None) -> dict[str, Any]:
        text = text.strip()
        if len(text) > self.s.llm_max_input_chars:
            text = text[: self.s.llm_max_input_chars] + " …[zkráceno]"
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT_TEMPLATE.format(
                source=source or "neznámý", author=author or "neznámý",
                age=format_age(posted_at), text=text.replace('"""', "''"))},
        ]
        raw = self._complete(messages)
        try:
            data = extract_json(raw)
        except LLMError:
            # Jeden pokus o opravu – menší lokální modely občas "ukecají" odpověď.
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": "Odpověz znovu POUZE validním JSON objektem."}]
            data = extract_json(self._complete(messages, retries=2))
        return normalize_analysis(data)


# --------------------------------------------------------------------------
# Finální rozhodnutí
# --------------------------------------------------------------------------
def decide(analysis: dict[str, Any], posted_at: datetime | None, settings: Settings,
           now: datetime | None = None) -> tuple[bool, str | None]:
    """Vrací (is_lead, reject_reason). Lead = poptávka + aktivní + čerstvá + skóre > práh."""
    now = now or datetime.now(timezone.utc)
    if analysis["intent"] != "poptavajici":
        return False, f"intent:{analysis['intent']}"
    if not analysis["is_active"]:
        return False, "inactive"
    if analysis["relevance_score"] <= settings.min_relevance_score:
        return False, f"low_score:{analysis['relevance_score']}"
    if settings.lead_project_types and analysis.get("project_type") not in settings.lead_project_types:
        return False, f"project_type:{analysis.get('project_type') or 'neuvedeno'}"
    if posted_at is None:
        if not settings.allow_unknown_age:
            return False, "unknown_age"
    elif (now - posted_at).total_seconds() > settings.max_post_age_hours * 3600:
        return False, "too_old"
    return True, None
