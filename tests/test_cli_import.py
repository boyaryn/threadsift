from pathlib import Path

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from threadsift import db
from threadsift.cli import app

runner = CliRunner()


@pytest.fixture
def url_env(clean_engine, database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("THREADSIFT_DATABASE_URL", database_url)
    return tmp_path


def test_import_prints_the_report(url_env: Path, clean_engine) -> None:
    sample = url_env / "sample.txt"
    sample.write_text(
        "# first batch\n"
        "https://www.threads.com/@a/post/AAA\n"
        "https://threads.net/@b/post/BBB?igshid=x\n"
        "https://www.threads.com/@someuser\n"
        "https://www.threads.com/@a/post/AAA/\n"
    )
    result = runner.invoke(app, ["import", str(sample)])

    assert result.exit_code == 0, result.output
    assert "Added: 2" in result.output
    assert "Already pending: 1" in result.output
    assert "Refresh requested: 0" in result.output
    assert "Rejected: 1" in result.output
    assert "line 4: https://www.threads.com/@someuser" in result.output
    with clean_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM url_queue")).scalar() == 2


def test_import_requests_refresh_on_second_import(url_env: Path, clean_engine) -> None:
    sample = url_env / "sample.txt"
    sample.write_text("https://www.threads.com/@a/post/AAA\n")
    runner.invoke(app, ["import", str(sample)])
    with clean_engine.begin() as connection:
        connection.execute(text("UPDATE url_queue SET state = 'done'"))

    result = runner.invoke(app, ["import", str(sample)])

    assert "Added: 0" in result.output
    assert "Refresh requested: 1" in result.output


def test_import_missing_file_is_an_error(url_env: Path) -> None:
    result = runner.invoke(app, ["import", str(url_env / "nope.txt")])
    assert result.exit_code != 0
