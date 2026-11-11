"""The run loop: cool-down check, warm-up, one URL at a time, and how a run ends."""
import random
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import Engine

from threadsift.archive.repository import ArchiveRepository
from threadsift.archive.runs import RunLog
from threadsift.collection.budget import ActionBudget, BudgetExhausted
from threadsift.collection.expansion import ExpansionResult
from threadsift.collection.guard import BlockedError
from threadsift.collection.monitor import EarlyWarning
from threadsift.collection.pacing import Pacer
from threadsift.config import Config, CooldownConfig
from threadsift.domain import CollectedItem, RunResult, StoreOutcome, StoreSummary
from threadsift.intake.queue import QueuedUrl, UrlQueue

OpenCollector = Callable[
    [ActionBudget, Callable[[CollectedItem], StoreOutcome]], AbstractContextManager[Any]
]


class Collector(Protocol):
    def warm_up(self) -> None: ...

    def collect(self, queued: QueuedUrl) -> ExpansionResult: ...


class CooldownActive(Exception):
    def __init__(self, reason: str, until: datetime, remaining: timedelta) -> None:
        self.reason, self.until, self.remaining = reason, until, remaining
        super().__init__(f"cool-down after a run that ended on {reason.replace('_', ' ')}")


def cooldown_hours(reason: str, config: CooldownConfig) -> float | None:
    return {"blocked": config.blocked_hours, "early_warning": config.early_warning_hours}.get(reason)


def cooldown_status(run_log: RunLog, config: CooldownConfig, now: datetime) -> CooldownActive | None:
    """A cool-down applies while the latest finished run ended blocked or on early warning."""
    latest = run_log.latest_finished()
    if latest is None:
        return None
    hours = cooldown_hours(latest.end_reason, config)
    if hours is None:
        return None
    until = latest.ended_at + timedelta(hours=hours)
    return CooldownActive(latest.end_reason, until, until - now) if until > now else None


def describe_wait(remaining: timedelta) -> str:
    minutes = max(1, round(remaining.total_seconds() / 60))
    if minutes >= 90:
        return f"about {round(minutes / 60)} hours"
    return f"about {minutes} minutes"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _Stop(Exception):
    """Internal: ends the run loop with a reason."""

    def __init__(self, end_reason: str, details: dict | None = None, error: str | None = None) -> None:
        self.end_reason, self.details, self.error = end_reason, details, error


