import math
import random

import pytest

from threadsift.collection.pacing import Pacer, pointer_path, scroll_gesture
from threadsift.config import PacingConfig


def pacer(seed: int = 1, **overrides) -> tuple[Pacer, list[float]]:
    slept: list[float] = []
    config = PacingConfig(**overrides) if overrides else PacingConfig()
    return Pacer(config, random.Random(seed), slept.append), slept


def test_action_pauses_are_within_the_configured_range() -> None:
    p, slept = pacer()
    for _ in range(500):
        p.action_pause()
    assert min(slept) >= 4 and max(slept) <= 12
    assert max(slept) - min(slept) > 4  # actually random


def test_url_pauses_are_within_the_configured_range() -> None:
    p, slept = pacer()
    for _ in range(500):
        p.url_pause()
    assert min(slept) >= 30 and max(slept) <= 90


def test_ranges_are_configurable() -> None:
    p, slept = pacer(action_pause_seconds=(1.0, 2.0), url_pause_seconds=(5.0, 6.0))
    p.action_pause()
    p.url_pause()
    assert 1 <= slept[0] <= 2 and 5 <= slept[1] <= 6


def test_longer_text_means_a_longer_stay_with_identical_draws() -> None:
    long_pacer, _ = pacer(seed=7)
    short_pacer, _ = pacer(seed=7)
    long_total = long_pacer.action_pause(text_chars=2000)
    short_total = short_pacer.action_pause(text_chars=100)
    assert long_total > short_total
    # Identical draws: the whole difference is the dwell for 1900 more characters
    # (0.01 s each, within the +/-25% jitter).
    assert 19 * 0.75 <= long_total - short_total <= 19 * 1.25


def test_dwell_is_proportional_to_text_with_jitter_bounds() -> None:
    p, _ = pacer(dwell_seconds_per_char=0.01, dwell_jitter=0.25)
    for _ in range(200):
        assert 15 <= p.dwell_seconds(2000) <= 25
    assert p.dwell_seconds(0) == 0


def test_dwell_jitter_makes_stays_vary() -> None:
    p, _ = pacer()
    assert len({round(p.dwell_seconds(1000), 3) for _ in range(20)}) > 1


def test_break_comes_after_the_drawn_batch_size_then_a_new_batch_is_drawn() -> None:
    p, _ = pacer(seed=3)
    schedule = p.breaks()
    for batch in range(5):
        processed = 0
        while True:
            processed += 1
            pause = schedule.url_processed()
            if pause is not None:
                break
        assert 10 <= processed <= 15, f"batch {batch}"
        assert 3 * 60 <= pause <= 8 * 60


def test_batch_sizes_and_break_lengths_vary() -> None:
    p, _ = pacer(seed=5)
    schedule = p.breaks()
    sizes, lengths, count = set(), set(), 0
    for _ in range(600):
        count += 1
        pause = schedule.url_processed()
        if pause is not None:
            sizes.add(count)
            lengths.add(round(pause))
            count = 0
    assert len(sizes) > 1 and len(lengths) > 1


def test_break_settings_are_configurable() -> None:
    p, _ = pacer(urls_per_break=(2, 2), break_minutes=(1.0, 1.0))
    schedule = p.breaks()
    assert [schedule.url_processed() for _ in range(4)] == [None, 60.0, None, 60.0]


def test_take_break_sleeps_for_the_given_time() -> None:
    p, slept = pacer()
    p.take_break(123.0)
    assert slept == [123.0]


def test_warmup_is_between_one_and_three_minutes_by_default() -> None:
    p, _ = pacer()
    draws = [p.warmup_seconds() for _ in range(200)]
    assert all(60 <= d <= 180 for d in draws)
    assert len(set(draws)) > 1


# --- pointer paths and scrolling ----------------------------------------

def distance_from_line(point, start, end) -> float:
    (x, y), (x1, y1), (x2, y2) = point, start, end
    return abs((y2 - y1) * x - (x2 - x1) * y + x2 * y1 - y2 * x1) / math.hypot(x2 - x1, y2 - y1)


@pytest.mark.parametrize("seed", range(10))
def test_pointer_path_starts_and_ends_exactly_at_the_endpoints(seed: int) -> None:
    path = pointer_path((100, 100), (700, 400), random.Random(seed))
    assert path[0] == (100, 100)
    assert path[-1] == (700, 400)
    assert len(path) >= 13


@pytest.mark.parametrize("seed", range(10))
def test_pointer_path_is_not_a_straight_line(seed: int) -> None:
    start, end = (100.0, 100.0), (700.0, 400.0)
    path = pointer_path(start, end, random.Random(seed))
    assert max(distance_from_line(p, start, end) for p in path) > 3


def test_pointer_paths_differ_between_moves() -> None:
    a = pointer_path((0, 0), (500, 0), random.Random(1))
    b = pointer_path((0, 0), (500, 0), random.Random(2))
    assert a != b


def test_pointer_speed_varies_along_the_path() -> None:
    path = pointer_path((0, 0), (800, 0), random.Random(4))
    gaps = [math.dist(a, b) for a, b in zip(path, path[1:])]
    assert max(gaps) > 2.5 * min(gaps)


def test_pointer_path_to_the_same_point_is_just_that_point() -> None:
    assert pointer_path((5, 5), (5, 5), random.Random(0)) == [(5, 5)]


@pytest.mark.parametrize("distance", [300, 900, 1500, -600])
def test_scroll_gesture_adds_up_to_the_distance(distance: int) -> None:
    for seed in range(20):
        ticks = scroll_gesture(distance, random.Random(seed))
        assert sum(t.delta_y for t in ticks) == distance
        assert all(0.05 <= t.delay_before <= 0.5 for t in ticks)


def test_scroll_steps_are_uneven() -> None:
    sizes = {abs(t.delta_y) for seed in range(10) for t in scroll_gesture(1200, random.Random(seed))}
    assert len(sizes) > 10


def test_scroll_sometimes_steps_back() -> None:
    backs = sum(
        1 for seed in range(100)
        for t in scroll_gesture(1200, random.Random(seed)) if t.delta_y < 0
    )
    assert 0 < backs < 200


def test_scroll_back_is_small() -> None:
    for seed in range(100):
        for t in scroll_gesture(1200, random.Random(seed)):
            if t.delta_y < 0:
                assert abs(t.delta_y) <= 80


def test_dwell_counts_at_most_a_screenful_of_text() -> None:
    long_pacer, _ = pacer(seed=9)
    capped_pacer, _ = pacer(seed=9)
    assert long_pacer.action_pause(text_chars=80_000) == capped_pacer.action_pause(text_chars=2_000)
    p, slept = pacer()
    for _ in range(100):
        p.action_pause(text_chars=500_000)
    assert max(slept) <= 12 + 0.01 * 2000 * 1.25


def test_the_dwell_cap_is_configurable() -> None:
    p, slept = pacer(dwell_max_chars=100, action_pause_seconds=(0.0, 0.0), dwell_jitter=0.0)
    p.action_pause(text_chars=10_000)
    assert slept == [pytest.approx(1.0)]
