"""Centrální konfigurace aplikace.

Všechny hodnoty se načítají z proměnných prostředí / souboru `.env`
(šablona je v `.env.example`). Nastavení, která se mění za běhu
(sledované skupiny, klíčová slova), jsou v SQLite a edituješ je v dashboardu.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "ano", "on"}


# Předvolby pro LLM poskytovatele. Všichni kromě "ollama" mluví
# OpenAI-kompatibilním API (/chat/completions).
LLM_PRESETS: dict[str, dict[str, str]] = {
    "ollama": {"base_url": "http://localhost:11434", "model": "llama3.1:8b"},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "meta-llama/llama-3.3-70b-instruct:free",
    },
}

@dataclass(frozen=True)
class Settings:
    # --- Cesty -------------------------------------------------------------
    data_dir: Path
    db_path: Path
    storage_state_path: Path
    log_path: Path

    # --- Prohlížeč ---------------------------------------------------------
    headless: bool
    browser_channel: str  # "" = Playwright Chromium, "chrome" = nainstalovaný Google Chrome
    browser_executable_path: str  # volitelně cesta ke konkrétnímu prohlížeči
    user_agent: str
    locale: str
    timezone: str
    viewport_width: int
    viewport_height: int
    proxy: str
    block_media: bool

    # --- Rate limiting / ochrana účtu ---------------------------------------
    active_hours_start: int
    active_hours_end: int
    cycle_interval_min_minutes: int
    cycle_interval_max_minutes: int
    pause_between_sources_min_s: float
    pause_between_sources_max_s: float
    max_page_loads_per_hour: int
    max_page_loads_per_day: int
    max_sources_per_cycle: int
    max_search_queries_per_cycle: int
    scrolls_per_source_min: int
    scrolls_per_source_max: int
    stop_after_known_posts: int
    stop_after_old_posts: int
    blocked_cooldown_hours: int

    # --- Filtrace ----------------------------------------------------------
    max_post_age_hours: int
    allow_unknown_age: bool
    min_relevance_score: int  # lead musí mít skóre STRIKTNĚ větší než tato hodnota
    lead_project_types: tuple[str, ...]  # prázdné = všechny typy projektů

    # --- LLM ---------------------------------------------------------------
    llm_provider: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_temperature: float
    llm_timeout_s: float
    llm_max_requests_per_minute: int
    llm_max_input_chars: int
    llm_max_tokens: int

    # --- Notifikace --------------------------------------------------------
    telegram_bot_token: str
    telegram_chat_id: str
    discord_webhook_url: str

    @property
    def llm_is_ollama(self) -> bool:
        return self.llm_provider == "ollama"


def load_settings() -> Settings:
    data_dir = Path(_str("DATA_DIR") or BASE_DIR / "data")
    data_dir.mkdir(parents=True, exist_ok=True)

    provider = _str("LLM_PROVIDER", "groq").lower()
    preset = LLM_PRESETS.get(provider, LLM_PRESETS["groq"])

    return Settings(
        data_dir=data_dir,
        db_path=Path(_str("DB_PATH") or data_dir / "leads.db"),
        storage_state_path=Path(_str("STORAGE_STATE_PATH") or data_dir / "fb_state.json"),
        log_path=Path(_str("LOG_PATH") or data_dir / "scraper.log"),
        headless=_bool("HEADLESS", True),
        browser_channel=_str("BROWSER_CHANNEL", ""),
        browser_executable_path=_str("BROWSER_EXECUTABLE_PATH", ""),
        user_agent=_str("USER_AGENT"),  # prázdné = odvodí se ze skutečné verze prohlížeče
        locale=_str("BROWSER_LOCALE", "cs-CZ"),
        timezone=_str("BROWSER_TIMEZONE", "Europe/Prague"),
        viewport_width=_int("VIEWPORT_WIDTH", 1366),
        viewport_height=_int("VIEWPORT_HEIGHT", 768),
        proxy=_str("PROXY", ""),
        block_media=_bool("BLOCK_MEDIA", True),
        active_hours_start=_int("ACTIVE_HOURS_START", 9),
        active_hours_end=_int("ACTIVE_HOURS_END", 1),  # přes půlnoc – USA píšou odpoledne a večer našeho času
        cycle_interval_min_minutes=_int("CYCLE_INTERVAL_MIN_MINUTES", 20),
        cycle_interval_max_minutes=_int("CYCLE_INTERVAL_MAX_MINUTES", 40),
        pause_between_sources_min_s=_float("PAUSE_BETWEEN_SOURCES_MIN_S", 40),
        pause_between_sources_max_s=_float("PAUSE_BETWEEN_SOURCES_MAX_S", 120),
        max_page_loads_per_hour=_int("MAX_PAGE_LOADS_PER_HOUR", 25),
        max_page_loads_per_day=_int("MAX_PAGE_LOADS_PER_DAY", 250),
        max_sources_per_cycle=_int("MAX_SOURCES_PER_CYCLE", 8),
        max_search_queries_per_cycle=_int("MAX_SEARCH_QUERIES_PER_CYCLE", 2),
        scrolls_per_source_min=_int("SCROLLS_PER_SOURCE_MIN", 4),
        scrolls_per_source_max=_int("SCROLLS_PER_SOURCE_MAX", 9),
        stop_after_known_posts=_int("STOP_AFTER_KNOWN_POSTS", 6),
        stop_after_old_posts=_int("STOP_AFTER_OLD_POSTS", 5),
        blocked_cooldown_hours=_int("BLOCKED_COOLDOWN_HOURS", 24),
        max_post_age_hours=_int("MAX_POST_AGE_HOURS", 72),
        allow_unknown_age=_bool("ALLOW_UNKNOWN_AGE", True),
        min_relevance_score=_int("MIN_RELEVANCE_SCORE", 8),
        lead_project_types=tuple(
            t.strip() for t in _str("LEAD_PROJECT_TYPES", "novy_web,eshop,landing_page,redesign").split(",")
            if t.strip()),
        llm_provider=provider,
        llm_base_url=(_str("LLM_BASE_URL") or preset["base_url"]).rstrip("/"),
        llm_api_key=_str("LLM_API_KEY"),
        llm_model=_str("LLM_MODEL") or preset["model"],
        llm_temperature=_float("LLM_TEMPERATURE", 0.0),
        llm_timeout_s=_float("LLM_TIMEOUT_S", 90),
        llm_max_requests_per_minute=_int("LLM_MAX_REQUESTS_PER_MINUTE", 20),
        llm_max_input_chars=_int("LLM_MAX_INPUT_CHARS", 3500),
        llm_max_tokens=_int("LLM_MAX_TOKENS", 2048),
        telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_str("TELEGRAM_CHAT_ID"),
        discord_webhook_url=_str("DISCORD_WEBHOOK_URL"),
    )


# ---------------------------------------------------------------------------
# Výchozí klíčová slova (při prvním spuštění se vloží do DB, pak je edituješ
# v dashboardu). Porovnává se bez diakritiky a bez ohledu na velikost písmen,
# jako PREFIX slova – "web" tedy chytí i "webovky", "webu", "weby".
# ---------------------------------------------------------------------------

# Téma: o čem příspěvek musí být (stačí 1 shoda).
DEFAULT_TOPIC_KEYWORDS = [
    "web", "webov", "webstr", "webdesign", "webar", "webak", "stranky", "stranek", "stránka",
    "e-shop", "eshop", "e shop", "internetovy obchod", "internetovou prodejnu",
    "wordpress", "woocommerce", "shoptet", "upgates", "prestashop", "shopify",
    "wix", "webflow", "elementor", "joomla", "drupal", "framer",
    "landing page", "microsite", "redesign", "webhosting",
    "website", "web developer", "frontend", "kodér", "koder",
    # EN
    "site", "online store", "ecommerce", "e-commerce", "squarespace", "bigcommerce", "revamp",
    "homepage", "home page", "web dev",
]

# Poptávka: signál, že autor něco SHÁNÍ (stačí 1 shoda).
DEFAULT_DEMAND_KEYWORDS = [
    "poptav", "hledam", "hledame", "shanim", "shanime", "potreb", "chtel bych", "chtela bych",
    "chteli bychom", "doporuc", "kdo by mi", "kdo by nam", "kdo umi", "kdo dela", "kdo vytvori",
    "umel by nekdo", "umela by nekdo", "zna nekdo", "znate nekoho", "nevite o nekom",
    "cenovou nabidku", "cenova nabidka", "za kolik", "kolik stoji", "kolik by stal",
    "kolik si rikate", "zakazk", "subdodavatel", "spolupraci na", "ozve se nekdo",
    "pomuze nekdo", "pomohl by", "pomohla by", "predelat", "upravit web", "uprava webu",
    # EN
    "looking for", "need a", "need an", "need someone", "need help", "in need of", "seeking",
    "hiring", "searching for", "anyone know", "anyone recommend", "can anyone", "could anyone",
    "does anyone", "recommend", "who can", "who could", "quote", "how much would", "how much does",
    "how much should", "budget", "willing to pay", "paid project", "build me", "build my", "redo my",
    "revamp my", "update my website", "fix my website", "want a website", "want to build",
    "would like a website",
]

# Silná poptávka – přebije nalezený "nabízecí" vzor (např. "Hledám webaře, nabízím 20k").
STRONG_DEMAND_KEYWORDS = [
    "poptav", "hledam", "hledame", "shanim", "shanime", "potrebuji", "potrebujeme",
    "potreboval bych", "potrebovala bych", "doporucte", "doporucite",
    "looking for", "need a", "need an", "need someone", "in need of", "seeking", "hiring",
    "searching for", "can anyone recommend", "any recommendations",
]

# Nabídky služeb / spam – při shodě (a bez silné poptávky) se příspěvek zahodí bez LLM.
DEFAULT_NEGATIVE_KEYWORDS = [
    "nabizim tvorbu", "nabizim sluzby", "nabizime tvorbu", "nabizime sluzby",
    "delam weby", "delame weby", "tvorim weby", "tvorime weby", "vytvorim vam", "vytvorime vam",
    "vytvorim web", "vytvorime web", "udelam vam", "udelame vam", "naprogramuji vam",
    "jsem webdesigner", "jsem webar", "jsem webova vyvojarka", "jsem webovy vyvojar", "jsem freelancer",
    "volne kapacity", "volnou kapacitu", "mam volne", "prijimam zakazky", "prijimame zakazky",
    "moje portfolio", "nase portfolio", "reference na", "napiste mi do sz", "piste do sz",
    "levne weby", "web na klic za", "akcni cena", "sleva na tvorbu", "zdarma do portfolia",
    "hledam praci", "hledam brigadu", "hledam klienty", "hledam nove klienty", "hledame klienty",
    "hledame nove klienty", "nabizim se",
    # EN – nabídky služeb (anglické skupiny jsou jimi zahlcené)
    "i will create", "i will build", "i will design", "i will develop", "i can build", "i can create",
    "i can design", "i build websites", "i design websites", "i create websites", "i make websites",
    "we build websites", "we design websites", "we create websites", "our agency", "our team can",
    "we offer", "i offer", "my portfolio", "our portfolio", "check my portfolio", "check my profile",
    "open for work", "available for work", "available for new projects", "taking new clients",
    "accepting new clients", "looking for clients", "looking for new clients", "looking for work",
    "looking for a job", "looking for projects", "looking for new projects", "need a website?",
    "need a new website?", "need a website for your", "need a new website for your", "for your business?",
    "i build", "we build", "i design", "we design", "i develop", "we develop",
    "affordable website", "cheap website", "website for only", "starting at $",
    "starting from $", "limited offer", "fiverr.com", "upwork.com/freelancers",
]

# Spam, který se zahazuje VŽDY.
SPAM_PATTERNS = [
    r"wa\.me/", r"t\.me/", r"\bcrypto\b", r"\bbitcoin\b", r"\bforex\b", r"\binvestic",
    r"prace z domova", r"vydelek az", r"pasivni prijem", r"\bmlm\b", r"\bbinance\b",
    r"make money online", r"work from home", r"earn \$?\d+ (?:daily|per day|a day)", r"sugar (?:daddy|mommy)",
]

# Cílový trh: "international" (anglické poptávky ze zahraničí) nebo "cz".
TARGET_MARKET = _str("TARGET_MARKET", "international").lower()

# Dotazy pro FB Search (vyhledávání příspěvků, filtr "Nejnovější").
SEARCH_QUERIES = {
    "international": [
        "looking for web designer",
        "need a website for my business",
        "looking for someone to build a website",
        "website redesign needed",
        "looking for wordpress developer",
        "need a shopify developer",
        "hiring web developer",
        "recommend a web designer",
    ],
    "cz": [
        "poptávám tvorbu webu",
        "hledám webaře",
        "hledám webdesignera",
        "poptávám e-shop",
        "redesign webu poptávka",
        "shoptet hledám",
    ],
}
DEFAULT_SEARCH_QUERIES = SEARCH_QUERIES.get(TARGET_MARKET, SEARCH_QUERIES["international"])

# Hledané výrazy pro nalezení vhodných FB skupin (průvodce je otevře v prohlížeči).
GROUP_SEARCH_TERMS = {
    "international": [
        "small business owners", "entrepreneurs", "startup founders", "shopify store owners",
        "ecommerce entrepreneurs", "wordpress help", "website design help", "hire web developer",
        "web design jobs", "freelance web developer jobs", "local business owners usa",
        "small business uk", "small business australia", "real estate agents", "coaches and consultants",
    ],
    "cz": [
        "podnikatelky", "podnikatelé", "živnostníci", "OSVČ", "začínající podnikatelé", "e-shopy",
        "shoptet", "wordpress čeština", "malé firmy", "podnikání",
    ],
}

LEAD_STATUSES = ["Nekontaktováno", "Kontaktováno", "Odpověděli", "Zamítnuto"]
