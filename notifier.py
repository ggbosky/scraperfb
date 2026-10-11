"""Okamžité notifikace o nových leadech přes Telegram bota a/nebo Discord webhook."""
from __future__ import annotations

import html
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from analyzer import format_age
from config import Settings
from database import from_iso

log = logging.getLogger(__name__)

URGENCY_LABEL = {"vysoka": "🔴 vysoká", "stredni": "🟠 střední", "nizka": "🟢 nízká"}


def messenger_url(author_id: str | None) -> str | None:
    """Přímý odkaz na konverzaci s autorem v Messengeru (funguje s číselným ID profilu)."""
    return f"https://www.facebook.com/messages/t/{author_id}" if author_id else None


def contact_url(lead: dict[str, Any]) -> str | None:
    """Kam autorovi napsat: uložený odkaz zdroje, jinak Messenger podle ID profilu."""
    return lead.get("message_url") or messenger_url(lead.get("author_id"))


def contact_label(lead: dict[str, Any]) -> str:
    return {"freelancer": "Podat nabídku", "reddit": "Napsat na Redditu"}.get(
        lead.get("source_type") or "", "Napsat zprávu")


def _snippet(text: str, limit: int = 600) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


class Notifier:
    def __init__(self, settings: Settings):
        self.s = settings
        self._client = httpx.Client(timeout=15)

    @property
    def enabled(self) -> bool:
        return bool((self.s.telegram_bot_token and self.s.telegram_chat_id) or self.s.discord_webhook_url)

    def close(self) -> None:
        self._client.close()

    # ---------------------------------------------------------------- public
    def send_lead(self, lead: dict[str, Any]) -> bool:
        """Pošle lead do všech nakonfigurovaných kanálů. Vrací True, pokud aspoň jeden uspěl."""
        ok = False
        if self.s.telegram_bot_token and self.s.telegram_chat_id:
            ok |= self._telegram(self._format_telegram(lead), buttons=self._buttons(lead))
        if self.s.discord_webhook_url:
            ok |= self._discord({"embeds": [self._format_discord(lead)]})
        if not self.enabled:
            log.info("Notifikace nejsou nakonfigurované – lead %s jen uložen do DB", lead.get("id"))
        return ok

    def send_alert(self, message: str) -> bool:
        """Systémové upozornění (vypršelá session, checkpoint, blokace…)."""
        ok = False
        if self.s.telegram_bot_token and self.s.telegram_chat_id:
            ok |= self._telegram(f"⚠️ <b>FB Lead Scraper</b>\n{html.escape(message)}")
        if self.s.discord_webhook_url:
            ok |= self._discord({"content": f"⚠️ **FB Lead Scraper**: {message}"})
        return ok

    # ------------------------------------------------------------ formatting
    @staticmethod
    def _format_telegram(lead: dict[str, Any]) -> str:
        e = html.escape
        posted_at = from_iso(lead.get("posted_at"))
        author = e(lead.get("author") or "neznámý autor")
        if lead.get("author_url"):
            author = f'<a href="{e(lead["author_url"], quote=True)}">{author}</a>'
        lines = [
            f"🔥 <b>Nová poptávka</b> · skóre <b>{lead.get('score')}/10</b>",
            f"📍 {e(lead.get('source_name') or '—')}",
            f"👤 {author} · 🕒 před {format_age(posted_at)}"
            + ("" if posted_at else " ⚠️ stáří neověřeno"),
        ]
        for key, icon in (("contact_phones", "📞"), ("contact_emails", "📧"), ("contact_websites", "🌐")):
            if lead.get(key):
                lines.append(f"{icon} {e(lead[key])}")
        details = []
        if lead.get("budget"):
            details.append(f"💰 {e(lead['budget'])}")
        if lead.get("deadline"):
            details.append(f"⏰ {e(lead['deadline'])}")
        if lead.get("urgency") in URGENCY_LABEL:
            details.append(f"⚡ {URGENCY_LABEL[lead['urgency']]}")
        if details:
            lines.append(" · ".join(details))
        tech = " · ".join(e(x) for x in (lead.get("project_type"), lead.get("platform")) if x)
        if tech:
            lines.append(f"🧩 {tech}")
        if lead.get("summary"):
            lines.append(f"\n<b>{e(lead['summary'])}</b>")
        lines.append(f"\n<i>„{e(_snippet(lead.get('text') or ''))}“</i>")
        return "\n".join(lines)

    @staticmethod
    def _buttons(lead: dict[str, Any]) -> list[dict[str, str]]:
        buttons = []
        if lead.get("url"):
            buttons.append({"text": "👉 Příspěvek", "url": lead["url"]})
        if msg := contact_url(lead):
            buttons.append({"text": f"💬 {contact_label(lead)}", "url": msg})
        elif lead.get("author_url"):
            buttons.append({"text": "👤 Profil autora", "url": lead["author_url"]})
        return buttons

    @staticmethod
    def _format_discord(lead: dict[str, Any]) -> dict[str, Any]:
        posted_at = from_iso(lead.get("posted_at"))
        fields = [
            {"name": "Skóre", "value": f"{lead.get('score')}/10", "inline": True},
            {"name": "Zdroj", "value": (lead.get("source_name") or "—")[:256], "inline": True},
            {"name": "Stáří", "value": f"před {format_age(posted_at)}", "inline": True},
        ]
        author = lead.get("author") or "neznámý"
        if lead.get("author_url"):
            author = f"[{author}]({lead['author_url']})"
        if msg := contact_url(lead):
            author += f" · [💬 {contact_label(lead).lower()}]({msg})"
        fields.append({"name": "👤 Autor", "value": author[:1024], "inline": False})
        for key, label in (("contact_phones", "📞 Telefon"), ("contact_emails", "📧 E-mail"),
                           ("contact_websites", "🌐 Web"), ("budget", "💰 Rozpočet"),
                           ("deadline", "⏰ Termín"), ("platform", "🧩 Platforma"),
                           ("project_type", "Typ")):
            if lead.get(key):
                fields.append({"name": label, "value": str(lead[key])[:1024], "inline": True})
        embed: dict[str, Any] = {
            "title": f"🔥 {(lead.get('summary') or 'Nová poptávka')[:240]}",
            "description": _snippet(lead.get("text") or "", 1500),
            "color": 0xE8590C,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if lead.get("url"):
            embed["url"] = lead["url"]
        return embed

    # ------------------------------------------------------------- transport
    def _telegram(self, text: str, buttons: list[dict[str, str]] | None = None) -> bool:
        payload: dict[str, Any] = {
            "chat_id": self.s.telegram_chat_id,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [buttons]}
        endpoint = f"https://api.telegram.org/bot{self.s.telegram_bot_token}/sendMessage"
        return self._post_with_retry(endpoint, payload, "Telegram")

    def _discord(self, payload: dict[str, Any]) -> bool:
        return self._post_with_retry(self.s.discord_webhook_url, payload, "Discord")

    def _post_with_retry(self, url: str, payload: dict[str, Any], name: str, retries: int = 3) -> bool:
        for attempt in range(retries):
            try:
                resp = self._client.post(url, json=payload)
                if resp.status_code == 429:
                    data = resp.json() if resp.content else {}
                    delay = (data.get("parameters", {}).get("retry_after")  # Telegram
                             or data.get("retry_after")                    # Discord
                             or 2 ** (attempt + 1))
                    time.sleep(min(float(delay), 60))
                    continue
                if resp.status_code >= 400:
                    log.error("%s notifikace selhala: HTTP %s %s", name, resp.status_code, resp.text[:300])
                    return False
                return True
            except httpx.HTTPError as exc:
                log.warning("%s notifikace – chyba sítě (%s), pokus %d", name, exc, attempt + 1)
                time.sleep(2 ** (attempt + 1))
        return False
