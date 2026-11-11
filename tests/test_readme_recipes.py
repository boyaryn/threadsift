import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, text

from threadsift import db
from threadsift.archive.repository import ArchiveRepository
from threadsift.domain import CollectedAuthor, CollectedItem
from threadsift.review.traits import set_active_trait

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"


def sql_blocks(section: str) -> list[str]:
    body = README.read_text().split(f"## {section}", 1)[1].split("\n## ", 1)[0]
    return re.findall(r"```sql\n(.*?)```", body, flags=re.S)


def labels(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = 'author_type' ORDER BY e.enumsortorder"
                )
            ).scalars()
        )


def view_names(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return set(connection.execute(text("SELECT viewname FROM pg_views WHERE schemaname = 'public'")).scalars())


def alembic(database_url: str, *args: str) -> None:
    """Run the plain `alembic` command line, as the README tells the operator to."""
    env = {**os.environ, "THREADSIFT_DATABASE_URL": database_url, "THREADSIFT_CONFIG": str(ROOT / "config.example.toml")}
    subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, check=True, capture_output=True)


def test_author_type_recipes_run(empty_database_url: str) -> None:
    db.upgrade(empty_database_url)
    engine = db.make_engine(empty_database_url)
    try:
        add, rename, remove = sql_blocks("Author type values")
        raw = engine.execution_options(isolation_level="AUTOCOMMIT")
        readme = README.read_text()
        assert "uv run alembic downgrade 0001" in readme
        assert "uv run threadsift db-upgrade" in readme.split("3. Recreate the views")[1]

        with raw.connect() as connection:
            assert labels(engine) == []

            connection.exec_driver_sql(add)
            assert labels(engine) == ["organization", "bot"]

            connection.exec_driver_sql(rename)
            assert labels(engine) == ["organization", "spam"]

            connection.exec_driver_sql("INSERT INTO authors (id, username, type) VALUES ('1', 'a', 'spam')")
            connection.exec_driver_sql("INSERT INTO authors (id, username, type) VALUES ('2', 'b', 'organization')")

            # Step 1: drop the views (the review views block changing the column type).
            alembic(empty_database_url, "downgrade", "0001")
            assert "review_items" not in view_names(engine)

            # Step 2: the recipe SQL.
            connection.exec_driver_sql(remove)
            assert labels(engine) == ["organization"]
            details = dict(connection.exec_driver_sql("SELECT id, type::text FROM authors").all())
            assert details == {"1": None, "2": "organization"}

        # Step 3: the views come back and still work with the recreated type.
        db.upgrade(empty_database_url)
        assert {"review_items", "review_queue", "author_candidates", "collection_yield"} <= view_names(engine)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM review_items")).scalar() == 0
    finally:
        engine.dispose()


def test_plain_alembic_command_uses_threadsift_configuration(empty_database_url: str) -> None:
    alembic(empty_database_url, "upgrade", "head")
    engine = db.make_engine(empty_database_url)
    try:
        assert "review_items" in view_names(engine)
    finally:
        engine.dispose()


def test_review_examples_run(empty_database_url: str) -> None:
    db.upgrade(empty_database_url)
    engine = db.make_engine(empty_database_url)
    try:
        set_active_trait(engine, "pro-renewables")
        t0 = datetime(2026, 10, 1, tzinfo=UTC)
        authors = {n: CollectedAuthor(id=str(n), username=f"user{n}") for n in (501, 502, 503)}

        def post(id, author, parent=None, minutes=0):
            return CollectedItem(
                id=str(id), code=f"c{id}", url=f"https://www.threads.com/@u/post/c{id}",
                author=authors[author], text=f"text {id}", parent_id=str(parent) if parent else None,
                root_id="1001", depth=0 if parent is None else 1, published_at=t0 + timedelta(minutes=minutes),
            )

        ArchiveRepository(engine).store_items([
            post(1001, 501), post(1002, 502, 1001, 1), post(1003, 503, 1001, 2),
            post(1004, 502, 1001, 3), post(1005, 501, 1001, 4),
        ])
        with engine.execution_options(isolation_level="AUTOCOMMIT").connect() as connection:
            connection.exec_driver_sql("ALTER TYPE author_type ADD VALUE 'organization'")
            for block in sql_blocks("Reviewing"):
                if "1001'" in block and "NULL" in block:
                    continue  # taking a review back runs last
                connection.exec_driver_sql(block)

            reviews = {r[0]: (r[1], r[2]) for r in connection.exec_driver_sql(
                "SELECT post_id, is_evidence, is_lead FROM post_reviews")}
            assert reviews == {"1001": (True, False), "1002": (False, True), "1003": (False, False)}
            assert connection.exec_driver_sql("SELECT status::text FROM author_trait_status").scalar() == "match"
            assert connection.exec_driver_sql("SELECT type::text FROM authors WHERE id = '502'").scalar() == "organization"

            takeback = next(b for b in sql_blocks("Reviewing") if "1001'" in b and "NULL" in b)
            connection.exec_driver_sql(takeback)
            assert connection.exec_driver_sql("SELECT count(*) FROM post_reviews WHERE post_id = '1001'").scalar() == 0
    finally:
        engine.dispose()
