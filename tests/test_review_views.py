from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text

from threadsift import db
from threadsift.archive.repository import ArchiveRepository
from threadsift.domain import CollectedAuthor, CollectedItem
from threadsift.review.traits import set_active_trait

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

ALICE = CollectedAuthor(id="u1", username="alice")
BOB = CollectedAuthor(id="u2", username="bob")
CAROL = CollectedAuthor(id="u3", username="carol")


def item(id, author, text, parent=None, root=None, depth=0, minutes=0):
    return CollectedItem(
        id=id, code=f"c{id}", url=f"https://www.threads.com/@{author.username}/post/c{id}",
        author=author, text=text, parent_id=parent, root_id=root or id, depth=depth,
        published_at=T0 + timedelta(minutes=minutes),
    )


def seed(engine: Engine) -> None:
    """Thread P (alice) with replies A (bob), B (carol), C (bob, under A);
    and a second thread Q (alice) with reply R (carol)."""
    ArchiveRepository(engine).store_items([
        item("P", ALICE, "Solar panels are great", minutes=0),
        item("A", BOB, "Agreed, I installed some", parent="P", root="P", depth=1, minutes=1),
        item("B", CAROL, "Gas is cheaper", parent="P", root="P", depth=1, minutes=2),
        item("C", BOB, "Pays back in 6 years", parent="A", root="P", depth=2, minutes=3),
        item("Q", ALICE, "Wind or hydro?", minutes=10),
        item("R", CAROL, "Hydro", parent="Q", root="Q", depth=1, minutes=11),
    ])


@pytest.fixture
def engine(clean_engine: Engine) -> Engine:
    set_active_trait(clean_engine, "pro-renewables", "positive attitude towards renewable energy")
    seed(clean_engine)
    return clean_engine


@pytest.fixture
def fresh_engine(empty_database_url: str) -> Iterator[Engine]:
    """A freshly migrated database of its own, for tests that add enum values."""
    db.upgrade(empty_database_url)
    engine = db.make_engine(empty_database_url)
    set_active_trait(engine, "pro-renewables", "positive attitude towards renewable energy")
    seed(engine)
    yield engine
    engine.dispose()


def add_type_value(engine: Engine, value: str) -> None:
    with engine.execution_options(isolation_level="AUTOCOMMIT").connect() as connection:
        connection.execute(text(f"ALTER TYPE author_type ADD VALUE '{value}'"))


def run(engine: Engine, sql: str, **params) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql), params)


def rows(engine: Engine, sql: str, **params) -> list[dict]:
    with engine.connect() as connection:
        return [dict(r) for r in connection.execute(text(sql), params).mappings()]


def one(engine: Engine, sql: str, **params) -> dict:
    result = rows(engine, sql, **params)
    assert len(result) == 1, result
    return result[0]


def review(engine: Engine, post_id: str, evidence, lead, view: str = "review_items") -> None:
    run(engine, f"UPDATE {view} SET is_evidence = :e, is_lead = :l WHERE post_id = :id",
        e=evidence, l=lead, id=post_id)


def queue_ids(engine: Engine) -> list[str]:
    return [r["post_id"] for r in rows(engine, "SELECT post_id FROM review_queue")]


def stored_review(engine: Engine, post_id: str) -> dict | None:
    found = rows(engine, "SELECT * FROM post_reviews WHERE post_id = :id", id=post_id)
    return found[0] if found else None


# --- 6.2 review_items ----------------------------------------------------

def test_items_start_unreviewed(engine) -> None:
    items = rows(engine, "SELECT * FROM review_items ORDER BY post_id")
    assert [i["post_id"] for i in items] == ["A", "B", "C", "P", "Q", "R"]
    assert all(i["is_evidence"] is None and i["is_lead"] is None for i in items)
    assert all(i["author_status"] == "unknown" and i["author_type"] is None for i in items)
    assert all(i["trait"] == "pro-renewables" for i in items)


def test_item_shows_thread_context(engine) -> None:
    c = one(engine, "SELECT * FROM review_items WHERE post_id = 'C'")
    assert c["text"] == "Pays back in 6 years"
    assert c["parent_text"] == "Agreed, I installed some"
    assert c["root_text"] == "Solar panels are great"
    assert c["username"] == "bob" and c["depth"] == 2


