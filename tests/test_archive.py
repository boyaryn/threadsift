from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text

from threadsift.archive.repository import ArchiveRepository
from threadsift.domain import CollectedAuthor, CollectedItem, MediaRef, StoreOutcome

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(days=1)

SUN = CollectedAuthor(id="u1", username="sun_fan", display_name="Sun Fan", is_verified=False,
                      profile_pic_url="https://pic/1.jpg")


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(T0)


@pytest.fixture
def repo(clean_engine: Engine, clock: Clock) -> ArchiveRepository:
    return ArchiveRepository(clean_engine, clock=clock)


def post(id: str = "p1", text: str = "Installed solar panels", **overrides) -> CollectedItem:
    values = dict(
        id=id, code=f"code{id}", url=f"https://www.threads.com/@sun_fan/post/code{id}",
        author=SUN, text=text, parent_id=None, root_id=id, depth=0, like_count=1, reply_count=0,
    )
    values.update(overrides)
    return CollectedItem(**values)


def reply(id: str, parent_id: str, root_id: str | None, depth: int | None, **overrides) -> CollectedItem:
    return post(id, f"reply {id}", parent_id=parent_id, root_id=root_id, depth=depth, **overrides)


def row(engine: Engine, sql: str, **params):
    with engine.connect() as connection:
        return connection.execute(text(sql), params).mappings().first()


def count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(text(f"SELECT count(*) FROM {table}")).scalar()


# --- 4.2 authors ---------------------------------------------------------

def test_author_is_inserted(repo, clean_engine) -> None:
    repo.store_item(post())
    author = row(clean_engine, "SELECT * FROM authors WHERE id = 'u1'")
    assert author["username"] == "sun_fan"
    assert author["display_name"] == "Sun Fan"
    assert author["first_seen_at"] == T0 and author["last_seen_at"] == T0
    assert author["type"] is None


def test_author_rename_keeps_one_record_and_items(repo, clean_engine, clock) -> None:
    repo.store_item(post("p1"))
    clock.now = T1
    renamed = replace(SUN, username="solar_fan", is_verified=True)
    repo.store_item(post("p2", author=renamed))

    assert count(clean_engine, "authors") == 1
    author = row(clean_engine, "SELECT * FROM authors")
    assert author["username"] == "solar_fan"
    assert author["is_verified"] is True
    assert author["first_seen_at"] == T0 and author["last_seen_at"] == T1
    assert count(clean_engine, "posts WHERE author_id = 'u1'") == 2


def test_missing_profile_fields_do_not_erase_known_ones(repo, clean_engine) -> None:
    repo.store_item(post("p1"))
    sparse = CollectedAuthor(id="u1", username="sun_fan")
    repo.store_item(post("p2", author=sparse))
    author = row(clean_engine, "SELECT * FROM authors")
    assert author["display_name"] == "Sun Fan"
    assert author["profile_pic_url"] == "https://pic/1.jpg"


# --- 4.3 posts -----------------------------------------------------------

def test_new_post_has_first_text_equal_to_latest(repo, clean_engine) -> None:
    assert repo.store_item(post()) is StoreOutcome.NEW
    stored = row(clean_engine, "SELECT * FROM posts")
    assert stored["first_text"] == stored["latest_text"] == "Installed solar panels"
    assert stored["text_changed_at"] is None
    assert stored["first_seen_at"] == T0 and stored["last_seen_at"] == T0


def test_edited_post_keeps_first_version(repo, clean_engine, clock) -> None:
    repo.store_item(post(text="Installed solar panels"))
    clock.now = T1
    outcome = repo.store_item(post(text="Installed solar panels, very happy!"))

    assert outcome is StoreOutcome.UPDATED
    stored = row(clean_engine, "SELECT * FROM posts")
    assert stored["first_text"] == "Installed solar panels"
    assert stored["latest_text"] == "Installed solar panels, very happy!"
    assert stored["text_changed_at"] == T1
    assert stored["first_seen_at"] == T0


def test_second_edit_keeps_original_first_text(repo, clean_engine, clock) -> None:
    repo.store_item(post(text="v1"))
    clock.now = T1
    repo.store_item(post(text="v2"))
    clock.now = T1 + timedelta(days=1)
    repo.store_item(post(text="v3"))
    stored = row(clean_engine, "SELECT * FROM posts")
    assert (stored["first_text"], stored["latest_text"]) == ("v1", "v3")
    assert stored["text_changed_at"] == T1 + timedelta(days=1)


