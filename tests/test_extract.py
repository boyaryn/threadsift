import json
from pathlib import Path

import pytest

from threadsift.collection.capture import Payload
from threadsift.collection.extract import ExtractionError, extract, is_post

FIXTURES = Path(__file__).parent / "fixtures"


def load(case: str) -> list[Payload]:
    docs = [json.loads(f.read_text(encoding="utf-8")) for f in sorted((FIXTURES / case).glob("*.json"))]
    return [Payload(d["url"], d["kind"], d["data"]) for d in docs]


def walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def target_code(case: str) -> str:
    """The page's own post is the first post in the first captured document."""
    return next(d["code"] for d in walk(load(case)[0].data) if is_post(d))


def raw_texts(case: str) -> dict[str, str]:
    return {str(d["pk"]): (d.get("caption") or {}).get("text") or "" for p in load(case) for d in walk(p.data) if is_post(d)}


def by_id(extraction):
    return {i.id: i for i in extraction.items}


def parent_chain(items: dict, pk: str) -> list[str]:
    chain = []
    while items[pk].parent_id is not None:
        pk = items[pk].parent_id
        chain.append(pk)
        if pk not in items:
            break
    return chain


# --- first page of a post -------------------------------------------------

def test_first_page_of_a_post_with_replies() -> None:
    result = extract(load("post-with-replies"), target_code("post-with-replies"))
    items = by_id(result)
    target = result.target

    assert (target.parent_id, target.root_id, target.depth) == (None, target.id, 0)
    assert target.reply_count == 456 and len(result.items) == 18  # the post, its own continuation and a page of replies
    replies = [i for i in result.items if i.id != target.id]
    assert all(i.root_id == target.id and i.depth >= 1 for i in replies)
    assert all(i.parent_id in items for i in replies)
    assert min(i.depth for i in replies) == 1
    assert result.more_replies[target.id] is True  # a 456-reply post pages its replies


def test_each_chain_starts_at_the_post_and_each_reply_follows_the_one_before() -> None:
    result = extract(load("post-with-replies"), target_code("post-with-replies"))
    items = by_id(result)
    for item in result.items:
        if item.parent_id is not None:
            assert item.depth == items[item.parent_id].depth + 1
    first_level = [i for i in result.items if i.parent_id == result.target.id]
    assert first_level and all(i.depth == 1 for i in first_level)


def test_authors_own_continuation_is_a_reply_to_the_post() -> None:
    result = extract(load("post-with-replies"), target_code("post-with-replies"))
    own = [i for i in result.items if i.id != result.target.id and i.author.id == result.target.author.id and i.parent_id == result.target.id]
    assert own and all(i.depth == 1 for i in own)


def test_post_without_replies() -> None:
    result = extract(load("post-without-replies"), target_code("post-without-replies"))
    assert len(result.items) == 1
    assert result.target.reply_count == 0
    assert (result.target.parent_id, result.target.depth) == (None, 0)


# --- reply pages, nested replies, URL pointing to a reply --------------------

def test_a_reply_page_brings_its_ancestors_and_replies() -> None:
    result = extract(load("reply-page"), target_code("reply-page"))
    items = by_id(result)
    target = result.target
    assert target.depth == 1 and target.parent_id == target.root_id
    assert items[target.root_id].depth == 0 and items[target.root_id].parent_id is None
    below = [i for i in result.items if i.depth and i.depth > 1]
    assert len(below) >= 7 and all(i.root_id == target.root_id for i in below)


def test_nested_replies_keep_their_parents_up_to_the_top_level_post() -> None:
    result = extract(load("nested-reply-page"), target_code("nested-reply-page"))
    items = by_id(result)
    target = result.target
    chain = parent_chain(items, target.id)
    assert len(chain) == target.depth >= 3
    assert items[chain[-1]].parent_id is None and items[chain[-1]].depth == 0
    assert [items[c].depth for c in chain] == list(range(target.depth - 1, -1, -1))
    assert all(items[c].root_id == chain[-1] for c in chain)
    reply = next(i for i in result.items if i.parent_id == target.id)
    assert reply.depth == target.depth + 1 and reply.root_id == chain[-1]


def test_url_pointing_to_a_reply_collects_it_with_the_parents_that_arrive() -> None:
    result = extract(load("reply-url"), target_code("reply-url"))
    items = by_id(result)
    assert len(result.items) == 2
    reply = result.target
    parent = items[reply.parent_id]
    assert reply.depth == 1 and reply.root_id == parent.id
    assert (parent.parent_id, parent.depth, parent.root_id) == (None, 0, parent.id)


def test_reply_page_with_only_the_target_payload_leaves_unknown_links_empty() -> None:
    target_only = load("reply-page")[:1]
    result = extract(target_only, target_code("reply-page"))
    assert len(result.items) == 1
    assert (result.target.parent_id, result.target.root_id, result.target.depth) == (None, None, None)


def test_reply_whose_parent_was_cut_off_still_records_the_parent_and_its_thread() -> None:
    result = extract(load("reply-page"), target_code("reply-page"))
    chain_reply = next(i for i in result.items if i.depth and i.depth > 2)
    kept = [i for i in result.items if i.id != chain_reply.parent_id]
    assert chain_reply.parent_id is not None and chain_reply.root_id is not None
    assert len(kept) == len(result.items) - 1  # the information stays on the reply itself


def test_chains_of_a_second_page_share_posts_with_the_first() -> None:
    merged = load("reply-page-chain") + load("nested-reply-page")
    result = extract(merged, target_code("reply-page-chain"))
    ids = [i.id for i in result.items]
    assert len(ids) == len(set(ids))


# --- content --------------------------------------------------------------