def test_reviewed_as_lead_only(engine) -> None:
    review(engine, "P", False, True)
    stored = stored_review(engine, "P")
    assert (stored["is_evidence"], stored["is_lead"]) == (False, True)


def test_reviewed_as_evidence_only_treats_null_lead_as_no(engine) -> None:
    review(engine, "A", True, None)
    stored = stored_review(engine, "A")
    assert (stored["is_evidence"], stored["is_lead"]) == (True, False)


def test_reviewed_as_both(engine) -> None:
    review(engine, "A", True, True)
    stored = stored_review(engine, "A")
    assert (stored["is_evidence"], stored["is_lead"]) == (True, True)


def test_reviewed_as_neither_is_no_longer_unreviewed(engine) -> None:
    review(engine, "B", False, False)
    stored = stored_review(engine, "B")
    assert (stored["is_evidence"], stored["is_lead"]) == (False, False)
    shown = one(engine, "SELECT is_evidence, is_lead FROM review_items WHERE post_id = 'B'")
    assert (shown["is_evidence"], shown["is_lead"]) == (False, False)
    assert "B" not in queue_ids(engine)


def test_review_is_recorded_as_human_with_current_time(engine) -> None:
    review(engine, "A", True, False)
    stored = stored_review(engine, "A")
    assert stored["reviewer_kind"] == "human"
    assert stored["reviewer_name"] == "human"
    assert abs(stored["reviewed_at"] - datetime.now(UTC)) < timedelta(minutes=1)
    trait = one(engine, "SELECT id FROM traits WHERE is_active")["id"]
    assert stored["trait_id"] == trait


def test_changing_a_review_updates_it_in_place(engine) -> None:
    review(engine, "A", True, False)
    review(engine, "A", False, True)
    assert len(rows(engine, "SELECT 1 FROM post_reviews")) == 1
    stored = stored_review(engine, "A")
    assert (stored["is_evidence"], stored["is_lead"]) == (False, True)


def test_setting_both_flags_to_null_makes_it_unreviewed_again(engine) -> None:
    review(engine, "A", True, True)
    review(engine, "A", None, None)
    assert stored_review(engine, "A") is None
    shown = one(engine, "SELECT is_evidence, is_lead FROM review_items WHERE post_id = 'A'")
    assert (shown["is_evidence"], shown["is_lead"]) == (None, None)


def test_editing_only_author_fields_does_not_create_a_review(engine) -> None:
    run(engine, "UPDATE review_items SET author_status = 'match' WHERE post_id = 'A'")
    assert rows(engine, "SELECT 1 FROM post_reviews") == []


@pytest.mark.parametrize(
    "column", ["text", "username", "root_text", "parent_text", "depth", "url", "post_id", "trait"]
)
def test_read_only_columns_are_rejected(engine, column) -> None:
    before = rows(engine, "SELECT * FROM review_items ORDER BY post_id")
    value = "7" if column == "depth" else "'changed'"
    with pytest.raises(Exception, match="read-only"):
        run(engine, f"UPDATE review_items SET {column} = {value} WHERE post_id = 'A'")
    assert rows(engine, "SELECT * FROM review_items ORDER BY post_id") == before
    assert one(engine, "SELECT latest_text FROM posts WHERE id = 'A'")["latest_text"] == "Agreed, I installed some"


def test_read_only_change_alongside_a_valid_edit_changes_nothing(engine) -> None:
    with pytest.raises(Exception, match="read-only"):
        run(engine, "UPDATE review_items SET is_evidence = true, text = 'x' WHERE post_id = 'A'")
    assert stored_review(engine, "A") is None


def test_views_cannot_be_inserted_into_or_deleted_from(engine) -> None:
    for view in ("review_items", "review_queue", "author_candidates", "collection_yield"):
        with pytest.raises(Exception):
            run(engine, f"DELETE FROM {view}")
    with pytest.raises(Exception):
        run(engine, "INSERT INTO review_items (post_id) VALUES ('X')")
    assert len(rows(engine, "SELECT 1 FROM posts")) == 6


def test_editing_without_an_active_trait_is_an_error(clean_engine) -> None:
    seed(clean_engine)
    with pytest.raises(Exception, match="no active trait"):
        review(clean_engine, "A", True, False)
    with pytest.raises(Exception, match="no active trait"):
        run(clean_engine, "UPDATE review_items SET author_status = 'match' WHERE post_id = 'A'")


