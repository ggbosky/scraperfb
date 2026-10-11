"""Streamlit dashboard pro správu leadů, zdrojů, klíčových slov a workeru.

Spuštění:  streamlit run app.py
"""
from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from analyzer import KeywordPrefilter, LLMClassifier, LLMError, decide
from config import LEAD_STATUSES, load_settings
from database import Database, from_iso, utcnow
from notifier import Notifier

st.set_page_config(page_title="FB Lead Hunter", page_icon="🎯", layout="wide")

settings = load_settings()
TZ = ZoneInfo(settings.timezone)


@st.cache_resource
def get_db() -> Database:
    db = Database(settings.db_path)
    db.seed_defaults()
    return db


db = get_db()


def fmt_dt(value: str | None) -> str:
    dt = from_iso(value)
    return dt.astimezone(TZ).strftime("%d.%m. %H:%M") if dt else "—"


def worker_status() -> tuple[str, str]:
    hb = from_iso(db.get_kv("worker_heartbeat"))
    if db.get_kv("paused") == "1":
        return "⏸️ Pozastaven", db.get_kv("pause_reason") or "ručně"
    cooldown = from_iso(db.get_kv("cooldown_until"))
    if cooldown and cooldown > utcnow():
        return "🧊 Cooldown", f"do {fmt_dt(db.get_kv('cooldown_until'))}"
    if hb and (utcnow() - hb).total_seconds() < 180:
        return "🟢 Běží", f"PID {db.get_kv('worker_pid')}"
    return "⚪ Neběží", "spusť `python main.py`"


# ==========================================================================
# Sidebar
# ==========================================================================
with st.sidebar:
    st.title("🎯 FB Lead Hunter")
    status, detail = worker_status()
    st.metric("Worker", status, help=detail)
    st.caption(detail)

    col_a, col_b = st.columns(2)
    if db.get_kv("paused") == "1":
        if col_a.button("▶️ Obnovit", type="primary"):
            db.set_kv("paused", None)
            db.set_kv("pause_reason", None)
            st.rerun()
    elif col_a.button("⏸️ Pozastavit"):
        db.set_kv("paused", "1")
        db.set_kv("pause_reason", "pozastaveno z dashboardu")
        st.rerun()
    if col_b.button("⚡ Spustit teď", help="Worker přeskočí čekání a spustí cyklus hned"):
        db.set_kv("run_now", "1")
        st.toast("Požadavek odeslán workeru")

    stats = db.stats()
    st.divider()
    st.metric("Leady celkem", stats["leads"], delta=f"+{stats['leads_24h']} za 24 h")
    st.metric("Nekontaktováno", stats["by_status"].get("Nekontaktováno", 0))
    st.metric("Zpracovaných příspěvků", stats["total_posts"])
    loads_h = db.count_page_loads(utcnow() - timedelta(hours=1))
    loads_d = db.count_page_loads(utcnow() - timedelta(days=1))
    st.caption(f"Načtení stránek: {loads_h}/{settings.max_page_loads_per_hour} za hodinu · "
               f"{loads_d}/{settings.max_page_loads_per_day} za den")


tab_leads, tab_audit, tab_sources, tab_keywords, tab_system = st.tabs(
    ["🎯 Leady", "🔍 Audit příspěvků", "👥 Skupiny & hledání", "🔑 Klíčová slova", "⚙️ Systém"]
)

