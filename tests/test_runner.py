import json
import random
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from threadsift.archive.runs import RunLog
from threadsift.collection.budget import ActionBudget
from threadsift.collection.expansion import ExpansionResult
from threadsift.collection.guard import BlockedError
from threadsift.collection.monitor import EarlyWarning, Signal
from threadsift.collection.pacing import Pacer
from threadsift.collection.runner import (
    CooldownActive,
    Runner,
    cooldown_status,
    describe,
    describe_wait,
)
from threadsift.config import Config, CooldownConfig
from threadsift.domain import CollectedAuthor, CollectedItem, RunResult, StoreSummary
from threadsift.intake.queue import UrlQueue

T0 = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


# --- a scripted collector --------------------------------------------------------

@dataclass
class Result:
    state: str = "done"
    reason: str | None = None
    actions: int = 10
    new: int = 3
    updated: int = 0
    items: int = 1


@dataclass
class Raise:
    error: BaseException
    actions: int = 7
    items: int = 1


class FakeCollector:
    def __init__(self, budget: ActionBudget, store, script: dict, events: list, warmup: int, warmup_error=None) -> None:
        self.budget, self.store, self.script, self.events = budget, store, script, events
        self.warmup, self.warmup_error = warmup, warmup_error
        self.last_summary = StoreSummary()

    def warm_up(self) -> None:
        self.events.append("warm_up")
        self.budget.spend(self.warmup)
        if self.warmup_error:
            raise self.warmup_error

    def collect(self, queued) -> ExpansionResult:
        self.events.append(("collect", queued.code))
        step = self.script.get(queued.code, Result())
        self.budget.spend(step.actions)
        for n in range(step.items):
            self.store(
                CollectedItem(
                    id=f"{queued.code}-{n}", code=f"{queued.code}{n}", url=f"https://www.threads.com/@u/post/{queued.code}{n}",
                    author=CollectedAuthor("u1", "user"), text=f"text {queued.code} {n}",
                    parent_id=None, root_id=f"{queued.code}-{n}", depth=0,
                )
            )
        if isinstance(step, Raise):
            self.last_summary = StoreSummary(new=step.items)
            raise step.error
        return ExpansionResult(
            step.state, step.reason, f"{queued.code}-0" if step.items else None,
            StoreSummary(new=step.new, updated=step.updated),
        )


class Setup:
    def __init__(self, engine, tmp_path, script=None, warmup=0, warmup_error=None, open_error=None, **limits) -> None:
        base = Config()
        self.config = replace(base, limits=replace(base.limits, **limits)) if limits else base
        self.engine, self.tmp_path = engine, tmp_path
        self.clock = Clock()
        self.events: list = []
        self.opened: list = []
        self.script, self.warmup, self.warmup_error, self.open_error = script or {}, warmup, warmup_error, open_error
        self.sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            self.events.append(("sleep", seconds))

        self.pacer = Pacer(self.config.pacing, random.Random(1), sleep)
        self.log = RunLog(engine)

    @contextmanager
    def open_collector(self, budget, store):
        if self.open_error:
            raise self.open_error
        collector = FakeCollector(budget, store, self.script, self.events, self.warmup, self.warmup_error)
        self.opened.append(collector)
        yield collector

    def runner(self) -> Runner:
        return Runner(self.engine, self.config, self.open_collector, clock=self.clock, pacer=self.pacer)

    def add_urls(self, *codes: str) -> None:
        path = self.tmp_path / f"urls-{len(list(self.tmp_path.iterdir()))}.txt"
        path.write_text("".join(f"https://www.threads.com/@u/post/{c}\n" for c in codes))
        queue = UrlQueue(self.engine, self.clock)
        queue.import_file(path)
        self.clock.advance(seconds=1)  # later imports are younger

    def finished_run(self, reason: str, ago: timedelta) -> None:
        started = self.clock.now - ago - timedelta(minutes=5)
        run_id = self.log.start_run(started, False)
        self.log.finish_run(run_id, self.clock.now - ago, reason, None, 0, 0, 0, 0, 0)

    def states(self) -> dict[str, tuple[str, str | None]]:
        with self.engine.connect() as c:
            rows = c.execute(text("SELECT code, state::text AS state, reason FROM url_queue")).all()
        return {r.code: (r.state, r.reason) for r in rows}

    def rows(self, sql: str) -> list:
        with self.engine.connect() as c:
            return list(c.execute(text(sql)).mappings())


@pytest.fixture
def setup(clean_engine, tmp_path):
    def make(**kwargs) -> Setup:
        return Setup(clean_engine, tmp_path, **kwargs)
    return make


