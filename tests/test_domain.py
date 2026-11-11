import dataclasses

import pytest

from threadsift.domain import (
    CollectedAuthor,
    CollectedItem,
    MediaRef,
    StoreOutcome,
    StoreSummary,
)

AUTHOR = CollectedAuthor(id="u1", username="sun_fan")


def make_item(**overrides) -> CollectedItem:
    values = dict(
        id="p1", code="ABC", url="https://www.threads.com/@sun_fan/post/ABC",
        author=AUTHOR, text="hello", parent_id=None, root_id="p1", depth=0,
    )
    values.update(overrides)
    return CollectedItem(**values)


def test_top_level_post_is_its_own_root() -> None:
    item = make_item()
    assert item.parent_id is None and item.root_id == item.id and item.depth == 0


@pytest.mark.parametrize("overrides", [{"root_id": "other"}, {"depth": 1}, {"depth": None}])
def test_parentless_item_with_a_root_must_be_its_own_depth_zero_root(overrides: dict) -> None:
    with pytest.raises(ValueError):
        make_item(**overrides)


def test_reply_with_an_unknown_parent_has_no_links_at_all() -> None:
    item = make_item(id="r1", parent_id=None, root_id=None, depth=None)
    assert (item.parent_id, item.root_id, item.depth) == (None, None, None)


def test_reply_may_have_unknown_root_and_depth() -> None:
    item = make_item(id="r1", parent_id="p1", root_id=None, depth=None)
    assert item.root_id is None and item.depth is None


def test_items_are_immutable_and_media_is_a_tuple() -> None:
    item = make_item(media=(MediaRef("image", "https://img/1.jpg"),))
    assert item.media[0].url == "https://img/1.jpg"
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.text = "changed"  # type: ignore[misc]


def test_store_summary_counts_outcomes() -> None:
    summary = StoreSummary()
    for outcome in (StoreOutcome.NEW, StoreOutcome.NEW, StoreOutcome.UPDATED, StoreOutcome.UNCHANGED):
        summary.add(outcome)
    assert (summary.new, summary.updated, summary.unchanged) == (2, 1, 1)
