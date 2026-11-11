import statistics
from collections import deque
from dataclasses import dataclass

from threadsift.config import EarlyWarningConfig


@dataclass(frozen=True)
class Signal:
    name: str
    value: float
    threshold: float

    def describe(self) -> str:
        return f"{self.name}: {self.value:g} (threshold {self.threshold:g})"


class EarlyWarning(Exception):
    """Signs of quiet throttling passed their thresholds. The run must stop at once."""

    def __init__(self, signals: list[Signal], failed_responses: list[dict] | None = None) -> None:
        self.signals = signals
        self.failed_responses = failed_responses or []
        super().__init__("early warning: " + "; ".join(s.describe() for s in signals))

    def details(self) -> dict:
        details: dict = {
            "signals": [
                {"name": s.name, "value": s.value, "threshold": s.threshold} for s in self.signals
            ]
        }
        if self.failed_responses:  # which responses failed, and how, so the cause can be judged
            details["failed_responses"] = self.failed_responses
        return details


class Monitor:
    """Rolling statistics for one run; each record_* call raises EarlyWarning
    when its signal passes the configured threshold."""

    def __init__(self, config: EarlyWarningConfig) -> None:
        self._config = config
        self._empty_reveals = 0
        self._responses: deque[bool] = deque(maxlen=config.failure_window)
        self._notes: deque[dict | None] = deque(maxlen=config.failure_window)  # what was wrong with each failure
        self._load_times: list[float] = []

    def record_reveal(self, new_items: int, more_expected: bool) -> None:
        """A reveal action (scroll, "view more replies", ...) finished.

        It counts toward the empty streak only if it loaded nothing although the
        known reply counts say more replies exist. Loading at least one item
        resets the streak.
        """
        if new_items > 0:
            self._empty_reveals = 0
            return
        if not more_expected:
            return
        self._empty_reveals += 1
        if self._empty_reveals >= self._config.empty_reveals:
            raise EarlyWarning(
                [Signal("empty_reveals", self._empty_reveals, self._config.empty_reveals)]
            )

    def record_response(self, ok: bool, note: dict | None = None) -> None:
        """A GraphQL response arrived; `ok` is False if it failed or came back
        empty when content was expected. `note` says what was wrong (kept for the
        warning's details)."""
        self._responses.append(ok)
        self._notes.append(None if ok else (note or {}))
        window = self._config.failure_window
        if len(self._responses) < window:
            return
        ratio = self._responses.count(False) / window
        if ratio > self._config.failure_ratio:
            raise EarlyWarning(
                [Signal("failing_responses", round(ratio, 3), self._config.failure_ratio)],
                [n for n in self._notes if n],
            )

    def record_page_load(self, seconds: float) -> None:
        """A post page finished loading (do not record the home-feed warm-up)."""
        self._load_times.append(seconds)
        n = self._config.slowdown_pages
        if len(self._load_times) < 2 * n:
            return
        baseline = statistics.median(self._load_times[:n])
        recent = statistics.median(self._load_times[-n:])
        if recent > self._config.slowdown_factor * baseline:
            raise EarlyWarning(
                [
                    Signal(
                        "slow_responses",
                        round(recent / baseline, 2) if baseline else float("inf"),
                        self._config.slowdown_factor,
                    )
                ]
            )
