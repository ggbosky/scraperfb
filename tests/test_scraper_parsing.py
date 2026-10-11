import base64
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from scraper import (
    canonical_post_url, canonical_profile_url, convert_cookies, extract_post_id, extract_profile_id,
    extract_stories, group_feed_url, parse_fb_time, parse_graphql_payload, search_posts_url,
)

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("url,expected", [
    ("https://www.facebook.com/groups/webari/posts/1234567890/?__cft__[0]=abc", "1234567890"),
    ("https://www.facebook.com/groups/123/permalink/555/", "555"),
    ("https://www.facebook.com/permalink.php?story_fbid=pfbid02abc&id=100", "pfbid02abc"),
    ("https://www.facebook.com/jan.novak/posts/pfbid0xyz?__tn__=R", "pfbid0xyz"),
    ("https://www.facebook.com/groups/123/?multi_permalinks=777", "777"),
    ("https://www.facebook.com/groups/123/", None),
])
def test_extract_post_id(url, expected):
    assert extract_post_id(url) == expected


def test_canonical_post_url_strips_tracking():
    assert canonical_post_url("/groups/1/posts/2?__cft__[0]=x&__tn__=y") == \
        "https://www.facebook.com/groups/1/posts/2/"
    assert canonical_post_url("https://m.facebook.com/permalink.php?story_fbid=5&id=6&ref=a") == \
        "https://www.facebook.com/permalink.php?story_fbid=5&id=6"


def test_group_feed_url_is_chronological():
    assert group_feed_url("https://www.facebook.com/groups/webari/about") == \
        "https://www.facebook.com/groups/webari/?sorting_setting=CHRONOLOGICAL"
    assert group_feed_url("123456") == "https://www.facebook.com/groups/123456/?sorting_setting=CHRONOLOGICAL"


def test_search_url_uses_recent_posts_filter():
    url = search_posts_url("hledám webaře")
    qs = parse_qs(urlparse(url).query)
    assert qs["q"] == ["hledám webaře"]
    decoded = json.loads(base64.b64decode(unquote(qs["filters"][0])))
    assert json.loads(decoded["recent_posts:0"])["name"] == "recent_posts"


@pytest.mark.parametrize("label,delta", [
    ("Právě teď", timedelta(0)),
    ("5 min", timedelta(minutes=5)),
    ("2 h", timedelta(hours=2)),
    ("před 3 hodinami", timedelta(hours=3)),
    ("3 d", timedelta(days=3)),
    ("1 t", timedelta(weeks=1)),
    ("Just now", timedelta(0)),
    ("4 hrs", timedelta(hours=4)),
])
def test_parse_relative_time(label, delta):
    assert parse_fb_time(label, NOW) == NOW - delta


def test_parse_absolute_time():
    y = parse_fb_time("Včera v 10:15", NOW)  # 10:15 pražského času = 08:15 UTC (letní čas)
    assert y == datetime(2026, 10, 10, 8, 15, tzinfo=timezone.utc)
    d = parse_fb_time("12. října", NOW)
    assert (d.year, d.month, d.day) == (2025, 10, 12)  # budoucí datum → loňský rok
    assert parse_fb_time("nesmysl", NOW) is None


def _story(pid, text, ts, comment_text="komentář", shared=None):
    story = {
        "__typename": "Story",
        "id": f"S:{pid}",
        "post_id": pid,
        "feedback": {"comments": [{"body": {"text": comment_text}, "message": {"text": comment_text},
                                   "created_time": 1}]},
        "comet_sections": {
            "content": {"story": {"actors": [{"name": "Jana Nováková", "id": "100004567",
                                                "url": "https://www.facebook.com/groups/9/user/100004567/"}],
                                    "message": {"text": text}}},
            "context_layout": {"story": {"comet_sections": {"metadata": [{"story": {
                "creation_time": ts,
                "url": f"https://www.facebook.com/groups/9/posts/{pid}/?__cft__[0]=x"}}]}}},
        },
    }
    if shared:
        story["comet_sections"]["content"]["story"]["attached_story"] = shared
    return story


def test_extract_stories_from_graphql_like_json():
    shared = _story("999", "původní starý příspěvek", 1_500_000_000)
    payload = {"data": {"node": {"group_feed": {"edges": [
        {"node": _story("111", "Poptávám web pro kavárnu", 1_760_000_000)},
        {"node": _story("222", "Hledám webaře na Shoptet", 1_760_000_100, shared=shared)},
    ]}}}}
    stories = extract_stories(payload)
    assert set(stories) == {"111", "222", "999"}
    s = stories["222"]
    assert s["text"] == "Hledám webaře na Shoptet"
    assert s["creation_time"] == 1_760_000_100  # ne čas sdíleného příspěvku
    assert s["author"] == "Jana Nováková"
    assert s["author_id"] == "100004567"
    assert s["author_url"] == "https://www.facebook.com/profile.php?id=100004567"
    assert "/posts/222/" in s["url"]


def test_parse_graphql_payload_multiline():
    body = 'for (;;);{"a":1}\n{"b":2}\n'
    assert parse_graphql_payload(body) == [{"a": 1}, {"b": 2}]
    assert parse_graphql_payload('{"x":1}') == [{"x": 1}]


def test_convert_cookie_editor_export():
    raw = [
        {"name": "c_user", "value": "1", "domain": ".facebook.com", "path": "/", "secure": True,
         "httpOnly": False, "sameSite": "no_restriction", "expirationDate": 1999999999.5},
        {"name": "xs", "value": "abc", "domain": ".facebook.com", "secure": True, "httpOnly": True,
         "sameSite": "unspecified", "session": True},
        {"name": "other", "value": "x", "domain": ".google.com"},
    ]
    state = convert_cookies(raw)
    assert [c["name"] for c in state["cookies"]] == ["c_user", "xs"]
    assert state["cookies"][0]["sameSite"] == "None"
    assert state["cookies"][1]["expires"] == -1 and state["cookies"][1]["sameSite"] == "Lax"
    with pytest.raises(ValueError):
        convert_cookies([raw[2]])


@pytest.mark.parametrize("url,pid,canonical", [
    ("https://www.facebook.com/groups/9/user/1000123/?__cft__[0]=x", "1000123",
     "https://www.facebook.com/profile.php?id=1000123"),
    ("https://www.facebook.com/profile.php?id=555666&__tn__=R", "555666",
     "https://www.facebook.com/profile.php?id=555666"),
    ("https://www.facebook.com/jana.novakova?__cft__[0]=x", None, "https://www.facebook.com/jana.novakova/"),
    ("https://www.facebook.com/groups/9/", None, None),
    ("http://127.0.0.1:8000/#", None, None),
])
def test_profile_urls(url, pid, canonical):
    assert extract_profile_id(url) == pid
    assert canonical_profile_url(url) == canonical
