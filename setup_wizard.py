"""Průvodce nastavením: zeptá se na vše potřebné, každý údaj ověří a zapíše do .env / DB.

  python setup_wizard.py            # celé nastavení
  python setup_wizard.py --groups   # jen výběr sledovaných skupin (např. po schválení žádostí)
  python setup_wizard.py --llm      # jen AI klíč

Spouští ho setup.bat. Návratový kód 0 = spustit aplikaci, 10 = nespouštět.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import re
import sys
import webbrowser
from pathlib import Path
from urllib.parse import quote

import httpx

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
ENV_TEMPLATE = BASE_DIR / ".env.example"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
GEMINI_KEY_PAGE = "https://aistudio.google.com/apikey"
TEST_POST = ("Hi everyone, I'm looking for someone to build a website for my bakery in Ohio. "
             "5 pages and online ordering, budget around $1,500.")

EXIT_START, EXIT_DONT_START = 0, 10


# --------------------------------------------------------------------------
# Pomocné funkce (testované v tests/test_setup_wizard.py)
# --------------------------------------------------------------------------
def update_env(path: Path, values: dict[str, str]) -> None:
    """Nastaví hodnoty v .env – existující řádky přepíše, chybějící přidá na konec."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    out = []
    for line in lines:
        m = re.match(r"^\s*#?\s*([A-Z0-9_]+)\s*=", line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in remaining.items()]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ.update(values)  # ať je load_settings() hned vidí


def read_env(path: Path) -> dict[str, str]:
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Z0-9_]+)\s*=(.*)$", line)
            if m:
                values[m.group(1)] = m.group(2).strip()
    return values


def parse_selection(answer: str, count: int) -> list[int]:
    """"1,3,5-7" → [0, 2, 4, 5, 6] (indexy od 0); "v" / "vse" / "all" = všechny."""
    answer = answer.strip().lower()
    if answer in {"v", "vse", "vše", "all", "a"}:
        return list(range(count))
    picked: list[int] = []
    for part in re.split(r"[,\s;]+", answer):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise ValueError(f"Nerozumím „{part}“ – zadej čísla, např. 1,3,5-7")
        start, end = int(m.group(1)), int(m.group(2) or m.group(1))
        for n in range(min(start, end), max(start, end) + 1):
            if not 1 <= n <= count:
                raise ValueError(f"Číslo {n} není v seznamu (1–{count})")
            if n - 1 not in picked:
                picked.append(n - 1)
    return picked


def pick_gemini_models(models: list[dict]) -> list[str]:
    """Seřadí dostupné modely Gemini Flash od nejnovějšího stabilního."""
    names = []
    for m in models:
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        name = m.get("name", "").removeprefix("models/")
        if re.search(r"lite|image|tts|live|thinking|audio|exp|embedding|preview|latest", name):
            continue
        ver = re.fullmatch(r"gemini-(\d+(?:\.\d+)?)-flash(?:-(\d{3}))?", name)
        if ver:
            names.append((float(ver.group(1)), 0 if ver.group(2) else 1, name))
    return [n for *_, n in sorted(names, reverse=True)]


# --------------------------------------------------------------------------
# Interakce
# --------------------------------------------------------------------------
def title(text: str) -> None:
    print("\n" + "=" * 64 + f"\n {text}\n" + "=" * 64)


