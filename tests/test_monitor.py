import pytest

from threadsift.collection.monitor import EarlyWarning, Monitor
from threadsift.config import EarlyWarningConfig


def monitor(**overrides) -> Monitor:
    return Monitor(EarlyWarningConfig(**overrides))


# --- empty reveals --------------------------------------------------------

def test_three_empty_reveals_in_a_row_fire_at_the_threshold() -> None:
    m = monitor()
    m.record_reveal(0, more_expected=True)
    m.record_reveal(0, more_expected=True)
    with pytest.raises(EarlyWarning) as warning:
        m.record_reveal(0, more_expected=True)
    assert [s.name for s in warning.value.signals] == ["empty_reveals"]
    assert warning.value.signals[0].value == 3
    assert warning.value.signals[0].threshold == 3


def test_threshold_is_configurable() -> None:
    m = monitor(empty_reveals=2)
    m.record_reveal(0, True)
    with pytest.raises(EarlyWarning):
        m.record_reveal(0, True)


def test_a_reveal_that_loads_one_item_resets_the_streak() -> None:
    m = monitor()
    for _ in range(10):
        m.record_reveal(0, True)
        m.record_reveal(0, True)
        m.record_reveal(1, True)


def test_deleted_reply_gaps_with_working_reveals_do_not_fire() -> None:
    # The post shows fewer replies than its count (some were deleted), but every
    # reveal control still loads content, and the last ones have nothing to load.
    m = monitor()
    for loaded in (5, 5, 3, 1):
        m.record_reveal(loaded, more_expected=True)
    for _ in range(20):
        m.record_reveal(0, more_expected=False)


def test_empty_reveals_when_nothing_more_is_expected_do_not_count() -> None:
    m = monitor()
    for _ in range(50):
        m.record_reveal(0, more_expected=False)


def test_the_streak_keeps_counting_across_urls() -> None:
    m = monitor()
    m.record_reveal(0, True)
    m.record_reveal(0, True)
    with pytest.raises(EarlyWarning):
        m.record_reveal(0, True)


# --- failing responses ----------------------------------------------------

def test_failing_responses_fire_above_thirty_percent_of_the_last_twenty() -> None:
    m = monitor()
    for _ in range(14):
        m.record_response(True)
    for _ in range(6):
        m.record_response(False)  # 6 of 20 = 30% exactly: not above
    with pytest.raises(EarlyWarning) as warning:
        m.record_response(False)  # window slides: 7 of 20 = 35%
    signal = warning.value.signals[0]
    assert signal.name == "failing_responses"
    assert signal.value == pytest.approx(0.35)
    assert signal.threshold == 0.3


def test_no_verdict_until_the_window_is_full() -> None:
    m = monitor()
    for _ in range(19):
        m.record_response(False)
    with pytest.raises(EarlyWarning):
        m.record_response(False)


def test_old_failures_slide_out_of_the_window() -> None:
    m = monitor()
    for _ in range(6):
        m.record_response(False)
    for _ in range(500):
        m.record_response(True)


def test_occasional_failures_do_not_fire() -> None:
    m = monitor()
    for i in range(200):
        m.record_response(i % 10 != 0)  # 10% failing


# --- slowdown -------------------------------------------------------------

def test_slow_pages_fire_when_the_recent_median_is_over_three_times_the_early_median() -> None:
    m = monitor()
    for _ in range(5):
        m.record_page_load(2.0)
    for _ in range(4):
        m.record_page_load(7.0)
    with pytest.raises(EarlyWarning) as warning:
        m.record_page_load(7.0)
    signal = warning.value.signals[0]
    assert signal.name == "slow_responses"
    assert signal.value == pytest.approx(3.5)
    assert signal.threshold == 3


def test_exactly_three_times_slower_does_not_fire() -> None:
    m = monitor()
    for _ in range(5):
        m.record_page_load(2.0)
    for _ in range(20):
        m.record_page_load(6.0)


def test_one_slow_page_among_normal_ones_does_not_fire() -> None:
    m = monitor()
    for _ in range(5):
        m.record_page_load(2.0)
    for i in range(30):
        m.record_page_load(30.0 if i % 5 == 0 else 2.5)


def test_normal_variation_does_not_fire() -> None:
    m = monitor()
    for seconds in [2, 3, 2.5, 4, 3, 2, 5, 3, 4, 3.5, 2.8, 4.2, 3.1]:
        m.record_page_load(seconds)


def test_no_verdict_before_both_windows_exist() -> None:
    m = monitor()
    for _ in range(5):
        m.record_page_load(1.0)
    for _ in range(4):
        m.record_page_load(100.0)


def test_slowdown_window_and_factor_are_configurable() -> None:
    m = monitor(slowdown_pages=2, slowdown_factor=2.0)
    m.record_page_load(1.0)
    m.record_page_load(1.0)
    m.record_page_load(2.5)
    with pytest.raises(EarlyWarning):
        m.record_page_load(2.5)


# --- the warning itself ---------------------------------------------------

def test_warning_carries_signal_names_and_values_for_the_run_log() -> None:
    m = monitor()
    m.record_reveal(0, True)
    m.record_reveal(0, True)
    with pytest.raises(EarlyWarning) as warning:
        m.record_reveal(0, True)
    assert warning.value.details() == {
        "signals": [{"name": "empty_reveals", "value": 3, "threshold": 3}]
    }
    assert "empty_reveals" in str(warning.value)