# --- 12.1 the start: cool-downs ------------------------------------------------------

def test_a_run_three_hours_after_a_challenge_is_refused_and_opens_nothing(setup) -> None:
    s = setup()
    s.add_urls("A")
    s.finished_run("blocked", ago=timedelta(hours=3))
    with pytest.raises(CooldownActive) as refused:
        s.runner().run()
    assert refused.value.reason == "blocked"
    assert refused.value.remaining == timedelta(hours=21)
    assert describe_wait(refused.value.remaining) == "about 21 hours"
    assert s.opened == [] and s.events == []
    assert len(s.rows("SELECT 1 FROM runs")) == 1  # the refusal is not logged as a run


def test_a_run_seven_hours_after_an_early_warning_starts(setup) -> None:
    s = setup()
    s.add_urls("A")
    s.finished_run("early_warning", ago=timedelta(hours=7))
    assert s.runner().run().end_reason == "completed"


def test_a_run_five_hours_after_an_early_warning_is_refused_with_the_time_left(setup) -> None:
    s = setup()
    s.add_urls("A")
    s.finished_run("early_warning", ago=timedelta(hours=5))
    with pytest.raises(CooldownActive) as refused:
        s.runner().run()
    assert refused.value.remaining == timedelta(hours=1)
    assert describe_wait(refused.value.remaining) == "about 60 minutes"


def test_the_explicit_override_starts_and_is_logged(setup) -> None:
    s = setup()
    s.add_urls("A")
    s.finished_run("blocked", ago=timedelta(hours=3))
    result = s.runner().run(override_cooldown=True)
    assert result.end_reason == "completed" and result.cooldown_overridden
    assert s.rows("SELECT cooldown_overridden FROM runs ORDER BY id DESC LIMIT 1")[0]["cooldown_overridden"] is True
    assert "cool-down (overridden)" in describe(result)


def test_an_override_is_not_marked_when_there_was_no_cooldown(setup) -> None:
    s = setup()
    s.add_urls("A")
    result = s.runner().run(override_cooldown=True)
    assert result.cooldown_overridden is False


@pytest.mark.parametrize("reason", ["completed", "url_limit", "action_budget", "interrupted", "error"])
def test_only_blocked_and_early_warning_runs_start_a_cooldown(setup, reason) -> None:
    s = setup()
    s.add_urls("A")
    s.finished_run(reason, ago=timedelta(minutes=1))
    assert s.runner().run().end_reason == "completed"


def test_cooldown_lengths_are_configurable_and_the_end_moment_counts_as_over(setup) -> None:
    s = setup()
    s.config = replace(s.config, cooldown=CooldownConfig(blocked_hours=1.0, early_warning_hours=0.5))
    s.add_urls("A", "B")
    s.finished_run("blocked", ago=timedelta(hours=1))
    assert cooldown_status(s.log, s.config.cooldown, s.clock.now) is None  # exactly over
    s.finished_run("blocked", ago=timedelta(minutes=59))
    assert cooldown_status(s.log, s.config.cooldown, s.clock.now) is not None


def test_only_the_latest_run_decides(setup) -> None:
    s = setup()
    s.add_urls("A")
    s.finished_run("blocked", ago=timedelta(hours=3))
    s.finished_run("completed", ago=timedelta(minutes=10))  # e.g. an overridden run that went fine
    assert s.runner().run().end_reason == "completed"


def test_the_cooldown_is_checked_before_anything_else_even_with_nothing_pending(setup) -> None:
    s = setup()
    s.finished_run("blocked", ago=timedelta(minutes=10))
    with pytest.raises(CooldownActive):
        s.runner().run()


def test_the_home_feed_warm_up_comes_before_the_first_url(setup) -> None:
    s = setup()
    s.add_urls("A", "B")
    s.runner().run()
    assert s.events[0] == "warm_up"
    assert s.events[1] == ("collect", "A")


def test_with_nothing_pending_no_browser_is_opened_and_the_run_is_logged(setup) -> None:
    s = setup()
    result = s.runner().run()
    assert result.end_reason == "completed" and s.opened == [] and s.events == []
    assert [r["end_reason"] for r in s.rows("SELECT end_reason::text AS end_reason FROM runs")] == ["completed"]


# --- 12.2 the per-URL loop -----------------------------------------------------------------

def test_the_run_limit_leaves_the_rest_pending_oldest_first(setup) -> None:
    s = setup(urls_per_run=5)
    s.add_urls("A", "B", "C")
    s.add_urls("D", "E", "F", "G", "H")
    result = s.runner().run()
    assert result.end_reason == "url_limit"
    assert [e[1] for e in s.events if isinstance(e, tuple) and e[0] == "collect"] == ["A", "B", "C", "D", "E"]
    states = {code: st for code, (st, _) in s.states().items()}
    assert states == {**{c: "done" for c in "ABCDE"}, **{c: "pending" for c in "FGH"}}


