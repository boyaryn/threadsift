import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from threadsift.config import PacingConfig

Point = tuple[float, float]


def real_sleep(seconds: float) -> None:
    time.sleep(seconds)


class Pacer:
    """Randomized waits. The random source and the sleep function are injectable
    so tests run instantly and deterministically."""

    def __init__(
        self,
        config: PacingConfig,
        rng: random.Random | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._config = config
        self._rng = rng or random.Random()
        self._sleep = sleep or real_sleep

    def dwell_seconds(self, text_chars: int) -> float:
        """Time to stay on a page, longer for more visible text, with jitter."""
        # A person reads about a screenful, however long the page grows.
        text_chars = min(text_chars, self._config.dwell_max_chars)
        base = self._config.dwell_seconds_per_char * max(text_chars, 0)
        jitter = self._config.dwell_jitter
        return base * (1 + self._rng.uniform(-jitter, jitter))

    def action_pause(self, text_chars: int = 0) -> float:
        """Wait between page actions, plus a dwell for the text on the page."""
        low, high = self._config.action_pause_seconds
        seconds = self._rng.uniform(low, high) + self.dwell_seconds(text_chars)
        self._sleep(seconds)
        return seconds

    def url_pause(self) -> float:
        low, high = self._config.url_pause_seconds
        seconds = self._rng.uniform(low, high)
        self._sleep(seconds)
        return seconds

    def warmup_seconds(self) -> float:
        low, high = self._config.warmup_minutes
        return self._rng.uniform(low, high) * 60

    def breaks(self) -> "BreakSchedule":
        return BreakSchedule(self._config, self._rng)

    def take_break(self, seconds: float) -> None:
        self._sleep(seconds)

    def pointer_path(self, start: Point, end: Point) -> list[Point]:
        return pointer_path(start, end, self._rng)

    def scroll_gesture(self, distance: int) -> list["WheelTick"]:
        return scroll_gesture(distance, self._rng)


class BreakSchedule:
    """A long break after every random batch of processed URLs."""

    def __init__(self, config: PacingConfig, rng: random.Random) -> None:
        self._config = config
        self._rng = rng
        self._left = self._draw_batch()

    def _draw_batch(self) -> int:
        low, high = self._config.urls_per_break
        return self._rng.randint(int(low), int(high))

    def url_processed(self) -> float | None:
        """Call after each processed URL. Returns the break length in seconds when
        the batch is complete (and draws the next batch size), otherwise None."""
        self._left -= 1
        if self._left > 0:
            return None
        self._left = self._draw_batch()
        low, high = self._config.break_minutes
        return self._rng.uniform(low, high) * 60


def _smoothstep(t: float) -> float:
    return t * t * (3 - 2 * t)


def pointer_path(start: Point, end: Point, rng: random.Random) -> list[Point]:
    """A curved, jittered path from start to end. Points are evenly spaced in
    time but eased, so the pointer speeds up and slows down. It starts exactly
    at `start` and ends exactly at `end`."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    distance = math.hypot(dx, dy)
    if distance == 0:
        return [start]
    normal = (-dy / distance, dx / distance)

    def control(fraction: float) -> Point:
        offset = rng.uniform(-0.25, 0.25) * distance
        return (
            start[0] + dx * fraction + normal[0] * offset,
            start[1] + dy * fraction + normal[1] * offset,
        )

    c1, c2 = control(0.3), control(0.7)
    steps = max(12, min(60, int(distance / 12) + rng.randint(-3, 3)))
    points: list[Point] = []
    for i in range(steps + 1):
        t = _smoothstep(i / steps)
        u = 1 - t
        x = u**3 * start[0] + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t**3 * end[0]
        y = u**3 * start[1] + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t**3 * end[1]
        if 0 < i < steps:
            x += rng.uniform(-1.5, 1.5)
            y += rng.uniform(-1.5, 1.5)
        points.append((x, y))
    return points


@dataclass(frozen=True)
class WheelTick:
    delta_y: int
    delay_before: float


def scroll_gesture(distance: int, rng: random.Random) -> list[WheelTick]:
    """One scroll gesture (one counted page action): several mouse-wheel ticks of
    uneven size with short pauses, sometimes with a small step back. The ticks add
    up to exactly `distance` pixels."""
    ticks: list[WheelTick] = []
    remaining = distance
    while abs(remaining) > 60:
        size = int(rng.uniform(0.15, 0.5) * abs(remaining)) + 40
        size = min(size, abs(remaining))
        delta = size if remaining > 0 else -size
        ticks.append(WheelTick(delta, rng.uniform(0.05, 0.4)))
        remaining -= delta
        if abs(remaining) > 120 and rng.random() < 0.15:
            back = -int(rng.uniform(20, 80)) * (1 if remaining > 0 else -1)
            ticks.append(WheelTick(back, rng.uniform(0.1, 0.5)))
            remaining -= back
    if remaining:
        ticks.append(WheelTick(remaining, rng.uniform(0.05, 0.4)))
    return ticks