def test_type_outside_the_list_is_rejected_and_unchanged(engine) -> None:
    with pytest.raises(Exception, match="invalid input value for enum"):
        run(engine, "UPDATE review_items SET author_type = 'organization' WHERE post_id = 'A'")
    assert one(engine, "SELECT type FROM authors WHERE id = 'u2'")["type"] is None


def test_new_type_value_is_accepted(fresh_engine) -> None:
    add_type_value(fresh_engine, "organization")
    run(fresh_engine, "UPDATE review_items SET author_type = 'organization' WHERE post_id = 'A'")
    assert one(fresh_engine, "SELECT type::text AS d FROM authors WHERE id = 'u2'")["d"] == "organization"
    for post_id in ("A", "C"):
        shown = one(fresh_engine, "SELECT author_type::text AS d FROM review_items WHERE post_id = :id", id=post_id)
        assert shown["d"] == "organization"


def test_author_type_can_be_cleared(fresh_engine) -> None:
    add_type_value(fresh_engine, "organization")
    run(fresh_engine, "UPDATE review_items SET author_type = 'organization' WHERE post_id = 'A'")
    run(fresh_engine, "UPDATE review_items SET author_type = NULL WHERE post_id = 'A'")
    assert one(fresh_engine, "SELECT type FROM authors WHERE id = 'u2'")["type"] is None


def test_author_status_is_set_for_the_active_trait(engine) -> None:
    run(engine, "UPDATE review_items SET author_status = 'match' WHERE post_id = 'A'")
    status = one(engine, "SELECT * FROM author_trait_status")
    assert (status["author_id"], status["status"]) == ("u2", "match")
    assert status["trait_id"] == one(engine, "SELECT id FROM traits WHERE is_active")["id"]
    for post_id in ("A", "C"):
        assert one(engine, "SELECT author_status FROM review_items WHERE post_id = :id", id=post_id)["author_status"] == "match"
    run(engine, "UPDATE review_items SET author_status = 'unknown' WHERE post_id = 'A'")
    assert one(engine, "SELECT status FROM author_trait_status")["status"] == "unknown"


def test_invalid_author_status_is_rejected(engine) -> None:
    with pytest.raises(Exception, match="invalid input value for enum"):
        run(engine, "UPDATE review_items SET author_status = 'maybe' WHERE post_id = 'A'")


# --- 6.3 review_queue ----------------------------------------------------

def test_queue_lists_unreviewed_items_in_thread_order(engine) -> None:
    # Grouped by thread, then by depth, then by publication time.
    assert queue_ids(engine) == ["P", "A", "B", "C", "Q", "R"]


def test_queue_shows_thread_context(engine) -> None:
    c = one(engine, "SELECT * FROM review_queue WHERE post_id = 'C'")
    assert c["root_text"] == "Solar panels are great"
    assert c["parent_text"] == "Agreed, I installed some"
    assert c["username"] == "bob"


def test_editing_a_flag_in_the_queue_stores_the_review_and_removes_the_row(engine) -> None:
    review(engine, "A", True, None, view="review_queue")
    assert stored_review(engine, "A")["is_evidence"] is True
    assert "A" not in queue_ids(engine)
    assert queue_ids(engine) == ["P", "B", "C", "Q", "R"]


def test_reviewed_as_neither_leaves_the_queue(engine) -> None:
    review(engine, "B", False, False, view="review_queue")
    assert "B" not in queue_ids(engine)


def test_setting_back_to_unreviewed_returns_to_the_queue(engine) -> None:
    review(engine, "A", True, False)
    review(engine, "A", None, None)
    assert "A" in queue_ids(engine)


def test_matched_author_leaves_the_queue(engine) -> None:
    run(engine, "UPDATE review_queue SET author_status = 'match' WHERE post_id = 'A'")
    assert queue_ids(engine) == ["P", "B", "Q", "R"]  # bob's A and C are gone


def test_author_with_a_type_leaves_the_queue(fresh_engine) -> None:
    add_type_value(fresh_engine, "organization")
    run(fresh_engine, "UPDATE review_queue SET author_type = 'organization' WHERE post_id = 'B'")
    assert queue_ids(fresh_engine) == ["P", "A", "C", "Q"]  # carol's B and R are gone