# ==========================================================================
# Leady
# ==========================================================================
with tab_leads:
    f1, f2, f3, f4 = st.columns([2, 1, 1, 2])
    statuses = f1.multiselect("Stav", LEAD_STATUSES, default=["Nekontaktováno", "Kontaktováno", "Odpověděli"])
    min_score = f2.slider("Min. skóre", 1, 10, 1)
    days = f3.selectbox("Období", [1, 3, 7, 14, 30, 90, 3650], index=3,
                        format_func=lambda d: "vše" if d == 3650 else f"{d} dní")
    search = f4.text_input("Hledat v textu / autorovi / skupině")

    leads = db.get_leads(statuses=statuses or None, min_score=min_score,
                         since=utcnow() - timedelta(days=days), search=search.strip())
    if not leads:
        st.info("Zatím žádné leady pro zvolený filtr. Worker je posílá i na Telegram/Discord hned po nalezení.")
    else:
        df = pd.DataFrame([{
            "id": r["id"],
            "Kdy": fmt_dt(r["posted_at"] or r["scraped_at"]),
            "Skóre": r["score"],
            "Shrnutí": r["summary"] or r["text"][:120],
            "Stav": r["lead_status"],
            "Odkaz": r["url"],
            "Rozpočet": r["budget"] or "",
            "Termín": r["deadline"] or "",
            "Zdroj": r["source_name"] or "",
            "Autor": r["author"] or "",
            "Platforma": r["platform"] or "",
            "Poznámka": r["notes"] or "",
        } for r in leads]).set_index("id")

        edited = st.data_editor(
            df,
            key="leads_editor",
            hide_index=True,
            height=min(600, 38 + 35 * len(df)),
            disabled=[c for c in df.columns if c not in ("Stav", "Poznámka")],
            column_config={
                "Skóre": st.column_config.ProgressColumn("Skóre", min_value=0, max_value=10, format="%d",
                                                         width="small"),
                "Odkaz": st.column_config.LinkColumn("Příspěvek", display_text="Otevřít ↗"),
                "Stav": st.column_config.SelectboxColumn("Stav", options=LEAD_STATUSES, required=True),
                "Shrnutí": st.column_config.TextColumn(width="medium"),
            },
        )
        changed = edited[(edited["Stav"] != df["Stav"]) | (edited["Poznámka"] != df["Poznámka"])]
        if not changed.empty:
            if st.button(f"💾 Uložit změny ({len(changed)})", type="primary"):
                for post_id, row in changed.iterrows():
                    db.update_lead_status(int(post_id), row["Stav"], row["Poznámka"])
                st.success("Uloženo")
                st.rerun()

        st.subheader("Detail leadu")
        by_id = {r["id"]: r for r in leads}
        sel = st.selectbox("Vyber lead", list(by_id),
                           format_func=lambda i: f"[{by_id[i]['score']}/10] {fmt_dt(by_id[i]['posted_at'])} – "
                                                 f"{(by_id[i]['summary'] or by_id[i]['text'])[:90]}")
        if sel:
            r = by_id[sel]
            c1, c2 = st.columns([3, 2])
            with c1:
                st.markdown(f"**{r['summary'] or ''}**")
                st.text_area("Celý text příspěvku", r["text"], height=220, disabled=True, key=f"txt_{sel}")
                if r["url"]:
                    st.link_button("👉 Otevřít příspěvek na Facebooku", r["url"], type="primary")
            with c2:
                st.markdown(
                    f"- **Autor:** {r['author'] or '—'}\n"
                    f"- **Zdroj:** {r['source_name'] or '—'}\n"
                    f"- **Publikováno:** {fmt_dt(r['posted_at'])} · nalezeno {fmt_dt(r['scraped_at'])}\n"
                    f"- **Typ / platforma:** {r['project_type'] or '—'} / {r['platform'] or '—'}\n"
                    f"- **Rozpočet:** {r['budget'] or '—'} · **Termín:** {r['deadline'] or '—'}\n"
                    f"- **Urgence:** {r['urgency'] or '—'} · **Lokalita:** {r['location'] or '—'}\n"
                    f"- **Zdůvodnění LLM:** _{r['reasoning'] or '—'}_"
                )
                new_status = st.radio("Stav", LEAD_STATUSES, index=LEAD_STATUSES.index(r["lead_status"]),
                                      horizontal=True, key=f"st_{sel}")
                notes = st.text_area("Poznámka", r["notes"] or "", key=f"nt_{sel}")
                b1, b2 = st.columns(2)
                if b1.button("Uložit", key=f"save_{sel}"):
                    db.update_lead_status(sel, new_status, notes)
                    st.rerun()
                if b2.button("🚫 Není to lead", key=f"nolead_{sel}",
                             help="Označí jako falešně pozitivní (zmizí z přehledu)"):
                    db.set_manual_lead(sel, False)
                    st.rerun()

