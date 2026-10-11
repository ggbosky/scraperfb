from datetime import timedelta

from analyzer import LLMError, normalize_analysis
from database import utcnow
from main import Pipeline
from scraper import ScrapedPost


class FakeClassifier:
    model = "fake"

    def __init__(self, result=None, fail=False):
        self.result = result or {"intent": "poptavajici", "is_active": True, "relevance_score": 10,
                                 "project_type": "novy_web", "summary": "Nový web pro kavárnu"}
        self.fail = fail
        self.calls = 0

    def classify(self, text, **kw):
        self.calls += 1
        if self.fail:
            raise LLMError("down")
        return normalize_analysis(self.result)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send_lead(self, lead):
        self.sent.append(lead)
        return True


def _post(pid, text, hours_ago=1, source="Skupina A"):
    return ScrapedPost(text=text, post_id=pid, url=f"https://www.facebook.com/groups/1/posts/{pid}/",
                       posted_at=utcnow() - timedelta(hours=hours_ago), source_name=source, origin="graphql",
                       author="Jana", author_id="1000123",
                       author_url="https://www.facebook.com/profile.php?id=1000123")


def test_lead_is_saved_and_notified(db, settings):
    clf, notif = FakeClassifier(), FakeNotifier()
    p = Pipeline(db, settings, clf, notif)
    assert p.handle(_post("1", "Dobrý den, poptávám tvorbu webu pro kavárnu, rozpočet 20 tis.")) == "lead"
    assert p.handle(_post("1", "Dobrý den, poptávám tvorbu webu pro kavárnu, rozpočet 20 tis.")) == "known"
    assert len(notif.sent) == 1 and notif.sent[0]["notified_at"] is None
    assert db.get_leads()[0]["notified_at"] is not None
    assert p.stats.leads_found == 1 and p.stats.posts_new == 1


def test_lead_keeps_author_and_contacts(db, settings):
    notif = FakeNotifier()
    p = Pipeline(db, settings, FakeClassifier(), notif)
    p.handle(_post("5", "Poptávám nový web pro autoservis, volejte 777 123 456 nebo servis@example.cz"))
    lead = notif.sent[0]
    assert lead["author_id"] == "1000123" and lead["author_url"].endswith("id=1000123")
    assert lead["contact_phones"] == "777 123 456" and lead["contact_emails"] == "servis@example.cz"


def test_small_fixes_are_not_leads_by_default(db, settings):
    notif = FakeNotifier()
    clf = FakeClassifier({"intent": "poptavajici", "is_active": True, "relevance_score": 10,
                          "project_type": "uprava"})
    p = Pipeline(db, settings, clf, notif)
    assert p.handle(_post("6", "Potřebuji opravit kontaktní formulář na webu, kdo pomůže?")) == "project_type:uprava"
    assert notif.sent == []


def test_offer_and_old_posts_never_reach_llm(db, settings):
    clf, notif = FakeClassifier(), FakeNotifier()
    p = Pipeline(db, settings, clf, notif)
    assert p.handle(_post("2", "Dělám weby na zakázku, pište do SZ, volné kapacity!")) == "prefilter:offer_pattern"
    assert p.handle(_post("3", "Poptávám tvorbu webu", hours_ago=200)) == "too_old"
    assert clf.calls == 0 and notif.sent == []


def test_crosspost_is_detected_as_duplicate(db, settings):
    clf, notif = FakeClassifier(), FakeNotifier()
    p = Pipeline(db, settings, clf, notif)
    text = "Sháním webaře na redesign webu autoservisu, spěchá!"
    assert p.handle(_post("10", text, source="Skupina A")) == "lead"
    assert p.handle(_post("11", text + " 🙏", source="Skupina B")) == "duplicate"
    assert clf.calls == 1 and len(notif.sent) == 1


def test_low_score_and_llm_error_with_retry(db, settings):
    notif = FakeNotifier()
    p = Pipeline(db, settings, FakeClassifier({"intent": "poptavajici", "is_active": True,
                                               "relevance_score": 8, "project_type": "novy_web"}), notif)
    assert p.handle(_post("20", "Hledám někoho na úpravu webu, kolik by to stálo?")) == "low_score:8"

    p.classifier = FakeClassifier(fail=True)
    assert p.handle(_post("21", "Poptávám e-shop na Shoptetu pro prodej kávy.")) == "llm_error"
    p.classifier = FakeClassifier()
    p.reanalyze_failed()
    assert [l["post_key"] for l in db.get_leads()] == ["fb:21"]