class Runner:
    def __init__(
        self,
        engine: Engine,
        config: Config,
        open_collector: OpenCollector,
        clock: Callable[[], datetime] = _utcnow,
        pacer: Pacer | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._config = config
        self._open_collector = open_collector
        self._clock = clock
        self._pacer = pacer or Pacer(config.pacing, rng)
        self._queue = UrlQueue(engine, clock)
        self._archive = ArchiveRepository(engine, clock)
        self._log = RunLog(engine)

    def run(self, override_cooldown: bool = False, url_limit: int | None = None) -> RunResult:
        """Collect pending URLs. Raises CooldownActive, before opening anything, if a
        cool-down applies and was not overridden."""
        started = self._clock()
        overridden = False
        cooldown = cooldown_status(self._log, self._config.cooldown, started)
        if cooldown is not None:
            if not override_cooldown:
                raise cooldown
            overridden = True

        limit = url_limit or self._config.limits.urls_per_run
        picked = self._queue.pending(limit)
        run_id = self._log.start_run(started, overridden)
        counts = {"done": 0, "partial": 0, "failed": 0}
        budget = ActionBudget(self._config.limits.run_action_budget, self._config.limits.url_action_budget)
        end_reason, details, error = "completed", None, None

        if picked:
            try:
                with self._open_collector(budget, self._archive.store_item) as collector:
                    end_reason = self._loop(run_id, collector, budget, picked, counts, limit)
            except _Stop as stop:
                end_reason, details, error = stop.end_reason, stop.details, stop.error
            except KeyboardInterrupt:
                end_reason = "interrupted"
            except Exception as failure:  # e.g. the browser could not start
                stop = self._stop_for(failure)
                end_reason, details, error = stop.end_reason, stop.details, stop.error

        return self._finish(run_id, started, overridden, end_reason, details, error, budget, counts, len(picked))

    # --- the loop -----------------------------------------------------------------

    def _loop(self, run_id: int, collector: Collector, budget: ActionBudget,
              picked: list[QueuedUrl], counts: dict[str, int], limit: int) -> str:
        schedule = self._pacer.breaks()
        try:
            collector.warm_up()
        except Exception as error:
            raise self._stop_for(error) from error
        for index, queued in enumerate(picked):
            if not budget.can_start_url():
                return "action_budget"
            self._process(run_id, collector, budget, queued, counts)
            if index + 1 < len(picked):
                self._between_urls(schedule)
        return "url_limit" if len(picked) >= limit and self._queue.pending(1) else "completed"

    def _between_urls(self, schedule: Any) -> None:
        self._pacer.url_pause()
        pause = schedule.url_processed()
        if pause is not None:
            self._pacer.take_break(pause)

    def _process(self, run_id: int, collector: Collector, budget: ActionBudget,
                 queued: QueuedUrl, counts: dict[str, int]) -> None:
        attempt = self._log.start_attempt(run_id, queued.id, self._clock())
        budget.start_url()
        try:
            result = collector.collect(queued)
        except (Exception, KeyboardInterrupt) as error:
            actions = budget.end_url()
            summary = getattr(collector, "last_summary", None) or StoreSummary()
            reason = self._stop_reason(error)
            self._log.finish_attempt(attempt, self._clock(), actions, summary.new, summary.updated, "pending", reason)
            self._queue.record_result(queued.id, "pending", reason)
            if isinstance(error, KeyboardInterrupt):
                raise
            raise self._stop_for(error) from error
        actions = budget.end_url()
        self._queue.record_result(queued.id, result.state, result.reason, result.post_id)
        self._log.finish_attempt(
            attempt, self._clock(), actions, result.summary.new, result.summary.updated, result.state, result.reason
        )
        counts[result.state] += 1

    # --- how a run ends -------------------------------------------------------------

    @staticmethod
    def _stop_reason(error: BaseException) -> str:
        if isinstance(error, BlockedError):
            return error.reason
        if isinstance(error, EarlyWarning):
            return str(error)
        if isinstance(error, KeyboardInterrupt):
            return "interrupted"
        if isinstance(error, BudgetExhausted):
            return "cap: action budget"
        return f"error: {type(error).__name__}: {error}"

    def _stop_for(self, error: Exception) -> _Stop:
        if isinstance(error, BlockedError):
            return _Stop("blocked", {"kind": error.kind, "detail": error.detail, "reason": error.reason})
        if isinstance(error, EarlyWarning):
            return _Stop("early_warning", error.details())
        if isinstance(error, BudgetExhausted):
            return _Stop("action_budget", {"scope": error.scope})
        text = f"{type(error).__name__}: {error}"
        return _Stop("error", {"error": text}, text)

    def _finish(self, run_id: int, started: datetime, overridden: bool, end_reason: str,
                details: dict | None, error: str | None, budget: ActionBudget,
                counts: dict[str, int], picked: int) -> RunResult:
        ended = self._clock()
        pending = picked - sum(counts.values())
        self._log.finish_run(run_id, ended, end_reason, details, budget.run_spent,
                             counts["done"], counts["partial"], counts["failed"], pending)
        hours = cooldown_hours(end_reason, self._config.cooldown)
        return RunResult(
            run_id=run_id, started_at=started, ended_at=ended, end_reason=end_reason,
            actions_spent=budget.run_spent, done=counts["done"], partial=counts["partial"],
            failed=counts["failed"], pending=pending, stop_details=details,
            cooldown_overridden=overridden, error=error,
            next_run_allowed=ended + timedelta(hours=hours) if hours is not None else None,
        )


END_REASON_TEXT = {
    "completed": "completed: nothing more is pending",
    "url_limit": "run limit reached",
    "action_budget": "action budget reached",
    "early_warning": "stopped on an early warning sign",
    "blocked": "stopped: blocking was detected",
    "interrupted": "interrupted",
    "error": "stopped on an unexpected error",
}


def _failed_response_lines(failed: list[dict] | None) -> list[str]:
    """One line per kind of failed response: how many, what went wrong, which operation."""
    if not failed:
        return []
    kinds: dict[tuple, int] = {}
    for note in failed:
        key = (note.get("reason"), note.get("operation") or "unknown operation", note.get("content_type") or "no content type")
        kinds[key] = kinds.get(key, 0) + 1
    lines = ["Responses that failed:"]
    for (reason, operation, content_type), count in sorted(kinds.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {count}x {reason}, {operation} ({content_type})")
    return lines


def describe(result: RunResult) -> str:
    lines = [f"Run {result.run_id} ended: {END_REASON_TEXT.get(result.end_reason, result.end_reason)}."]
    if result.stop_details:
        if result.end_reason == "blocked":
            lines.append(f"Detected: {result.stop_details.get('reason')} ({result.stop_details.get('detail')})")
        elif result.end_reason == "early_warning":
            signals = "; ".join(
                f"{s['name']}: {s['value']:g} (threshold {s['threshold']:g})"
                for s in result.stop_details.get("signals", [])
            )
            lines.append(f"Signs seen: {signals}")
            lines.extend(_failed_response_lines(result.stop_details.get("failed_responses")))
    if result.error:
        lines.append(f"Error: {result.error}")
    lines.append(
        f"URLs: {result.done} done, {result.partial} partial, {result.failed} failed, "
        f"{result.pending} still pending. Page actions spent: {result.actions_spent}."
    )
    if result.cooldown_overridden:
        lines.append("This run was started during a cool-down (overridden).")
    if result.next_run_allowed is not None:
        lines.append(f"Runs are possible again after {result.next_run_allowed:%Y-%m-%d %H:%M} UTC.")
    return "\n".join(lines)
