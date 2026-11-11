from pathlib import Path

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from threadsift import db
from threadsift.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_local_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def test_db_upgrade_migrates_fresh_database_and_is_a_noop_twice(
    empty_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("THREADSIFT_DATABASE_URL", empty_database_url)

    first = runner.invoke(app, ["db-upgrade"])
    assert first.exit_code == 0, first.output
    assert "upgraded" in first.output

    engine = db.make_engine(empty_database_url)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO traits (name) VALUES ('kept')"))

    second = runner.invoke(app, ["db-upgrade"])
    assert second.exit_code == 0, second.output
    assert "already up to date" in second.output

    with engine.connect() as connection:
        assert connection.execute(text("SELECT name FROM traits")).scalars().all() == ["kept"]
    engine.dispose()


def test_db_upgrade_reports_unreachable_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "THREADSIFT_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none?connect_timeout=1"
    )
    result = runner.invoke(app, ["db-upgrade"])
    assert result.exit_code == 1
    assert "Database error" in result.output


def test_stray_python_files_in_the_migrations_folder_are_found(tmp_path) -> None:
    for name in ("0001_initial_schema.py", "0002_review_views.py", ".gitkeep", "notes.txt", ".DS_Store"):
        (tmp_path / name).write_text("x")
    assert db.stray_migration_files(tmp_path) == []
    for name in ("._0001_initial_schema.py", "scratch.py", "0003.py"):
        (tmp_path / name).write_bytes(b"\x00\x05\x16\x07")
    assert db.stray_migration_files(tmp_path) == ["._0001_initial_schema.py", "0003.py", "scratch.py"]


def test_db_upgrade_explains_stray_files_instead_of_a_traceback(empty_database_url, monkeypatch) -> None:
    monkeypatch.setenv("THREADSIFT_DATABASE_URL", empty_database_url)
    monkeypatch.setattr(db, "stray_migration_files", lambda directory=None: ["._0001_initial_schema.py"])
    result = runner.invoke(app, ["db-upgrade"])
    assert result.exit_code == 1
    assert "._0001_initial_schema.py" in result.output and "find . -name '._*'" in result.output
    assert "Traceback" not in result.output


def test_real_migration_folder_has_no_stray_files() -> None:
    assert db.stray_migration_files() == []
