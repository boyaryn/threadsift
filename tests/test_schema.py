from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from threadsift import db

TABLES = {
    "traits",
    "authors",
    "author_trait_status",
    "posts",
    "post_reviews",
    "url_queue",
    "runs",
    "url_attempts",
}

ENUMS = {
    "author_type": [],
    "trait_status": ["unknown", "match"],
    "reviewer_kind": ["human", "automated"],
    "url_state": ["pending", "done", "partial", "failed"],
    "run_end_reason": [
        "completed",
        "url_limit",
        "action_budget",
        "early_warning",
        "blocked",
        "interrupted",
        "error",
    ],
}

COLUMNS = {
    "traits": {"id", "name", "description", "is_active", "created_at"},
    "authors": {
        "id", "username", "display_name", "is_verified", "profile_pic_url",
        "type", "first_seen_at", "last_seen_at",
    },
    "author_trait_status": {"author_id", "trait_id", "status", "updated_at"},
    "posts": {
        "id", "code", "url", "author_id", "parent_id", "root_id", "depth",
        "published_at", "first_text", "latest_text", "text_changed_at",
        "like_count", "reply_count", "media", "quoted_post_url",
        "first_seen_at", "last_seen_at",
    },
    "post_reviews": {
        "post_id", "trait_id", "is_evidence", "is_lead", "reviewer_kind",
        "reviewer_name", "reviewed_at",
    },
    "url_queue": {
        "id", "code", "canonical_url", "state", "reason", "source",
        "first_queued_at", "last_attempt_at", "post_id",
    },
    "runs": {
        "id", "started_at", "ended_at", "end_reason", "stop_details",
        "cooldown_overridden", "actions_spent", "urls_done", "urls_partial",
        "urls_failed", "urls_pending",
    },
    "url_attempts": {
        "id", "run_id", "url_id", "started_at", "ended_at", "actions_spent",
        "items_new", "items_updated", "outcome", "reason",
    },
}


def test_tables_exist(clean_engine: Engine) -> None:
    with clean_engine.connect() as connection:
        tables = set(
            connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )
    assert TABLES <= tables


@pytest.mark.parametrize("table", sorted(COLUMNS))
def test_columns(clean_engine: Engine, table: str) -> None:
    with clean_engine.connect() as connection:
        columns = set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :t"
                ),
                {"t": table},
            ).scalars()
        )
    assert columns == COLUMNS[table]


@pytest.mark.parametrize("name", sorted(ENUMS))
def test_enums(clean_engine: Engine, name: str) -> None:
    with clean_engine.connect() as connection:
        labels = connection.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = :n ORDER BY e.enumsortorder"
            ),
            {"n": name},
        ).scalars().all()
        exists = connection.execute(
            text("SELECT count(*) FROM pg_type WHERE typname = :n"), {"n": name}
        ).scalar()
    assert exists == 1
    assert labels == ENUMS[name]


def test_only_one_active_trait(clean_engine: Engine) -> None:
    with clean_engine.begin() as connection:
        connection.execute(text("INSERT INTO traits (name, is_active) VALUES ('a', true)"))
        connection.execute(text("INSERT INTO traits (name, is_active) VALUES ('b', false)"))
        connection.execute(text("INSERT INTO traits (name, is_active) VALUES ('c', false)"))
    with pytest.raises(Exception, match="traits_one_active"):
        with clean_engine.begin() as connection:
            connection.execute(text("INSERT INTO traits (name, is_active) VALUES ('d', true)"))


def test_url_queue_code_is_unique(clean_engine: Engine) -> None:
    insert = text(
        "INSERT INTO url_queue (code, canonical_url, source) "
        "VALUES (:code, 'https://www.threads.com/@u/post/' || :code, 'f.txt')"
    )
    with clean_engine.begin() as connection:
        connection.execute(insert, {"code": "ABC"})
    with pytest.raises(Exception, match="url_queue_code_key"):
        with clean_engine.begin() as connection:
            connection.execute(insert, {"code": "ABC"})


def test_no_foreign_keys_on_parent_and_root(clean_engine: Engine) -> None:
    with clean_engine.connect() as connection:
        constrained = set(
            connection.execute(
                text(
                    "SELECT a.attname FROM pg_constraint c "
                    "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) "
                    "WHERE c.contype = 'f' AND c.conrelid = 'posts'::regclass"
                )
            ).scalars()
        )
    assert constrained == {"author_id"}


def test_orphan_reply_can_be_stored(clean_engine: Engine) -> None:
    with clean_engine.begin() as connection:
        connection.execute(text("INSERT INTO authors (id, username) VALUES ('1', 'u')"))
        connection.execute(
            text(
                "INSERT INTO posts (id, code, url, author_id, parent_id, root_id, first_text, latest_text) "
                "VALUES ('2', 'c', 'https://x', '1', 'missing-parent', 'missing-root', 't', 't')"
            )
        )


def test_migrations_use_no_newer_than_postgres_14_features() -> None:
    sql = "\n".join(path.read_text() for path in sorted(Path("migrations/versions").glob("*.py")))
    assert "MERGE" not in sql.upper().split()
    assert "NULLS NOT DISTINCT" not in sql.upper()
    assert "SECURITY_INVOKER" not in sql.upper()


def test_upgrade_then_downgrade_leaves_empty_schema(empty_database_url: str) -> None:
    db.upgrade(empty_database_url)
    engine = db.make_engine(empty_database_url)
    with engine.connect() as connection:
        tables = set(
            connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )
    assert TABLES <= tables

    db.downgrade(empty_database_url)
    with engine.connect() as connection:
        tables = set(
            connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )
        types = connection.execute(
            text(
                "SELECT typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE n.nspname = 'public' AND t.typtype = 'e'"
            )
        ).scalars().all()
        views = connection.execute(
            text("SELECT viewname FROM pg_views WHERE schemaname = 'public'")
        ).scalars().all()
    engine.dispose()
    assert tables <= {"alembic_version"}
    assert types == []
    assert views == []
