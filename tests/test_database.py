from datetime import timedelta

import sqlite3

import pytest

from database import SCHEMA, Database, PostRecord, utcnow


def _rec(key="fb:1", text="Poptávám web", h="h1"):
    return PostRecord(post_key=key, text=text, content_hash=h, url="https://www.facebook.com/groups/1/posts/1/",
                      posted_at=utcnow() - timedelta(hours=1))


def test_insert_is_deduplicated(db):
    assert db.insert_post(_rec()) is not None
    assert db.insert_post(_rec()) is None
    assert db.known_keys(["fb:1", "fb:2"]) == {"fb:1"}


def test_content_hash_lookup_only_for_analyzed(db):
    first = db.insert_post(_rec("fb:1", h="same"))
    second = db.insert_post(_rec("fb:2", h="same"))
    assert db.find_by_content_hash("same", exclude_id=second) is None  # první ještě není analyzovaný
    db.save_analysis(first, {"intent": "poptavajici", "is_active": True, "relevance_score": 9},
                     True, None, "test")
    assert db.find_by_content_hash("same", exclude_id=second)["id"] == first


def test_lead_status_flow(db):
    pid = db.insert_post(_rec())
    db.save_analysis(pid, {"intent": "poptavajici", "is_active": True, "relevance_score": 10,
                           "summary": "web"}, True, None, "test")
    assert [r["id"] for r in db.get_leads()] == [pid]
    db.update_lead_status(pid, "Kontaktováno", "napsal jsem do SZ")
    lead = db.get_post(pid)
    assert lead["lead_status"] == "Kontaktováno" and lead["notes"] == "napsal jsem do SZ"
    assert db.get_leads(statuses=["Nekontaktováno"]) == []
    with pytest.raises(ValueError):
        db.update_lead_status(pid, "Neexistuje")


def test_keywords_sources_and_page_loads(db):
    assert "poptav" in db.get_keywords("demand")
    db.set_keywords("search", ["a", "b", "a", " "])
    assert db.get_keywords("search") == ["a", "b"]
    db.add_source("Skupina", "https://www.facebook.com/groups/123/")
    db.add_source("Skupina", "https://www.facebook.com/groups/123/")
    assert len(db.list_sources()) == 1
    db.record_page_load("x")
    db.record_page_load("y")
    assert db.count_page_loads(utcnow() - timedelta(hours=1)) == 2


def test_reanalysis_queue(db):
    pid = db.insert_post(_rec())
    db.mark_analysis_error(pid, "timeout")
    assert [r["id"] for r in db.posts_for_reanalysis()] == [pid]
    db.mark_analysis_error(pid, "timeout")
    db.mark_analysis_error(pid, "timeout")
    assert db.posts_for_reanalysis(max_attempts=3) == []


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    new_cols = ("author_url", "author_id", "contact_emails", "contact_phones", "contact_websites")
    previous_schema = "\n".join(l for l in SCHEMA.splitlines() if not l.strip().startswith(new_cols))
    conn = sqlite3.connect(path)
    conn.executescript(previous_schema)
    conn.execute("INSERT INTO posts (post_key, text, content_hash, scraped_at) VALUES ('fb:1', 'x', 'h', 'now')")
    conn.commit()
    conn.close()
    Database(path)
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(posts)")}
    assert set(new_cols) <= cols
    assert sqlite3.connect(path).execute("SELECT post_key FROM posts").fetchall() == [("fb:1",)]


def test_reset_data_keeps_sources_and_keywords(db):
    db.add_source("Skupina", "https://www.facebook.com/groups/1/")
    db.insert_post(_rec())
    db.start_run()
    db.record_page_load("x")
    db.reset_data()
    assert db.stats()["total_posts"] == 0 and db.recent_runs() == []
    assert db.count_page_loads(utcnow() - timedelta(days=1)) == 0
    assert len(db.list_sources()) == 1 and db.get_keywords("topic")