@pytest.mark.parametrize("case", ["post-with-replies", "media-post", "reply-url", "quote-post", "reply-page"])
def test_text_is_stored_exactly_as_published(case: str) -> None:
    raw = raw_texts(case)
    result = extract(load(case), target_code(case))
    assert result.items
    for item in result.items:
        assert item.text == raw[item.id]


def test_cyrillic_and_emoji_text_survives() -> None:
    texts = [i.text for case in ("post-with-replies", "media-post", "reply-url")
             for i in extract(load(case), target_code(case)).items]
    assert any("☀️" in t for t in texts)
    assert any("\n" in t for t in texts)


def test_author_fields() -> None:
    target = extract(load("post-with-replies"), target_code("post-with-replies")).target
    assert target.author.username.startswith("user_")
    assert target.author.id.startswith("9")
    assert target.author.display_name and target.author.profile_pic_url.startswith("https://example.invalid/")
    assert target.url == f"https://www.threads.com/@{target.author.username}/post/{target.code}"
    assert target.like_count and target.published_at is not None and target.published_at.tzinfo is not None


def test_carousel_media_is_stored_as_type_and_url() -> None:
    target = extract(load("media-post"), target_code("media-post")).target
    assert len(target.media) >= 2
    assert all(m.type in ("image", "video") and m.url.startswith("https://example.invalid/") for m in target.media)


def test_video_is_stored_as_a_video_not_as_its_thumbnail() -> None:
    result = extract(load("reply-url"), target_code("reply-url"))
    ancestor = by_id(result)[result.target.parent_id]
    assert [m.type for m in ancestor.media] == ["video"]


def test_post_without_media_has_none() -> None:
    assert extract(load("post-without-replies"), target_code("post-without-replies")).target.media == ()


def test_quoted_post_becomes_a_url_and_is_not_an_item() -> None:
    result = extract(load("quote-post"), target_code("quote-post"))
    assert len(result.items) == 1
    quoted = next(d for d in walk(load("quote-post")[0].data) if "quoted_post" in d and d["quoted_post"])["quoted_post"]
    assert result.target.quoted_post_url == f"https://www.threads.com/@{quoted['user']['username']}/post/{quoted['code']}"
    assert result.target.code != quoted["code"]


def test_post_without_a_quote_has_no_quoted_url() -> None:
    assert extract(load("post-without-replies"), target_code("post-without-replies")).target.quoted_post_url is None


# --- robustness -------------------------------------------------------------

def test_posts_not_connected_to_the_target_are_left_out() -> None:
    mixed = load("post-without-replies") + load("media-post") + load("quote-post")
    result = extract(mixed, target_code("post-without-replies"))
    assert [i.code for i in result.items] == [target_code("post-without-replies")]


def test_the_same_payload_twice_gives_the_same_items() -> None:
    once = extract(load("reply-page"), target_code("reply-page"))
    twice = extract(load("reply-page") * 2, target_code("reply-page"))
    assert [i.id for i in once.items] == [i.id for i in twice.items]


def test_extraction_does_not_depend_on_response_paths() -> None:
    wrapped = [Payload(p.url, p.kind, {"somewhere": {"else": [{"deeper": p.data}]}}) for p in load("reply-page")]
    original = extract(load("reply-page"), target_code("reply-page"))
    moved = extract(wrapped, target_code("reply-page"))
    assert [(i.id, i.parent_id, i.root_id, i.depth, i.text) for i in moved.items] == [
        (i.id, i.parent_id, i.root_id, i.depth, i.text) for i in original.items
    ]


def test_plain_dicts_are_accepted_as_well_as_payloads() -> None:
    raw = [p.data for p in load("post-without-replies")]
    assert len(extract(raw, target_code("post-without-replies")).items) == 1


def test_data_with_no_posts_is_a_parse_error() -> None:
    with pytest.raises(ExtractionError, match="^parse: no posts"):
        extract([Payload("u", "graphql", {"data": {"unrelated": [1, 2, {"pk": "1"}]}})], "ABC")


def test_target_missing_from_the_data_is_a_parse_error() -> None:
    with pytest.raises(ExtractionError, match="^parse: post NOPE is not"):
        extract(load("post-without-replies"), "NOPE")


def test_no_payloads_at_all_is_a_parse_error() -> None:
    with pytest.raises(ExtractionError, match="^parse"):
        extract([], "ABC")


# --- batches of direct replies loaded by scrolling ---------------------------------

def test_scrolled_in_batches_join_the_first_page_into_one_tree() -> None:
    payloads = load("direct-replies-paging")
    first_page = extract(payloads[:2], target_code("direct-replies-paging"))
    everything = extract(payloads, target_code("direct-replies-paging"))
    items = by_id(everything)

    assert len(everything.items) > len(first_page.items) + 20
    assert {i.id for i in first_page.items} <= set(items)
    assert all(i.parent_id in items for i in everything.items if i.parent_id)
    assert {i.root_id for i in everything.items} == {everything.target.id}
    direct = [i for i in everything.items if i.parent_id == everything.target.id]
    assert len(direct) > len([i for i in first_page.items if i.parent_id == first_page.target.id])


def test_the_more_replies_flag_follows_the_last_batch() -> None:
    payloads = load("direct-replies-paging")
    code = target_code("direct-replies-paging")
    owner = extract(payloads[:2], code).target.id
    assert extract(payloads[:2], code).more_replies[owner] is True
    assert extract(payloads[:5], code).more_replies[owner] is True
    assert extract(payloads, code).more_replies[owner] is False  # the last batch says there is no next page


def test_a_post_reports_its_total_replies_not_just_the_direct_ones() -> None:
    result = extract(load("direct-replies-paging"), target_code("direct-replies-paging"))
    direct = [i for i in result.items if i.parent_id == result.target.id]
    assert result.target.reply_count > len(direct)
