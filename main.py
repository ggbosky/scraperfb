"""Worker: nekonečná smyčka FB → scraper → předfiltr → LLM → SQLite → notifikace.

Spuštění:
  python main.py            # běží trvale (cykly s náhodným rozestupem, jen v aktivních hodinách)
  python main.py --once     # jeden cyklus a konec (vhodné pro cron / Plánovač úloh)
  python main.py --force    # spustit, i když se zdá, že už jiný worker běží
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

from analyzer import KeywordPrefilter, LLMClassifier, LLMError, decide, extract_contacts
from config import Settings, load_settings
from database import Database, PostRecord, from_iso, to_iso, utcnow
from notifier import Notifier
from scraper import (
    BlockedError,
    CheckpointError,
    FacebookScraper,
    RateLimitReached,
    ScrapedPost,
    SessionExpiredError,
    search_posts_url,
)

log = logging.getLogger("worker")

HEARTBEAT_STALE_S = 180


def setup_logging(settings: Settings) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file = RotatingFileHandler(settings.log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    file.setFormatter(fmt)
    root.handlers = [console, file]
    logging.getLogger("httpx").setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# Zpracování příspěvků
# --------------------------------------------------------------------------
@dataclass
class CycleStats:
    sources_scraped: int = 0
    posts_found: int = 0
    posts_new: int = 0
    llm_calls: int = 0
    leads_found: int = 0


class Pipeline:
    def __init__(self, db: Database, settings: Settings, classifier: LLMClassifier, notifier: Notifier):
        self.db = db
        self.s = settings
        self.classifier = classifier
        self.notifier = notifier
        self.stats = CycleStats()
        self.prefilter = self._build_prefilter()

    def _build_prefilter(self) -> KeywordPrefilter:
        return KeywordPrefilter(
            topic=self.db.get_keywords("topic"),
            demand=self.db.get_keywords("demand"),
            negative=self.db.get_keywords("negative"),
        )

    def reload(self) -> None:
        """Načte aktuální klíčová slova z DB (mohla se změnit v dashboardu)."""
        self.prefilter = self._build_prefilter()
        self.stats = CycleStats()

    def handle(self, post: ScrapedPost) -> str:
        """Uloží nový příspěvek a vyhodnotí ho. Vrací výsledek pro log."""
        self.stats.posts_found += 1
        contacts = extract_contacts(post.text)
        record = PostRecord(
            post_key=post.post_key, text=post.text, content_hash=post.content_hash, url=post.url,
            source_type=post.source_type, source_name=post.source_name, source_url=post.source_url,
            author=post.author, author_url=post.author_url, author_id=post.author_id,
            contact_emails=", ".join(contacts["emails"]) or None,
            contact_phones=", ".join(contacts["phones"]) or None,
            contact_websites=", ".join(contacts["websites"]) or None,
            posted_at=post.posted_at,
        )
        post_id = self.db.insert_post(record)
        if post_id is None:
            return "known"
        self.stats.posts_new += 1
        return self.evaluate(self.db.get_post(post_id))

    def evaluate(self, row: dict) -> str:
        post_id = row["id"]
        posted_at = from_iso(row.get("posted_at"))

        # 1) Stáří – staré příspěvky vůbec neposíláme do LLM.
        if posted_at and utcnow() - posted_at > timedelta(hours=self.s.max_post_age_hours):
            self.db.mark_skipped(post_id, "too_old")
            return "too_old"

        # 2) Rychlý klíčový předfiltr.
        pre = self.prefilter.check(row["text"])
        if not pre.passed:
            self.db.mark_skipped(post_id, f"prefilter:{pre.reason}")
            return f"prefilter:{pre.reason}"

        # 3) Stejný text už jsme hodnotili (cross-post do více skupin).
        dup = self.db.find_by_content_hash(row["content_hash"], exclude_id=post_id)
        if dup:
            self.db.mark_skipped(post_id, "duplicate", duplicate_of=dup["id"])
            return "duplicate"

        # 4) LLM klasifikace.
        try:
            self.stats.llm_calls += 1
            analysis = self.classifier.classify(
                row["text"], source=row.get("source_name") or "", author=row.get("author") or "",
                posted_at=posted_at)
        except LLMError as exc:
            log.error("LLM selhalo pro příspěvek %s: %s", post_id, exc)
            self.db.mark_analysis_error(post_id, str(exc))
            return "llm_error"

        is_lead, reason = decide(analysis, posted_at, self.s)
        self.db.save_analysis(post_id, analysis, is_lead, reason, self.classifier.model)
        if not is_lead:
            return reason or "rejected"

        # 5) Lead → okamžitá notifikace.
        self.stats.leads_found += 1
        lead = self.db.get_post(post_id)
        log.info("🔥 LEAD (%s/10): %s | %s", analysis["relevance_score"], analysis["summary"], row.get("url"))
        if self.notifier.send_lead(lead):
            self.db.mark_notified(post_id)
        return "lead"

    def reanalyze_failed(self) -> None:
        rows = self.db.posts_for_reanalysis()
        if rows:
            log.info("Znovu analyzuji %d příspěvků, u kterých dříve selhalo LLM", len(rows))
        for row in rows:
            self.evaluate(row)


# --------------------------------------------------------------------------
# Plánování cyklu
# --------------------------------------------------------------------------
def build_plan(db: Database, settings: Settings) -> list[dict]:
    """Skupiny, které nejdéle nebyly navštíveny + pár náhodných vyhledávacích dotazů."""
    groups = db.list_sources(enabled_only=True)[: settings.max_sources_per_cycle]
    queries = db.get_keywords("search")
    picked = random.sample(queries, min(len(queries), settings.max_search_queries_per_cycle))
    searches = [{"id": None, "type": "search", "name": f"🔎 {q}", "url": search_posts_url(q)} for q in picked]
    plan = groups + searches
    random.shuffle(plan)  # pořadí návštěv pokaždé jiné
    return plan


class Worker:
    def __init__(self, settings: Settings):
        self.s = settings
        self.db = Database(settings.db_path)
        self.db.seed_defaults()
        self.notifier = Notifier(settings)
        self.classifier = LLMClassifier(settings)
        self.pipeline = Pipeline(self.db, settings, self.classifier, self.notifier)
        self.pid = str(os.getpid())
        self._run_now = False

    # ------------------------------------------------------------ helpers
    def heartbeat(self) -> None:
        self.db.set_kv("worker_heartbeat", to_iso(utcnow()))
        self.db.set_kv("worker_pid", self.pid)

    def another_worker_running(self) -> bool:
        hb = from_iso(self.db.get_kv("worker_heartbeat"))
        other_pid = self.db.get_kv("worker_pid")
        return bool(hb and other_pid != self.pid and (utcnow() - hb).total_seconds() < HEARTBEAT_STALE_S)

    def in_active_hours(self) -> bool:
        hour = datetime.now(ZoneInfo(self.s.timezone)).hour
        start, end = self.s.active_hours_start, self.s.active_hours_end
        return start <= hour < end if start <= end else (hour >= start or hour < end)

    async def sleep(self, seconds: float) -> None:
        """Spí s průběžným heartbeatem; přeruší se, když dashboard požádá o okamžitý běh."""
        end = asyncio.get_running_loop().time() + seconds
        while (remaining := end - asyncio.get_running_loop().time()) > 0:
            self.heartbeat()
            if self.db.get_kv("run_now") == "1":
                self.db.set_kv("run_now", None)
                self._run_now = True
                log.info("Dashboard požádal o okamžitý běh")
                return
            await asyncio.sleep(min(15, remaining))

    def pause(self, reason: str) -> None:
        self.db.set_kv("paused", "1")
        self.db.set_kv("pause_reason", reason)
        log.error("⏸  Worker pozastaven: %s", reason)
        self.notifier.send_alert(f"Scraper pozastaven: {reason}\nPo vyřešení ho obnov v dashboardu.")

    # -------------------------------------------------------------- cycle
    async def run_cycle(self) -> None:
        self.pipeline.reload()
        stats = self.pipeline.stats
        run_id = self.db.start_run()
        error = None
        try:
            await asyncio.to_thread(self.pipeline.reanalyze_failed)
            plan = build_plan(self.db, self.s)
            if not plan:
                log.warning("Nejsou nastavené žádné skupiny ani vyhledávací dotazy – přidej je v dashboardu.")
                return
            cutoff = utcnow() - timedelta(hours=self.s.max_post_age_hours)
            log.info("▶ Nový cyklus: %d zdrojů", len(plan))
            async with FacebookScraper(self.s, self.db, heartbeat=self.heartbeat) as fb:
                await fb.warm_up()
                for i, src in enumerate(plan):
                    if self.db.get_kv("paused") == "1":
                        log.info("Pozastaveno z dashboardu – přerušuji cyklus")
                        break
                    self.heartbeat()
                    try:
                        posts = await fb.scrape_source(
                            src["url"], src["type"], src["name"], is_known=self.db.known_keys, cutoff=cutoff)
                        stats.sources_scraped += 1
                        if src["id"]:
                            self.db.update_source(src["id"], last_scraped_at=to_iso(utcnow()), last_error=None)
                    except (SessionExpiredError, CheckpointError, BlockedError, RateLimitReached):
                        raise
                    except Exception as exc:  # noqa: BLE001 – jedna rozbitá skupina nezastaví cyklus
                        log.exception("Chyba při scrapování %s", src["name"])
                        if src["id"]:
                            self.db.update_source(src["id"], last_error=str(exc)[:300],
                                                  last_scraped_at=to_iso(utcnow()))
                        continue

                    for post in posts:
                        outcome = await asyncio.to_thread(self.pipeline.handle, post)
                        if outcome not in ("known", "too_old"):
                            log.debug("  %s → %s", post.post_key, outcome)

                    if i < len(plan) - 1:
                        await self.sleep(random.uniform(self.s.pause_between_sources_min_s,
                                                        self.s.pause_between_sources_max_s))
        except RateLimitReached as exc:
            log.warning("⏳ %s – zbytek cyklu přeskočen", exc)
            error = str(exc)
        except SessionExpiredError as exc:
            error = str(exc)
            self.pause(f"Session vypršela: {exc}")
        except CheckpointError as exc:
            error = str(exc)
            self.pause(f"Facebook vyžaduje ověření účtu. Přihlas se ručně v prohlížeči a vyřeš ho. ({exc})")
        except BlockedError as exc:
            error = str(exc)
            until = utcnow() + timedelta(hours=self.s.blocked_cooldown_hours)
            self.db.set_kv("cooldown_until", to_iso(until))
            self.notifier.send_alert(f"FB dočasně omezil účet – pauza do {until:%d.%m. %H:%M} UTC.")
        except Exception as exc:  # noqa: BLE001
            error = repr(exc)
            log.exception("Neočekávaná chyba v cyklu")
        finally:
            self.db.finish_run(run_id, error=error, **stats.__dict__)
            log.info("■ Cyklus hotov: %s", stats)

    async def run(self, once: bool = False, force: bool = False) -> None:
        if self.another_worker_running() and not force:
            log.error("Jiný worker (PID %s) už běží. Dva najednou = dvojnásobné riziko blokace. "
                      "Pokud neběží, použij --force.", self.db.get_kv("worker_pid"))
            return
        self.heartbeat()
        log.info("Worker spuštěn (PID %s), LLM: %s / %s, notifikace: %s", self.pid,
                 self.s.llm_provider, self.s.llm_model, "ano" if self.notifier.enabled else "NE")
        try:
            while True:
                self.heartbeat()
                if self.db.get_kv("paused") == "1":
                    if once:
                        log.warning("Worker je pozastavený (%s)", self.db.get_kv("pause_reason"))
                        return
                    await self.sleep(60)
                    continue
                cooldown = from_iso(self.db.get_kv("cooldown_until"))
                if cooldown and cooldown > utcnow():
                    log.info("Cooldown po blokaci do %s", cooldown)
                    if once:
                        return
                    await self.sleep(min(600, (cooldown - utcnow()).total_seconds()))
                    continue
                if self.db.get_kv("run_now") == "1":
                    self.db.set_kv("run_now", None)
                    self._run_now = True
                if not self.in_active_hours() and not self._run_now:
                    if once:
                        log.info("Mimo aktivní hodiny (%s–%s) – nic nedělám",
                                 self.s.active_hours_start, self.s.active_hours_end)
                        return
                    await self.sleep(300)
                    continue
                self._run_now = False

                await self.run_cycle()
                if once:
                    return
                delay = random.uniform(self.s.cycle_interval_min_minutes, self.s.cycle_interval_max_minutes) * 60
                log.info("Další cyklus za %.0f min", delay / 60)
                await self.sleep(delay)
        finally:
            self.db.set_kv("worker_heartbeat", None)
            self.classifier.close()
            self.notifier.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="FB Lead Scraper – worker")
    parser.add_argument("--once", action="store_true", help="provést jeden cyklus a skončit")
    parser.add_argument("--force", action="store_true", help="ignorovat běžící worker")
    args = parser.parse_args()

    settings = load_settings()
    setup_logging(settings)
    try:
        asyncio.run(Worker(settings).run(once=args.once, force=args.force))
    except KeyboardInterrupt:
        log.info("Ukončeno uživatelem")


if __name__ == "__main__":
    main()