def test_unchanged_refresh_updates_only_last_seen(repo, clean_engine, clock) -> None:
    repo.store_item(post())
    clock.now = T1
    assert repo.store_item(post()) is StoreOutcome.UNCHANGED
    stored = row(clean_engine, "SELECT * FROM posts")
    assert stored["text_changed_at"] is None
    assert stored["first_seen_at"] == T0 and stored["last_seen_at"] == T1
    assert count(clean_engine, "posts") == 1


def test_count_changes_are_updates(repo, clean_engine, clock) -> None:
    repo.store_item(post(like_count=1, reply_count=0))
    clock.now = T1
    assert repo.store_item(post(like_count=5, reply_count=2)) is StoreOutcome.UPDATED
    stored = row(clean_engine, "SELECT * FROM posts")
    assert (stored["like_count"], stored["reply_count"]) == (5, 2)
    assert stored["text_changed_at"] is None


def test_missing_counts_keep_stored_values(repo, clean_engine) -> None:
    repo.store_item(post(like_count=7, reply_count=3))
    assert repo.store_item(post(like_count=None, reply_count=None)) is StoreOutcome.UNCHANGED
    stored = row(clean_engine, "SELECT * FROM posts")
    assert (stored["like_count"], stored["reply_count"]) == (7, 3)


def test_ukrainian_text_with_emoji_is_stored_exactly(repo, clean_engine) -> None:
    original = "Нарешті встановив сонячні панелі ☀️ ‍  ёжик\n\tрядок 2 👨‍👩‍👧"
    repo.store_item(post(text=original))
    assert row(clean_engine, "SELECT first_text, latest_text FROM posts")["first_text"] == original
    assert row(clean_engine, "SELECT latest_text FROM posts")["latest_text"] == original


# --- 4.4 links, media, quotes -------------------------------------------

def test_chain_links_are_restorable(repo, clean_engine) -> None:
    repo.store_items([
        post("P"),
        reply("A", "P", "P", 1),
        reply("B", "A", "P", 2),
        reply("C", "B", "P", 3),
    ])
    with clean_engine.connect() as connection:
        chain = connection.execute(text(
            """
            WITH RECURSIVE up AS (
                SELECT id, parent_id, 0 AS step FROM posts WHERE id = 'C'
                UNION ALL
                SELECT p.id, p.parent_id, up.step + 1 FROM posts p JOIN up ON p.id = up.parent_id
            ) SELECT id FROM up ORDER BY step
            """
        )).scalars().all()
        roots = connection.execute(text("SELECT DISTINCT root_id FROM posts")).scalars().all()
        depths = dict(connection.execute(text("SELECT id, depth FROM posts")).all())
    assert chain == ["C", "B", "A", "P"]
    assert roots == ["P"]
    assert depths == {"P": 0, "A": 1, "B": 2, "C": 3}


def test_parent_not_collected_is_still_stored(repo, clean_engine) -> None:
    repo.store_item(reply("C", "B-missing", "P", 3))
    stored = row(clean_engine, "SELECT * FROM posts WHERE id = 'C'")
    assert stored["parent_id"] == "B-missing"
    assert stored["root_id"] == "P"
    assert count(clean_engine, "posts WHERE root_id = 'P'") == 1


def test_reply_stored_before_its_ancestor(repo, clean_engine) -> None:
    repo.store_item(reply("A", "P", "P", 1))
    repo.store_item(post("P"))
    stored = {r["id"]: r for r in _all(clean_engine)}
    assert stored["A"]["root_id"] == "P" and stored["A"]["parent_id"] == "P"
    assert stored["P"]["root_id"] == "P" and stored["P"]["parent_id"] is None


def _all(engine: Engine):
    with engine.connect() as connection:
        return list(connection.execute(text("SELECT * FROM posts")).mappings())


def test_unknown_root_is_filled_in_when_learned(repo, clean_engine) -> None:
    repo.store_item(reply("A", "P-missing", None, None))
    assert row(clean_engine, "SELECT root_id, depth FROM posts")["root_id"] is None
    assert repo.store_item(reply("A", "P-missing", "P", 1)) is StoreOutcome.UPDATED
    stored = row(clean_engine, "SELECT root_id, depth FROM posts")
    assert (stored["root_id"], stored["depth"]) == ("P", 1)


def test_known_links_are_not_overwritten(repo, clean_engine) -> None:
    repo.store_item(reply("A", "P", "P", 1))
    repo.store_item(reply("A", "OTHER", "OTHER", 9))
    stored = row(clean_engine, "SELECT parent_id, root_id, depth FROM posts")
    assert (stored["parent_id"], stored["root_id"], stored["depth"]) == ("P", "P", 1)