def test_a_run_that_finishes_everything_pending_is_completed_not_limited(setup) -> None:
    s = setup(urls_per_run=3)
    s.add_urls("A", "B", "C")
    assert s.runner().run().end_reason == "completed"


def test_not_enough_run_budget_for_another_url_ends_the_run_and_leaves_it_pending(setup) -> None:
    s = setup(warmup=1100)  # 1100 of 1200 spent, 150 needed
    s.add_urls("A", "B")
    result = s.runner().run()
    assert result.end_reason == "action_budget"
    assert [e for e in s.events if isinstance(e, tuple) and e[0] == "collect"] == []
    assert {st for st, _ in s.states().values()} == {"pending"}
    assert result.actions_spent == 1100 and result.pending == 2


def test_the_run_budget_never_cuts_a_url_off_midway(setup) -> None:
    s = setup(script={c: Result(actions=150) for c in "ABCDEFGHIJKL"}, urls_per_run=20)
    s.add_urls(*"ABCDEFGHIJKL")
    result = s.runner().run()
    assert result.end_reason == "action_budget"
    assert result.done == 8 and result.actions_spent == 1200
    assert [r["actions_spent"] for r in s.rows("SELECT actions_spent FROM url_attempts")] == [150] * 8
    assert s.states()["I"][0] == "pending"


def test_urls_are_separated_by_pauses_and_batches_end_with_a_longer_break(setup) -> None:
    s = setup(urls_per_run=40)
    codes = [f"U{n:02d}" for n in range(40)]
    s.add_urls(*codes)
    s.runner().run()

    collects = [i for i, e in enumerate(s.events) if isinstance(e, tuple) and e[0] == "collect"]
    gaps = [[e[1] for e in s.events[a + 1:b] if isinstance(e, tuple) and e[0] == "sleep"] for a, b in zip(collects, collects[1:])]
    assert all(30 <= g[0] <= 90 for g in gaps)  # a pause before every next URL
    break_after = [n + 1 for n, g in enumerate(gaps) if len(g) == 2]
    assert break_after and 10 <= break_after[0] <= 15
    assert all(180 <= g[1] <= 480 for g in gaps if len(g) == 2)
    sizes = [b - a for a, b in zip([0] + break_after, break_after)]
    assert all(10 <= size <= 15 for size in sizes)
    assert not [e for e in s.events[collects[-1] + 1:] if isinstance(e, tuple) and e[0] == "sleep"]  # nothing after the last URL


def test_done_partial_and_failed_are_recorded_with_reasons_and_the_post(setup) -> None:
    s = setup(script={
        "A": Result("done"), "B": Result("partial", "cap: depth 5"),
        "C": Result("failed", "not found", items=0), "D": Result("failed", "parse: no posts found", items=0),
    })
    s.add_urls("A", "B", "C", "D")
    result = s.runner().run()
    assert (result.done, result.partial, result.failed) == (1, 1, 2)
    assert s.states() == {"A": ("done", None), "B": ("partial", "cap: depth 5"),
                          "C": ("failed", "not found"), "D": ("failed", "parse: no posts found")}
    assert s.rows("SELECT post_id FROM url_queue WHERE code = 'A'")[0]["post_id"] == "A-0"
    assert s.rows("SELECT last_attempt_at FROM url_queue WHERE code = 'A'")[0]["last_attempt_at"] is not None


def test_capture_runs_only_the_first_pending_url(setup) -> None:
    s = setup()
    s.add_urls("A", "B", "C")
    result = s.runner().run(url_limit=1)
    assert result.end_reason == "url_limit"
    assert [e for e in s.events if isinstance(e, tuple) and e[0] == "collect"] == [("collect", "A")]
    assert s.states()["B"][0] == "pending"


# --- 12.3 stops and errors -----------------------------------------------------------------

def test_blocking_ends_the_run_at_once_keeps_the_data_and_leaves_the_url_pending(setup) -> None:
    s = setup(script={"B": Raise(BlockedError("challenge", "redirected to /challenge/"))})
    s.add_urls("A", "B", "C")
    result = s.runner().run()

    assert result.end_reason == "blocked"
    assert result.stop_details == {"kind": "challenge", "detail": "redirected to /challenge/", "reason": "blocked: challenge"}
    assert [e[1] for e in s.events if isinstance(e, tuple) and e[0] == "collect"] == ["A", "B"]  # C never opened
    assert s.states() == {"A": ("done", None), "B": ("pending", "blocked: challenge"), "C": ("pending", None)}
    assert {r["code"] for r in s.rows("SELECT code FROM posts")} == {"A0", "B0"}  # replies saved so far are kept
    assert (result.done, result.pending) == (1, 2)
    assert result.next_run_allowed == result.ended_at + timedelta(hours=24)
    assert "blocked: challenge" in describe(result)


