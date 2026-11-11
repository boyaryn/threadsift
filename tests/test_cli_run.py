from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from threadsift.archive.runs import RunLog
from threadsift.cli import app
from threadsift.collection import browser
from threadsift.collection import collector as collector_module
from threadsift.collection import pacing
from threadsift.collection.guard import BlockedError
from threadsift.intake.queue import UrlQueue

from tests.test_runner import FakeCollector, Raise, Result

runner = CliRunner()


@pytest.fixture
def env(clean_engine, database_url, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("THREADSIFT_DATABASE_URL", database_url)
    (tmp_path / ".session").mkdir()
    (tmp_path / ".session" / browser.SESSION_MARKER).write_text("x")

    class Env:
        engine = clean_engine
        opened: list = []
        script: dict = {}
        events: list = []

    e = Env()
    e.opened, e.script, e.events = [], {}, []

    @contextmanager
    def fake_open(config, budget, store, *, capture_dir=None, **kwargs):
        e.opened.append({"capture_dir": capture_dir})
        yield FakeCollector(budget, store, e.script, e.events, warmup=0)

    monkeypatch.setattr(collector_module, "open_browser_collector", fake_open)
    monkeypatch.setattr(pacing, "real_sleep", lambda seconds: None)  # never really wait in tests

    def add(*codes):
        path = tmp_path / f"u{len(list(tmp_path.glob('u*.txt')))}.txt"
        path.write_text("".join(f"https://www.threads.com/@u/post/{c}\n" for c in codes))
        UrlQueue(clean_engine).import_file(path)

    def earlier_run(reason, hours_ago):
        log = RunLog(clean_engine)
        ended = datetime.now(UTC) - timedelta(hours=hours_ago)
        run_id = log.start_run(ended - timedelta(minutes=5), False)
        log.finish_run(run_id, ended, reason, None, 0, 0, 0, 0, 0)

    e.add, e.earlier_run, e.tmp = add, earlier_run, tmp_path
    return e


def test_a_run_prints_the_summary_and_the_run_log_lists_it(env) -> None:
    env.add("A", "B")
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0, result.output
    assert "Run 1 ended: completed" in result.output
    assert "2 done, 0 partial, 0 failed, 0 still pending" in result.output
    assert env.opened == [{"capture_dir": None}]

    log = runner.invoke(app, ["runs"])
    assert log.exit_code == 0
    assert "completed" in log.output and "actions" in log.output
    assert log.output.count("\n") >= 2


def test_run_log_with_no_runs(env) -> None:
    assert "No runs yet." in runner.invoke(app, ["runs"]).output


def test_a_run_during_a_cooldown_is_refused_with_the_time_left_and_opens_nothing(env) -> None:
    env.add("A")
    env.earlier_run("blocked", hours_ago=3)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 1
    assert "possible again in about 21 hours" in result.output
    assert "--override-cooldown" in result.output
    assert env.opened == [] and env.events == []


def test_a_shorter_cooldown_after_an_early_warning(env) -> None:
    env.add("A")
    env.earlier_run("early_warning", hours_ago=7)
    assert runner.invoke(app, ["run"]).exit_code == 0


def test_a_run_soon_after_an_early_warning_is_refused(env) -> None:
    env.add("A")
    env.earlier_run("early_warning", hours_ago=1)
    refused = runner.invoke(app, ["run"])
    assert refused.exit_code == 1 and "early warning" in refused.output and "about 5 hours" in refused.output


def test_the_explicit_override_starts_and_is_marked_in_the_run_log(env) -> None:
    env.add("A")
    env.earlier_run("blocked", hours_ago=3)
    result = runner.invoke(app, ["run", "--override-cooldown"])
    assert result.exit_code == 0, result.output
    assert "overridden" in result.output
    log = runner.invoke(app, ["runs"]).output
    assert "completed *" in log and "* started during a cool-down" in log


def test_blocking_exits_nonzero_and_says_what_was_detected_and_when_runs_resume(env) -> None:
    env.script = {"A": Raise(BlockedError("challenge", "redirected to /challenge/"))}
    env.add("A", "B")
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 1
    assert "blocked: challenge" in result.output
    assert "Runs are possible again after" in result.output
    assert "0 done" in result.output and "2 still pending" in result.output


def test_ctrl_c_exits_with_130_and_still_prints_the_summary(env) -> None:
    env.script = {"A": Raise(KeyboardInterrupt())}
    env.add("A")
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 130
    assert "interrupted" in result.output


def test_capture_collects_only_the_first_url_and_passes_the_directory(env) -> None:
    env.add("A", "B", "C")
    result = runner.invoke(app, ["run", "--capture", str(env.tmp / "cap")])
    assert result.exit_code == 0
    assert env.opened == [{"capture_dir": env.tmp / "cap"}]
    assert [e for e in env.events if isinstance(e, tuple) and e[0] == "collect"] == [("collect", "A")]
    assert "run limit reached" in result.output


def test_without_a_saved_session_nothing_is_opened(env) -> None:
    (env.tmp / ".session" / browser.SESSION_MARKER).unlink()
    env.add("A")
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 1 and "threadsift login" in result.output
    assert env.opened == []