def test_media_and_quote_are_stored_without_queueing(repo, clean_engine) -> None:
    item = post(
        media=(MediaRef("image", "https://img/1.jpg"), MediaRef("video", "https://vid/1.mp4")),
        quoted_post_url="https://www.threads.com/@other/post/QQQ",
    )
    repo.store_item(item)
    stored = row(clean_engine, "SELECT media, quoted_post_url FROM posts")
    assert stored["media"] == [
        {"type": "image", "url": "https://img/1.jpg"},
        {"type": "video", "url": "https://vid/1.mp4"},
    ]
    assert stored["quoted_post_url"] == "https://www.threads.com/@other/post/QQQ"
    assert count(clean_engine, "url_queue") == 0


# --- 4.5 small transactions ----------------------------------------------

def test_failure_on_item_n_keeps_earlier_items(repo, clean_engine) -> None:
    items = [post("p1"), post("p2"), post("p3", text=None), post("p4")]  # type: ignore[arg-type]
    with pytest.raises(Exception):
        repo.store_items(items)
    with clean_engine.connect() as connection:
        ids = connection.execute(text("SELECT id FROM posts ORDER BY id")).scalars().all()
    assert ids == ["p1", "p2"]


def test_failed_item_does_not_leave_a_half_written_author(repo, clean_engine) -> None:
    stranger = CollectedAuthor(id="u9", username="stranger")
    with pytest.raises(Exception):
        repo.store_item(post("p1", text=None, author=stranger))  # type: ignore[arg-type]
    assert count(clean_engine, "authors WHERE id = 'u9'") == 0


# --- 4.6 never delete, never touch review data ---------------------------

def seed_review(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO traits (id, name, is_active) VALUES (1, 't', true)"))
        connection.execute(text(
            "INSERT INTO post_reviews (post_id, trait_id, is_evidence, is_lead, reviewer_kind, reviewed_at) "
            "VALUES ('A', 1, true, false, 'human', '2026-10-01T00:00:00Z')"
        ))
        connection.execute(text(
            "INSERT INTO author_trait_status (author_id, trait_id, status) VALUES ('u1', 1, 'match')"
        ))


def test_reply_missing_on_refresh_stays_with_its_review(repo, clean_engine, clock) -> None:
    repo.store_items([post("P"), reply("A", "P", "P", 1), reply("B", "P", "P", 1)])
    seed_review(clean_engine)
    before_review = row(clean_engine, "SELECT * FROM post_reviews")
    before_a = row(clean_engine, "SELECT * FROM posts WHERE id = 'A'")

    clock.now = T1
    repo.store_items([post("P"), reply("B", "P", "P", 1)])  # A is gone from Threads

    assert count(clean_engine, "posts") == 3
    assert row(clean_engine, "SELECT * FROM posts WHERE id = 'A'") == before_a
    assert row(clean_engine, "SELECT * FROM post_reviews") == before_review
    assert row(clean_engine, "SELECT status FROM author_trait_status")["status"] == "match"


def test_collection_never_writes_review_tables(repo, clean_engine, clock) -> None:
    repo.store_items([post("P"), reply("A", "P", "P", 1)])
    clock.now = T1
    repo.store_items([post("P", text="edited"), reply("A", "P", "P", 1), reply("N", "P", "P", 1)])
    assert count(clean_engine, "post_reviews") == 0
    assert count(clean_engine, "author_trait_status") == 0


def test_new_replies_on_refresh_are_added_and_unreviewed(repo, clean_engine, clock) -> None:
    repo.store_items([post("P")])
    clock.now = T1
    summary = repo.store_items(
        [post("P")] + [reply(f"N{i}", "P", "P", 1) for i in range(5)]
    )
    assert (summary.new, summary.unchanged) == (5, 1)
    with clean_engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT p.id, p.parent_id, p.root_id, r.post_id AS reviewed "
            "FROM posts p LEFT JOIN post_reviews r ON r.post_id = p.id WHERE p.id LIKE 'N%'"
        )).mappings().all()
    assert len(rows) == 5
    assert all(r["parent_id"] == "P" and r["root_id"] == "P" and r["reviewed"] is None for r in rows)


def test_same_reply_from_two_urls_is_one_record(repo, clean_engine) -> None:
    repo.store_items([post("P"), reply("A", "P", "P", 1)])
    repo.store_items([reply("A", "P", "P", 1)])
    assert count(clean_engine, "posts WHERE id = 'A'") == 1