def test_an_early_warning_ends_the_run_with_the_signs_and_a_shorter_cooldown(setup) -> None:
    warning = EarlyWarning([Signal("empty_reveals", 3, 3)])
    s = setup(script={"A": Raise(warning)})
    s.add_urls("A", "B")
    result = s.runner().run()

    assert result.end_reason == "early_warning"
    assert result.stop_details == {"signals": [{"name": "empty_reveals", "value": 3, "threshold": 3}]}
    assert s.states()["A"][0] == "pending" and "empty_reveals" in s.states()["A"][1]
    assert result.next_run_allowed == result.ended_at + timedelta(hours=6)
    stored = s.rows("SELECT stop_details FROM runs")[0]["stop_details"]
    assert stored["signals"][0]["name"] == "empty_reveals"
    assert "empty_reveals: 3 (threshold 3)" in describe(result)


def test_a_missing_post_fails_only_that_url_and_the_run_continues(setup) -> None:
    s = setup(script={"B": Result("failed", "not found", items=0)})
    s.add_urls("A", "B", "C")
    result = s.runner().run()
    assert result.end_reason == "completed"
    assert (result.done, result.failed) == (2, 1)
    assert s.states()["C"][0] == "done"


def test_an_unexpected_exception_ends_the_run_so_failures_never_come_in_a_series(setup) -> None:
    s = setup(script={"A": Raise(RuntimeError("boom"))})
    s.add_urls("A", "B", "C")
    result = s.runner().run()
    assert result.end_reason == "error" and result.error == "RuntimeError: boom"
    assert s.states() == {"A": ("pending", "error: RuntimeError: boom"), "B": ("pending", None), "C": ("pending", None)}
    assert [e for e in s.events if isinstance(e, tuple) and e[0] == "collect"] == [("collect", "A")]
    assert "RuntimeError: boom" in describe(result)


def test_ctrl_c_during_a_url_keeps_the_data_and_leaves_the_url_pending(setup) -> None:
    s = setup(script={"B": Raise(KeyboardInterrupt())})
    s.add_urls("A", "B", "C")
    result = s.runner().run()
    assert result.end_reason == "interrupted"
    assert s.states() == {"A": ("done", None), "B": ("pending", "interrupted"), "C": ("pending", None)}
    assert {r["code"] for r in s.rows("SELECT code FROM posts")} == {"A0", "B0"}
    assert result.next_run_allowed is None