# ==========================================================================
# Audit – všechny zpracované příspěvky (ladění filtrů, false negatives)
# ==========================================================================
with tab_audit:
    st.caption("Všechny stažené příspěvky včetně zamítnutých – slouží k ladění klíčových slov a promptu.")
    a1, a2, a3 = st.columns([1, 2, 1])
    only_rejected = a1.toggle("Jen zamítnuté", value=True)
    audit_search = a2.text_input("Hledat v textu", key="audit_search")
    limit = a3.selectbox("Počet", [100, 300, 1000], index=1)
    rows = db.get_posts(only_rejected=only_rejected, limit=limit, search=audit_search.strip())
    if rows:
        adf = pd.DataFrame([{
            "id": r["id"],
            "Publikováno": fmt_dt(r["posted_at"]),
            "Důvod": r["reject_reason"] or ("LEAD" if r["is_lead"] else r["analysis_status"]),
            "Záměr": r["intent"] or "",
            "Skóre": r["score"] if r["score"] is not None else "",
            "Text": r["text"][:200].replace("\n", " "),
            "Zdroj": r["source_name"] or "",
            "Odkaz": r["url"],
        } for r in rows])
        reasons = sorted(adf["Důvod"].dropna().unique())
        pick = st.multiselect("Filtrovat podle důvodu", reasons)
        if pick:
            adf = adf[adf["Důvod"].isin(pick)]
        st.dataframe(adf, hide_index=True, height=min(450, 38 + 35 * len(adf)), column_config={
            "Odkaz": st.column_config.LinkColumn("Příspěvek", display_text="↗"),
            "Text": st.column_config.TextColumn(width="large"),
        })
        st.markdown("**Ruční oprava (false negative)**")
        m1, m2 = st.columns([1, 3])
        manual_id = m1.number_input("ID příspěvku", min_value=1, step=1, value=None)
        if manual_id and m2.button("✅ Označit jako lead"):
            db.set_manual_lead(int(manual_id), True)
            st.success(f"Příspěvek {manual_id} přesunut mezi leady")
    else:
        st.info("Zatím žádná data.")

# ==========================================================================
# Zdroje
# ==========================================================================
with tab_sources:
    st.subheader("Sledované FB skupiny")
    st.caption("Účet musí být členem skupiny (u soukromých). Feed se čte seřazený od nejnovějších.")
    with st.form("add_group", clear_on_submit=True):
        g1, g2, g3 = st.columns([2, 3, 1])
        g_name = g1.text_input("Název", placeholder="Podnikatelky CZ")
        g_url = g2.text_input("URL skupiny", placeholder="https://www.facebook.com/groups/123456789/")
        g3.write("")
        if g3.form_submit_button("➕ Přidat", type="primary"):
            if "facebook.com/groups/" not in g_url:
                st.error("Zadej URL ve tvaru https://www.facebook.com/groups/<id nebo název>/")
            else:
                db.add_source(g_name or g_url, g_url)
                st.success("Skupina přidána")
                st.rerun()

    sources = db.list_sources()
    if sources:
        sdf = pd.DataFrame([{
            "id": s["id"], "Aktivní": bool(s["enabled"]), "Název": s["name"], "URL": s["url"],
            "Naposledy": fmt_dt(s["last_scraped_at"]), "Chyba": s["last_error"] or "",
            "Smazat": False,
        } for s in sources]).set_index("id")
        sedit = st.data_editor(sdf, key="src_editor", hide_index=True,
                               disabled=["Naposledy", "Chyba"],
                               column_config={"URL": st.column_config.LinkColumn("URL")})
        if st.button("💾 Uložit skupiny"):
            for sid, row in sedit.iterrows():
                if row["Smazat"]:
                    db.delete_source(int(sid))
                else:
                    db.update_source(int(sid), enabled=int(row["Aktivní"]), name=row["Název"], url=row["URL"])
            st.success("Uloženo")
            st.rerun()
    else:
        st.info("Zatím žádné skupiny. Přidej první výše.")

    st.subheader("FB Search dotazy")
    st.caption(f"Každý cyklus se náhodně vybere {settings.max_search_queries_per_cycle} dotazů "
               "(filtr „Nejnovější příspěvky“). Jeden dotaz na řádek.")
    q_text = st.text_area("Dotazy", "\n".join(db.get_keywords("search")), height=180, key="queries")
    if st.button("💾 Uložit dotazy"):
        db.set_keywords("search", q_text.splitlines())
        st.success("Uloženo")

# ==========================================================================
# Klíčová slova
# ==========================================================================
with tab_keywords:
    st.markdown(
        "Předfiltr pustí příspěvek do LLM jen když obsahuje **téma** a zároveň **poptávku** "
        "(nebo otazník). **Negativní** fráze (nabídky služeb) příspěvek zahodí, pokud v něm není silná "
        "poptávka typu *hledám / poptávám / sháním*. Porovnává se bez diakritiky, jako začátek slova "
        "(`web` = web, webu, webovky…). Jedna fráze na řádek."
    )
    k1, k2, k3 = st.columns(3)
    topic = k1.text_area("🧩 Téma", "\n".join(db.get_keywords("topic")), height=420)
    demand = k2.text_area("🙋 Poptávka", "\n".join(db.get_keywords("demand")), height=420)
    negative = k3.text_area("🚫 Negativní (nabídky / spam)", "\n".join(db.get_keywords("negative")), height=420)
    if st.button("💾 Uložit klíčová slova", type="primary"):
        db.set_keywords("topic", topic.splitlines())
        db.set_keywords("demand", demand.splitlines())
        db.set_keywords("negative", negative.splitlines())
        st.success("Uloženo – worker je použije od příštího cyklu.")

    st.divider()
    st.subheader("🧪 Otestovat předfiltr")
    sample = st.text_area("Vlož text příspěvku", key="pf_sample",
                          placeholder="Ahoj, sháním někoho na předělání webu na WordPressu…")
    if sample:
        res = KeywordPrefilter(topic.splitlines(), demand.splitlines(), negative.splitlines()).check(sample)
        (st.success if res.passed else st.warning)(
            f"{'✅ Prošlo do LLM' if res.passed else '❌ Zamítnuto'} – důvod: `{res.reason}`")
        st.json({"téma": res.topic_hits, "poptávka": res.demand_hits, "negativní": res.negative_hits})

