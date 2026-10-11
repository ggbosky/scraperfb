from datetime import timedelta

import pytest

from analyzer import (
    KeywordPrefilter, LLMError, content_hash, decide, extract_contacts, extract_json, normalize_analysis,
)
from config import DEFAULT_DEMAND_KEYWORDS, DEFAULT_NEGATIVE_KEYWORDS, DEFAULT_TOPIC_KEYWORDS
from database import utcnow


@pytest.fixture
def prefilter():
    return KeywordPrefilter(DEFAULT_TOPIC_KEYWORDS, DEFAULT_DEMAND_KEYWORDS, DEFAULT_NEGATIVE_KEYWORDS)


@pytest.mark.parametrize("text", [
    "Dobrý den, poptávám tvorbu webu pro naši kavárnu v Brně.",
    "Sháním někoho, kdo mi předělá e-shop na Shoptetu, spěchá to.",
    "Doporučíte prosím šikovného webaře na menší firemní web?",
    "Hledám webaře, nabízím 15 000 Kč za jednoduchý web.",
    "Potřebovala bych upravit web na WordPressu, umí to někdo?",
    "Kolik by stál redesign stránek pro autoservis?",
    "Looking for a web designer to redo our dental clinic website, budget $2k.",
    "Need a web designer for my bakery, DM me!",
    "Can anyone recommend a Shopify developer? Need help with my store.",
    "ISO someone to build a simple website for my cleaning business?",
])
def test_prefilter_passes_demand(prefilter, text):
    res = prefilter.check(text)
    assert res.passed, res


@pytest.mark.parametrize("text,reason", [
    ("Dělám weby na zakázku, pište do SZ 🙂 levně a rychle!", "offer_pattern"),
    ("Hledám práci jako webdesignér, mám portfolio a praxi.", "offer_pattern"),
    ("Hledám nové klienty na tvorbu webů, první konzultace zdarma.", "offer_pattern"),
    ("Prodám dětský kočárek, skoro nepoužívaný, Praha 5.", "no_topic"),
    ("Vydělávej s bitcoin! Web s návodem v komentáři, hledám partnery.", "spam_pattern"),
    ("Krásný web, gratuluji k novému e-shopu.", "no_demand"),
    ("web", "too_short"),
    ("I will build a stunning website for your business, check my portfolio!", "offer_pattern"),
    ("Need a website? I build fast modern sites starting at $299. DM me", "offer_pattern"),
    ("Web developer looking for new clients, open for work 🙂", "offer_pattern"),
    ("Make money online from your website, work from home!", "spam_pattern"),
])
def test_prefilter_rejects(prefilter, text, reason):
    res = prefilter.check(text)
    assert not res.passed
    assert res.reason == reason


def test_content_hash_ignores_diacritics_urls_and_emoji():
    a = "Poptávám tvorbu webu!! 🙏 https://example.com/x"
    b = "poptavam   tvorbu webu"
    assert content_hash(a) == content_hash(b)


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json('Jasně, tady je výsledek: {"a": 3} hotovo') == {"a": 3}
    with pytest.raises(LLMError):
        extract_json("tohle není json")


def test_normalize_analysis_coerces_types():
    out = normalize_analysis({"intent": "Poptávající", "is_active": "true", "relevance_score": "9.4",
                              "budget": "null", "urgency": "Vysoká", "summary": "x"})
    assert out["intent"] == "poptavajici"
    assert out["is_active"] is True
    assert out["relevance_score"] == 9
    assert out["budget"] is None
    assert out["urgency"] == "vysoka"


def test_normalize_analysis_offer_never_active():
    out = normalize_analysis({"intent": "nabizejici", "is_active": True, "relevance_score": 15})
    assert out["is_active"] is False
    assert out["relevance_score"] == 10


def _analysis(**kw):
    base = {"intent": "poptavajici", "is_active": True, "relevance_score": 9, "project_type": "novy_web"}
    base.update(kw)
    return normalize_analysis(base)


def test_decide(settings):
    now = utcnow()
    assert decide(_analysis(), now - timedelta(hours=1), settings) == (True, None)
    assert decide(_analysis(relevance_score=8), now, settings)[1] == "low_score:8"
    assert decide(_analysis(intent="nabizejici"), now, settings)[1] == "intent:nabizejici"
    assert decide(_analysis(is_active=False), now, settings)[1] == "inactive"
    assert decide(_analysis(), now - timedelta(days=10), settings)[1] == "too_old"
    assert decide(_analysis(), None, settings)[0] is True  # ALLOW_UNKNOWN_AGE=true
    assert decide(_analysis(project_type="Landing page"), now, settings) == (True, None)
    assert decide(_analysis(project_type="redesign"), now, settings) == (True, None)
    assert decide(_analysis(project_type="uprava"), now, settings) == (True, None)
    assert decide(_analysis(project_type="jine"), now, settings)[1] == "project_type:jine"
    assert decide(_analysis(project_type=None), now, settings)[1] == "project_type:neuvedeno"


def test_extract_contacts():
    text = ("Pište na Jana.Novakova@seznam.cz nebo volejte +420 777 123 456, případně 602-111-222. "
            "Stávající web www.salon-krasa.cz a kavarna.cz/menu. Rozpočet 20 000 Kč, IČO 12345678, "
            "skupina https://www.facebook.com/groups/1/")
    c = extract_contacts(text)
    assert c["emails"] == ["jana.novakova@seznam.cz"]
    assert c["phones"] == ["+420 777 123 456", "602-111-222"]
    assert c["websites"] == ["www.salon-krasa.cz", "kavarna.cz/menu"]
    assert extract_contacts("Rozpočet 150 000 Kč, do 15. 3.") == {"emails": [], "phones": [], "websites": []}


def test_extract_international_phones():
    c = extract_contacts("Call +1 (415) 555-0132 or 415-555-0199. UK office +44 20 7946 0958. Budget $1,500 by 12/15.")
    assert c["phones"] == ["+1 (415) 555-0132", "+44 20 7946 0958", "415-555-0199"]


def test_marketplace_projects_need_only_topic(prefilter):
    text = "Corporate website development\n\nWe want a modern 10 page site with a blog. " + "Details. " * 900
    assert not prefilter.check(text).passed
    assert prefilter.check(text, marketplace=True).passed
    assert prefilter.check("Logo design for a bakery, vector files needed.", marketplace=True).reason == "no_topic"