def test_unknown_author_stays_in_the_queue(engine) -> None:
    run(engine, "UPDATE review_queue SET author_status = 'match' WHERE post_id = 'A'")
    run(engine, "UPDATE review_queue SET author_status = 'unknown' WHERE post_id = 'P'")
    assert "P" in queue_ids(engine) and "B" in queue_ids(engine)


def test_queue_read_only_columns_are_rejected(engine) -> None:
    with pytest.raises(Exception, match="read-only"):
        run(engine, "UPDATE review_queue SET text = 'x' WHERE post_id = 'A'")


def test_new_reply_after_refresh_appears_in_the_queue(engine) -> None:
    ArchiveRepository(engine).store_item(item("N", CAROL, "new", parent="P", root="P", depth=1, minutes=30))
    assert "N" in queue_ids(engine)


def test_matched_authors_new_replies_stay_out_of_the_queue(engine) -> None:
    run(engine, "UPDATE review_queue SET author_status = 'match' WHERE post_id = 'B'")
    ArchiveRepository(engine).store_item(item("N", CAROL, "new", parent="P", root="P", depth=1, minutes=30))
    assert "N" not in queue_ids(engine)


# --- 6.4 author_candidates ----------------------------------------------

def candidates(engine: Engine) -> list[dict]:
    return rows(engine, "SELECT * FROM author_candidates ORDER BY author_id")


def test_no_candidates_without_evidence(engine) -> None:
    review(engine, "A", False, True)
    assert candidates(engine) == []


def test_candidate_with_one_evidence_item(engine) -> None:
    review(engine, "A", True, False)
    found = candidates(engine)
    assert len(found) == 1
    assert (found[0]["author_id"], found[0]["username"]) == ("u2", "bob")
    assert found[0]["evidence_count"] == 1
    assert found[0]["author_status"] == "unknown" and found[0]["author_type"] is None


def test_evidence_does_not_promote_the_author(engine) -> None:
    review(engine, "A", True, False)
    review(engine, "C", True, False)
    assert candidates(engine)[0]["evidence_count"] == 2
    assert one(engine, "SELECT author_status FROM review_items WHERE post_id = 'A'")["author_status"] == "unknown"
    assert rows(engine, "SELECT 1 FROM author_trait_status") == []


def test_candidates_are_sorted_by_evidence_count(engine) -> None:
    review(engine, "B", True, False)
    review(engine, "A", True, False)
    review(engine, "C", True, False)
    assert [c["username"] for c in rows(engine, "SELECT * FROM author_candidates")] == ["bob", "carol"]


def test_deciding_the_author_removes_the_candidate(engine) -> None:
    review(engine, "A", True, False)
    run(engine, "UPDATE author_candidates SET author_status = 'match' WHERE author_id = 'u2'")
    assert one(engine, "SELECT status FROM author_trait_status")["status"] == "match"
    assert candidates(engine) == []


def test_author_with_type_is_not_a_candidate(fresh_engine) -> None:
    add_type_value(fresh_engine, "organization")
    review(fresh_engine, "A", True, False)
    run(fresh_engine, "UPDATE author_candidates SET author_type = 'organization' WHERE author_id = 'u2'")
    assert candidates(fresh_engine) == []


def test_candidate_columns_other_than_status_and_type_are_read_only(engine) -> None:
    review(engine, "A", True, False)
    with pytest.raises(Exception, match="read-only"):
        run(engine, "UPDATE author_candidates SET evidence_count = 9 WHERE author_id = 'u2'")
    with pytest.raises(Exception, match="read-only"):
        run(engine, "UPDATE author_candidates SET username = 'x' WHERE author_id = 'u2'")


def test_other_traits_evidence_is_not_counted(engine) -> None:
    review(engine, "A", True, False)
    set_active_trait(engine, "other-trait")
    assert candidates(engine) == []
    assert queue_ids(engine) == ["P", "A", "B", "C", "Q", "R"]
    set_active_trait(engine, "pro-renewables")
    assert len(candidates(engine)) == 1


# --- 6.5 collection_yield -----------------------------------------------

def _insert_url(engine: Engine, code: str, post_id: str | None, state: str) -> int:
    with engine.begin() as connection:
        return connection.execute(
            text(
                "INSERT INTO url_queue (code, canonical_url, source, state, post_id) "
                "VALUES (:c, 'https://www.threads.com/@x/post/' || :c, 'f.txt', CAST(:s AS url_state), :p) RETURNING id"
            ),
            {"c": code, "s": state, "p": post_id},
        ).scalar_one()