# ==========================================================================
# Systém
# ==========================================================================
with tab_system:
    s1, s2 = st.columns(2)
    with s1:
        st.subheader("🤖 Test LLM klasifikátoru")
        st.caption(f"Poskytovatel: **{settings.llm_provider}** · model: `{settings.llm_model}` · "
                   f"lead = skóre > {settings.min_relevance_score}")
        llm_text = st.text_area("Text příspěvku", key="llm_sample", height=150,
                                placeholder="Dobrý den, poptávám tvorbu e-shopu na Shoptetu, rozpočet 30 tis…")
        if st.button("Klasifikovat") and llm_text.strip():
            clf = LLMClassifier(settings)
            try:
                with st.spinner("Ptám se LLM…"):
                    result = clf.classify(llm_text)
                is_lead, reason = decide(result, utcnow(), settings)
                (st.success if is_lead else st.warning)(
                    "🔥 Byl by to LEAD" if is_lead else f"Není lead ({reason})")
                st.json(result)
            except LLMError as exc:
                st.error(f"LLM chyba: {exc}")
            finally:
                clf.close()
    with s2:
        st.subheader("🔔 Test notifikací")
        notifier = Notifier(settings)
        st.write(f"Telegram: {'✅' if settings.telegram_bot_token and settings.telegram_chat_id else '❌ nenastaven'}"
                 f" · Discord: {'✅' if settings.discord_webhook_url else '❌ nenastaven'}")
        if st.button("Poslat testovací lead"):
            ok = notifier.send_lead({
                "id": 0, "score": 10, "source_name": "Testovací skupina", "author": "Jan Novák",
                "posted_at": None, "budget": "do 20 000 Kč", "deadline": "do konce měsíce",
                "urgency": "stredni", "project_type": "redesign", "platform": "WordPress",
                "summary": "Testovací notifikace – redesign webu autoservisu.",
                "text": "Dobrý den, sháníme někoho na redesign webu našeho autoservisu…",
                "url": "https://www.facebook.com/",
            })
            if ok:
                st.success("Odesláno")
            else:
                st.error("Odeslání selhalo – zkontroluj .env a log")
        notifier.close()

        st.subheader("🧊 Blokace / cooldown")
        if db.get_kv("cooldown_until"):
            st.write(f"Cooldown do: {fmt_dt(db.get_kv('cooldown_until'))}")
            if st.button("Zrušit cooldown"):
                db.set_kv("cooldown_until", None)
                st.rerun()
        else:
            st.write("Žádný aktivní cooldown.")

    st.subheader("📜 Poslední běhy workeru")
    runs = db.recent_runs()
    if runs:
        st.dataframe(pd.DataFrame([{
            "Start": fmt_dt(r["started_at"]), "Konec": fmt_dt(r["finished_at"]),
            "Zdroje": r["sources_scraped"], "Nalezeno": r["posts_found"], "Nové": r["posts_new"],
            "LLM volání": r["llm_calls"], "Leady": r["leads_found"], "Chyba": r["error"] or "",
        } for r in runs]), hide_index=True)

    with st.expander("Aktuální konfigurace (.env)"):
        st.json({
            "headless": settings.headless, "browser_channel": settings.browser_channel or "chromium",
            "active_hours": f"{settings.active_hours_start}–{settings.active_hours_end}",
            "cycle_interval_min": f"{settings.cycle_interval_min_minutes}–{settings.cycle_interval_max_minutes}",
            "max_page_loads": f"{settings.max_page_loads_per_hour}/h, {settings.max_page_loads_per_day}/den",
            "max_post_age_hours": settings.max_post_age_hours,
            "allow_unknown_age": settings.allow_unknown_age,
            "min_relevance_score (striktně >)": settings.min_relevance_score,
            "llm": f"{settings.llm_provider} / {settings.llm_model}",
            "session": str(settings.storage_state_path),
            "session_exists": settings.storage_state_path.exists(),
        })
