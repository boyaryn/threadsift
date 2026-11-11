import json
import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

CASES = [
    "post-with-replies", "reply-page", "nested-reply-page", "reply-page-chain",
    "media-post", "media-post-reply-page", "quote-post", "reply-url", "post-without-replies",
    "direct-replies-paging",
]

SECRET_MARKERS = [
    "sessionid", "csrftoken", "fb_dtsg", "\"lsd\"", "cookie", "authorization", "bearer",
    "ds_user_id", "access_token", "fbcdn", "cdninstagram", "instagram.com",
]


def documents(case: str) -> list[dict]:
    files = sorted((FIXTURES / case).glob("*.json"))
    return [json.loads(f.read_text(encoding="utf-8")) for f in files]


def walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def posts(case: str) -> list[dict]:
    return [d for doc in documents(case) for d in walk(doc["data"]) if "pk" in d and "code" in d and "caption" in d]


@pytest.mark.parametrize("case", CASES)
def test_every_case_has_payload_files_in_the_capture_format(case: str) -> None:
    files = sorted((FIXTURES / case).glob("*.json"))
    assert files, case
    for f in files:
        assert re.fullmatch(r"\d{3}-(embedded|graphql)\.json", f.name)
        doc = json.loads(f.read_text(encoding="utf-8"))
        assert set(doc) == {"url", "kind", "data"}
        assert doc["kind"] in ("embedded", "graphql")


@pytest.mark.parametrize("case", CASES)
def test_fixtures_contain_no_secrets_or_real_media_links(case: str) -> None:
    for f in (FIXTURES / case).glob("*.json"):
        text = f.read_text(encoding="utf-8").lower()
        for marker in SECRET_MARKERS:
            assert marker not in text, (f, marker)
        for url in re.findall(r"https?://[^\"\\\s]+", text):
            assert "example.invalid" in url or "threads.com" in url, (f, url[:60])


@pytest.mark.parametrize("case", CASES)
def test_identities_are_synthetic(case: str) -> None:
    for post in posts(case):
        assert re.fullmatch(r"user_\d{3,4}", post["user"]["username"])
        assert re.fullmatch(r"7\d{18}", post["pk"])


def test_first_page_of_a_post_with_replies_has_the_post_and_a_page_of_replies() -> None:
    kinds = [d["kind"] for d in documents("post-with-replies")]
    assert kinds == ["embedded", "embedded"]
    found = posts("post-with-replies")
    assert len(found) >= 10
    assert any(not p["text_post_app_info"]["is_reply"] for p in found)


def test_an_edited_post_is_covered() -> None:
    assert any(p.get("caption_is_edited") for p in posts("post-with-replies"))


def test_reply_pages_come_as_target_upward_and_downward() -> None:
    for case in ("reply-page", "nested-reply-page", "reply-page-chain", "media-post-reply-page"):
        docs = documents(case)
        assert [d["kind"] for d in docs] == ["graphql"] * 3
        shapes = [
            ("containing_thread" in json.dumps(d["data"]), "direct_replies" in json.dumps(d["data"]))
            for d in docs
        ]
        assert shapes == [(False, False), (True, False), (False, True)] or sorted(shapes) == sorted(
            [(False, False), (True, False), (False, True)]
        )


def test_nested_replies_are_covered() -> None:
    chains = [
        d for doc in documents("nested-reply-page") for d in walk(doc["data"])
        if isinstance(d.get("containing_thread"), dict)
    ]
    assert chains and len(chains[0]["containing_thread"]["posts"]["edges"]) >= 3


def test_media_is_covered() -> None:
    assert any(p.get("carousel_media") for p in posts("media-post"))


def test_a_quote_is_covered() -> None:
    assert any(p["text_post_app_info"]["share_info"].get("quoted_post") for p in posts("quote-post"))


def test_a_url_pointing_to_a_reply_is_covered() -> None:
    docs = documents("reply-url")
    target = [d for d in walk(docs[0]["data"]) if "pk" in d and "code" in d and "caption" in d][0]
    assert target["text_post_app_info"]["is_reply"] is True
    assert "containing_thread" in json.dumps(docs[1]["data"])


def test_cyrillic_text_and_emoji_survive_anonymization() -> None:
    texts = [p["caption"]["text"] for case in CASES for p in posts(case) if p.get("caption")]
    assert any("☀️" in t for t in texts)
    assert any(re.search("[А-Яа-яІіЇїЄє]", t) for t in texts)


def test_batches_of_direct_replies_loaded_by_scrolling_are_covered() -> None:
    docs = documents("direct-replies-paging")
    assert [d["kind"] for d in docs] == ["embedded", "embedded", "graphql", "graphql", "graphql", "graphql"]
    pages = [
        next(d for d in walk(doc["data"]) if "direct_replies" in d)["direct_replies"]["page_info"]
        for doc in docs[2:]
    ]
    assert [p["has_next_page"] for p in pages] == [True, True, True, False]
    assert all(p["end_cursor"] in ("redacted", None) for p in pages)