def attempt(engine: Engine, url_id: int, actions: int) -> None:
    with engine.begin() as connection:
        run_id = connection.execute(text("INSERT INTO runs DEFAULT VALUES RETURNING id")).scalar_one()
        connection.execute(
            text("INSERT INTO url_attempts (run_id, url_id, actions_spent, outcome) VALUES (:r, :u, :a, 'done')"),
            {"r": run_id, "u": url_id, "a": actions},
        )


def yield_row(engine: Engine, url_id: int) -> dict:
    return one(engine, "SELECT * FROM collection_yield WHERE url_id = :id", id=url_id)


def test_yield_of_a_url_with_two_attempts_and_four_evidence_items(engine) -> None:
    url_id = _insert_url(engine, "cP", "P", "done")
    attempt(engine, url_id, 120)
    attempt(engine, url_id, 80)
    for post_id in ("P", "A", "B", "C"):
        review(engine, post_id, True, False)

    got = yield_row(engine, url_id)
    assert got["actions_spent"] == 200
    assert got["attempts"] == 2
    assert got["items"] == 4
    assert got["reviewed"] == 4
    assert got["evidence"] == 4
    assert float(got["evidence_per_100_actions"]) == 2.0


def test_yield_counts_only_the_urls_own_subtree(engine) -> None:
    thread_p = _insert_url(engine, "cP", "P", "done")
    reply_a = _insert_url(engine, "cA", "A", "done")
    thread_q = _insert_url(engine, "cQ", "Q", "done")
    assert yield_row(engine, thread_p)["items"] == 4  # P, A, B, C
    assert yield_row(engine, reply_a)["items"] == 2  # A, C
    assert yield_row(engine, thread_q)["items"] == 2  # Q, R


def test_yield_reviewed_evidence_and_lead_counts(engine) -> None:
    url_id = _insert_url(engine, "cP", "P", "done")
    attempt(engine, url_id, 50)
    review(engine, "P", False, True)
    review(engine, "A", True, False)
    review(engine, "B", False, False)
    got = yield_row(engine, url_id)
    assert (got["items"], got["reviewed"], got["evidence"], got["leads"]) == (4, 3, 1, 1)
    assert float(got["evidence_per_100_actions"]) == 2.0


def test_url_never_attempted_has_no_rate(engine) -> None:
    url_id = _insert_url(engine, "cP", "P", "done")
    got = yield_row(engine, url_id)
    assert got["actions_spent"] == 0 and got["attempts"] == 0
    assert got["evidence_per_100_actions"] is None


def test_url_without_a_collected_post_still_has_a_row(engine) -> None:
    url_id = _insert_url(engine, "cNone", None, "pending")
    got = yield_row(engine, url_id)
    assert (got["items"], got["evidence"]) == (0, 0)
    assert got["state"] == "pending"


def test_yield_is_read_only(engine) -> None:
    url_id = _insert_url(engine, "cP", "P", "done")
    with pytest.raises(Exception):
        run(engine, "UPDATE collection_yield SET actions_spent = 1 WHERE url_id = :id", id=url_id)


def test_yield_uses_the_active_trait_only(engine) -> None:
    url_id = _insert_url(engine, "cP", "P", "done")
    attempt(engine, url_id, 100)
    review(engine, "A", True, False)
    set_active_trait(engine, "other-trait")
    assert yield_row(engine, url_id)["evidence"] == 0


def test_the_reviewer_name_is_human_whatever_role_the_client_uses(engine) -> None:
    if not one(engine, "SELECT rolsuper FROM pg_roles WHERE rolname = current_user")["rolsuper"]:
        pytest.skip("needs a superuser role, to create a temporary role and switch to it")
    run(engine, "CREATE ROLE reviewer_tmp LOGIN")
    try:
        run(engine, "GRANT ALL ON ALL TABLES IN SCHEMA public TO reviewer_tmp")
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE reviewer_tmp"))
            assert connection.execute(text("SELECT current_user")).scalar() == "reviewer_tmp"
            connection.execute(text("UPDATE review_items SET is_evidence = true WHERE post_id = 'A'"))
        assert stored_review(engine, "A")["reviewer_name"] == "human"
    finally:
        run(engine, "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM reviewer_tmp")
        run(engine, "DROP ROLE reviewer_tmp")
