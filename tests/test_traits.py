from pathlib import Path

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from threadsift.cli import app
from threadsift.review.traits import get_active_trait, set_active_trait

runner = CliRunner()


def test_defining_the_trait(clean_engine) -> None:
    trait = set_active_trait(clean_engine, "pro-renewables", "positive attitude towards renewable energy sources")
    assert trait.is_active
    assert get_active_trait(clean_engine) == trait
    assert trait.description == "positive attitude towards renewable energy sources"


def test_no_active_trait_initially(clean_engine) -> None:
    assert get_active_trait(clean_engine) is None


def test_switching_the_active_trait_keeps_exactly_one_active(clean_engine) -> None:
    first = set_active_trait(clean_engine, "pro-renewables", "a")
    second = set_active_trait(clean_engine, "anti-nuclear", "b")
    assert second.id != first.id
    with clean_engine.connect() as connection:
        active = connection.execute(text("SELECT name FROM traits WHERE is_active")).scalars().all()
        total = connection.execute(text("SELECT count(*) FROM traits")).scalar()
    assert active == ["anti-nuclear"]
    assert total == 2

    again = set_active_trait(clean_engine, "pro-renewables")
    assert again.id == first.id and again.is_active
    assert again.description == "a"
    assert get_active_trait(clean_engine).name == "pro-renewables"


def test_setting_the_same_trait_updates_description(clean_engine) -> None:
    set_active_trait(clean_engine, "t", "old")
    assert set_active_trait(clean_engine, "t", "new").description == "new"
    assert set_active_trait(clean_engine, "t").description == "new"


def test_second_active_trait_is_rejected_by_the_database(clean_engine) -> None:
    set_active_trait(clean_engine, "one")
    with pytest.raises(Exception, match="traits_one_active"):
        with clean_engine.begin() as connection:
            connection.execute(text("INSERT INTO traits (name, is_active) VALUES ('two', true)"))


def test_empty_name_is_rejected(clean_engine) -> None:
    with pytest.raises(ValueError):
        set_active_trait(clean_engine, "   ")


def test_trait_commands(clean_engine, database_url: str, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("THREADSIFT_DATABASE_URL", database_url)

    empty = runner.invoke(app, ["trait", "show"])
    assert empty.exit_code == 0 and "No active trait" in empty.output

    created = runner.invoke(app, ["trait", "set", "pro-renewables", "-d", "positive attitude"])
    assert created.exit_code == 0, created.output
    assert "Active trait: pro-renewables" in created.output

    shown = runner.invoke(app, ["trait", "show"])
    assert "pro-renewables" in shown.output and "positive attitude" in shown.output

    blank = runner.invoke(app, ["trait", "set", "  "])
    assert blank.exit_code == 2