def ask(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{question}{suffix}: ").strip()
    return answer or default


def yes(question: str, default: bool = True) -> bool:
    hint = "A/n" if default else "a/N"
    answer = input(f"{question} ({hint}): ").strip().lower()
    if not answer:
        return default
    return answer[0] in "ay"


def pause(text: str = "Až budeš hotový, stiskni ENTER…") -> None:
    input(text)


# --------------------------------------------------------------------------
# Kroky
# --------------------------------------------------------------------------
def step_market() -> str:
    title("1/6  Odkud chceš klienty")
    print("  1) Zahraničí – anglicky psané poptávky (USA, UK, Kanada, Austrálie…)  [doporučeno]")
    print("  2) Česko – česky psané poptávky")
    choice = ask("Vyber 1 nebo 2", "1")
    market = "cz" if choice.strip() == "2" else "international"
    hours = {"international": ("9", "1"), "cz": ("7", "23")}[market]
    update_env(ENV_PATH, {"TARGET_MARKET": market, "ACTIVE_HOURS_START": hours[0],
                          "ACTIVE_HOURS_END": hours[1]})
    print(f"  OK – aplikace poběží {hours[0]}:00–{hours[1]}:00 našeho času "
          + ("(USA píšou hlavně odpoledne a večer)." if market == "international" else "."))
    return market


def _list_gemini_models(key: str) -> list[str]:
    resp = httpx.get("https://generativelanguage.googleapis.com/v1beta/models",
                     params={"key": key, "pageSize": 200}, timeout=30)
    if resp.status_code in (400, 401, 403):
        raise ValueError("Google klíč odmítl. Zkontroluj, že jsi zkopíroval celý klíč (začíná „AIza“).")
    resp.raise_for_status()
    return pick_gemini_models(resp.json().get("models", []))


def _test_model(key: str, model: str) -> dict:
    from analyzer import LLMClassifier
    from config import load_settings
    s = dataclasses.replace(load_settings(), llm_provider="gemini", llm_base_url=GEMINI_BASE_URL,
                            llm_api_key=key, llm_model=model, llm_max_requests_per_minute=30)
    clf = LLMClassifier(s)
    try:
        return clf.classify(TEST_POST)
    finally:
        clf.close()


def step_llm() -> None:
    from analyzer import LLMError
    title("2/6  AI pro vyhodnocení příspěvků (Google Gemini – zdarma)")
    current = read_env(ENV_PATH)
    if current.get("LLM_PROVIDER") == "gemini" and current.get("LLM_API_KEY"):
        if yes("Gemini klíč už je nastavený. Ponechat ho?"):
            return
    print("  Otevírám stránku Google AI Studio. Přihlas se Google účtem a klikni na")
    print("  „Create API key“ (Vytvořit klíč API). Klíč zkopíruj a vlož sem.")
    print(f"  Pokud se stránka neotevře, zadej do prohlížeče: {GEMINI_KEY_PAGE}")
    webbrowser.open(GEMINI_KEY_PAGE)
    while True:
        key = ask("\n  Vlož Gemini klíč (pravým tlačítkem myši / Ctrl+V)")
        if not key:
            print("  Klíč je potřeba – bez něj aplikace neumí poznat poptávku.")
            continue
        try:
            print("  Ověřuji klíč a hledám nejnovější model…")
            candidates = _list_gemini_models(key) or ["gemini-2.5-flash"]
        except (ValueError, httpx.HTTPError) as exc:
            print(f"  {exc}")
            continue
        for model in candidates[:3]:
            try:
                print(f"  Zkouším model {model} na ukázkové poptávce…")
                result = _test_model(key, model)
            except LLMError as exc:
                print(f"    nefunguje: {str(exc)[:160]}")
                continue
            print(f"  Funguje. AI vyhodnotila ukázku jako: {result['intent']}, skóre {result['relevance_score']}/10")
            print(f"  Shrnutí: {result['summary']}")
            update_env(ENV_PATH, {"LLM_PROVIDER": "gemini", "LLM_API_KEY": key, "LLM_MODEL": model,
                                  "LLM_BASE_URL": GEMINI_BASE_URL, "LLM_MAX_REQUESTS_PER_MINUTE": "8"})
            return
        print("  Žádný model neodpověděl. Zkus to za minutu znovu, nebo vytvoř nový klíč.")


def step_browser() -> None:
    title("3/6  Prohlížeč")
    from playwright.async_api import async_playwright

    async def detect() -> str | None:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(channel="chrome", headless=True)
            except Exception:  # noqa: BLE001 – Chrome není nainstalovaný
                return None
            version = browser.version
            await browser.close()
            return version

    version = asyncio.run(detect())
    if version:
        update_env(ENV_PATH, {"BROWSER_CHANNEL": "chrome"})
        print(f"  Našel jsem Google Chrome {version} – použiju ho (Facebook ho zná, je to bezpečnější).")
    else:
        update_env(ENV_PATH, {"BROWSER_CHANNEL": ""})
        print("  Google Chrome nenalezen – použiju vestavěný Chromium. To je v pořádku.")


def step_facebook() -> bool:
    title("4/6  Přihlášení na Facebook")
    from config import load_settings
    from scraper import ScraperError, cmd_check, cmd_login
    settings = load_settings()
    if settings.storage_state_path.exists():
        print("  Ověřuji uložené přihlášení…")
        try:
            if asyncio.run(cmd_check(settings)) and yes("Použít stávající přihlášení?"):
                return True
        except ScraperError as exc:
            print(f"  Uložené přihlášení neplatí ({exc}).")
    print("  Otevře se okno prohlížeče. Přihlas se účtem, který je (nebo bude) členem skupin.")
    print("  Tip: vedlejší účet s historií je bezpečnější než hlavní osobní účet.")
    pause("  Stiskni ENTER pro otevření prohlížeče…")
    return asyncio.run(cmd_login(settings))


def step_groups(market: str) -> None:
    title("5/6  Skupiny, které se budou hlídat")
    from config import GROUP_SEARCH_TERMS, SEARCH_QUERIES, load_settings
    from database import Database
    from scraper import FacebookScraper, ScraperError

    settings = load_settings()
    db = Database(settings.db_path)
    db.seed_defaults()
    db.set_keywords("search", SEARCH_QUERIES[market])
    print("  Facebook vyhledávání poptávek už je nastavené – poběží i bez skupin.")
    print("  Nejvíc leadů ale přichází ze skupin. Vhodné jsou skupiny s tisíci členy a denními")
    print("  příspěvky, kde majitelé firem shánějí služby (ne skupiny plné webařů).")

    if yes("\n  Otevřít v prohlížeči vyhledávání vhodných skupin?"):
        terms = GROUP_SEARCH_TERMS[market]
        for term in terms[:8]:
            webbrowser.open(f"https://www.facebook.com/search/groups/?q={quote(term)}")
        print("  Otevřel jsem vyhledávání: " + ", ".join(terms[:8]))
        print("  Další nápady: " + ", ".join(terms[8:]))
        print("  Přidej se do 10–20 aktivních skupin. U soukromých se čeká na schválení –")
        print("  ty pak doplníš později příkazem: .venv\\Scripts\\python setup_wizard.py --groups")
        pause("\n  Až se přidáš do skupin, stiskni ENTER…")

    print("  Načítám skupiny, ve kterých je tvůj účet členem (chvíli to potrvá)…")
    try:
        async def load():
            async with FacebookScraper(settings, db) as fb:
                return await fb.joined_groups()
        groups = asyncio.run(load())
    except ScraperError as exc:
        print(f"  Nepodařilo se načíst skupiny: {exc}")
        groups = []
    except Exception as exc:  # noqa: BLE001
        print(f"  Nepodařilo se načíst skupiny ({exc}). Můžeš je vložit ručně.")
        groups = []

    known = {s["url"] for s in db.list_sources()}
    groups = [g for g in groups if g["url"] not in known]
    if groups:
        print()
        for i, g in enumerate(groups, 1):
            print(f"  {i:>3}. {g['name']}")
        while True:
            answer = ask("\n  Které hlídat? Čísla oddělená čárkou (1,3,5-8), v = všechny, ENTER = žádné", "")
            if not answer:
                break
            try:
                for idx in parse_selection(answer, len(groups)):
                    db.add_source(groups[idx]["name"], groups[idx]["url"])
                break
            except ValueError as exc:
                print(f"  {exc}")
    elif not known:
        print("  Zatím jsi v žádné skupině (nebo se je nepodařilo načíst).")

    print("\n  Můžeš vložit i URL dalších skupin (jedna na řádek, prázdný řádek = konec):")
    while url := input("  URL: ").strip():
        if re.search(r"facebook\.com/groups/[^/\s]+", url):
            db.add_source(url.rstrip("/").split("/groups/")[1].split("/")[0], url)
        else:
            print("  To není odkaz na FB skupinu (https://www.facebook.com/groups/...).")
    total = len(db.list_sources(enabled_only=True))
    print(f"  Sledovaných skupin: {total}")


def step_autostart() -> None:
    title("6/6  Spouštění")
    if not sys.platform.startswith("win"):
        print("  Automatické spuštění na Macu/Linuxu nastavíš podle README (kapitola Běh 24/7).")
        return
    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
    shortcut = startup / "FB Lead Hunter.cmd"
    if yes("  Spouštět aplikaci automaticky po zapnutí počítače?"):
        startup.mkdir(parents=True, exist_ok=True)
        shortcut.write_text(f'@echo off\r\ncd /d "{BASE_DIR}"\r\nstart "FB Lead Hunter" /min cmd /c start.bat\r\n',
                            encoding="utf-8")
        print("  Hotovo – po přihlášení do Windows se aplikace spustí sama (minimalizovaně).")
    elif shortcut.exists():
        shortcut.unlink()
        print("  Automatické spouštění vypnuto.")


def main() -> int:
    parser = argparse.ArgumentParser(description="FB Lead Hunter – průvodce nastavením")
    parser.add_argument("--groups", action="store_true", help="jen výběr skupin")
    parser.add_argument("--llm", action="store_true", help="jen nastavení AI klíče")
    args = parser.parse_args()

    if not ENV_PATH.exists() and ENV_TEMPLATE.exists():
        ENV_PATH.write_text(ENV_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    os.environ.update({k: v for k, v in read_env(ENV_PATH).items() if v})

    print("\nFB Lead Hunter – nastavení. Na otázky odpovídej a potvrzuj klávesou ENTER.")
    print("(Hodnota v [hranatých závorkách] se použije, když jen stiskneš ENTER.)")
    try:
        if args.llm:
            step_llm()
            return EXIT_DONT_START
        market = read_env(ENV_PATH).get("TARGET_MARKET") or "international"
        if args.groups:
            step_groups(market)
            return EXIT_DONT_START
        market = step_market()
        step_llm()
        step_browser()
        if not step_facebook():
            print("\nPřihlášení na Facebook se nepovedlo. Spusť setup.bat znovu.")
            return EXIT_DONT_START
        step_groups(market)
        step_autostart()
    except KeyboardInterrupt:
        print("\nPřerušeno. Nastavení můžeš kdykoli dokončit spuštěním setup.bat.")
        return EXIT_DONT_START

    title("Hotovo")
    print("  Dashboard: http://localhost:8501 (otevře se sám po spuštění)")
    print("  Aplikace poběží, dokud necháš otevřené její okno. Ukončíš ji zavřením okna.")
    print("  Příště ji spustíš dvojklikem na start.bat.")
    return EXIT_START if yes("\n  Spustit aplikaci teď?") else EXIT_DONT_START


if __name__ == "__main__":
    sys.exit(main())