def test_ctrl_c_during_a_pause_between_urls_stops_before_the_next_one(setup) -> None:
    s = setup()
    s.add_urls("A", "B", "C")
    calls = {"n": 0}

    def sleep(seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt

    s.pacer = Pacer(s.config.pacing, random.Random(1), sleep)
    result = s.runner().run()
    assert result.end_reason == "interrupted"
    assert s.states() == {"A": ("done", None), "B": ("pending", None), "C": ("pending", None)}


def test_blocking_during_the_warm_up_ends_the_run_before_any_url(setup) -> None:
    s = setup(warmup_error=BlockedError("rate limited", "HTTP 429"))
    s.add_urls("A")
    result = s.runner().run()
    assert result.end_reason == "blocked"
    assert [e for e in s.events if isinstance(e, tuple) and e[0] == "collect"] == []
    assert s.states()["A"] == ("pending", None)
    assert s.rows("SELECT 1 FROM url_attempts") == []


def test_running_out_of_budget_in_the_warm_up_ends_the_run_normally(setup) -> None:
    s = setup(warmup=1300)
    s.add_urls("A")
    assert s.runner().run().end_reason == "action_budget"


def test_a_browser_that_cannot_start_ends_the_run_cleanly(setup) -> None:
    s = setup(open_error=RuntimeError("Chrome not found"))
    s.add_urls("A")
    result = s.runner().run()
    assert result.end_reason == "error" and "Chrome not found" in result.error
    assert s.rows("SELECT end_reason::text AS r FROM runs")[0]["r"] == "error"
    assert s.states()["A"] == ("pending", None)


# --- 12.4 the run log -------------------------------------------------------------------------

def test_the_summary_of_eight_done_and_two_partial_matches_the_stored_row(setup) -> None:
    script = {c: Result(actions=15) for c in "ABCDEFGH"} | {c: Result("partial", "cap: action budget", actions=15) for c in "IJ"}
    s = setup(script=script, warmup=5)
    s.add_urls(*"ABCDEFGHIJ")
    result = s.runner().run()

    assert (result.done, result.partial, result.failed, result.pending) == (8, 2, 0, 0)
    assert result.actions_spent == 5 + 10 * 15 and result.end_reason == "completed"
    row = s.rows("SELECT * FROM runs")[0]
    assert (row["urls_done"], row["urls_partial"], row["urls_failed"], row["urls_pending"]) == (8, 2, 0, 0)
    assert row["actions_spent"] == 155 and row["end_reason"] == "completed"
    assert row["started_at"] == result.started_at and row["ended_at"] == result.ended_at
    assert row["cooldown_overridden"] is False and row["ended_at"] >= row["started_at"]
    shown = describe(result)
    assert "8 done, 2 partial, 0 failed, 0 still pending" in shown
    assert "completed" in shown and "155" in shown


def test_an_attempt_records_the_run_the_actions_the_new_items_and_the_outcome(setup) -> None:
    s = setup(script={"A": Result(actions=42, new=30, updated=4)})
    s.add_urls("A")
    result = s.runner().run()
    (attempt,) = s.rows("SELECT a.*, a.outcome::text AS outcome_text, q.code FROM url_attempts a JOIN url_queue q ON q.id = a.url_id")
    assert attempt["run_id"] == result.run_id and attempt["code"] == "A"
    assert (attempt["actions_spent"], attempt["items_new"], attempt["items_updated"]) == (42, 30, 4)
    assert attempt["outcome_text"] == "done" and attempt["reason"] is None
    assert attempt["started_at"] is not None and attempt["ended_at"] is not None


def test_a_stopped_attempt_records_what_was_stored_before_the_stop(setup) -> None:
    s = setup(script={"A": Raise(BlockedError("challenge"), actions=9, items=3)})
    s.add_urls("A")
    s.runner().run()
    (attempt,) = s.rows("SELECT *, outcome::text AS outcome_text FROM url_attempts")
    assert (attempt["actions_spent"], attempt["items_new"], attempt["outcome_text"]) == (9, 3, "pending")
    assert attempt["reason"] == "blocked: challenge"


def test_the_stop_details_are_stored_as_json(setup) -> None:
    s = setup(script={"A": Raise(BlockedError("challenge", "x"))})
    s.add_urls("A")
    s.runner().run()
    details = s.rows("SELECT stop_details FROM runs")[0]["stop_details"]
    assert details["kind"] == "challenge" and details["reason"] == "blocked: challenge"
    json.dumps(details)


def test_attempts_feed_the_collection_yield_view(setup) -> None:
    s = setup(script={"A": Result(actions=60), "B": Result(actions=30)})
    s.add_urls("A")
    s.runner().run()
    s.add_urls("A")  # re-listed: refreshed in a second run
    s.clock.advance(seconds=1)
    s.runner().run()
    row = s.rows("SELECT attempts, actions_spent FROM collection_yield WHERE canonical_url LIKE '%/A'")[0]
    assert (row["attempts"], row["actions_spent"]) == (2, 120)


def test_the_run_log_lists_the_latest_run_first(setup) -> None:
    s = setup()
    s.add_urls("A")
    s.runner().run()
    s.clock.advance(hours=1)
    s.add_urls("B")
    second = s.runner().run()
    recent = s.log.recent(10)
    assert [r["id"] for r in recent] == [second.run_id, second.run_id - 1]


def test_an_early_warning_summary_lists_the_failed_responses_by_kind() -> None:
    from threadsift.collection.runner import describe

    result = RunResult(
        run_id=3, started_at=datetime(2026, 11, 11, 12, 0, tzinfo=UTC), ended_at=datetime(2026, 11, 11, 12, 5, tzinfo=UTC),
        end_reason="early_warning", actions_spent=7, pending=1,
        stop_details={
            "signals": [{"name": "failing_responses", "value": 0.35, "threshold": 0.3}],
            "failed_responses": [
                {"status": 502, "reason": "HTTP 502", "operation": "OpA", "content_type": "text/plain"},
                {"status": 502, "reason": "HTTP 502", "operation": "OpA", "content_type": "text/plain"},
                {"status": 500, "reason": "HTTP 500"},
            ],
        },
    )
    text = describe(result)
    assert "Responses that failed:" in text
    assert "2x HTTP 502, OpA (text/plain)" in text
    assert "1x HTTP 500, unknown operation (no content type)" in text
