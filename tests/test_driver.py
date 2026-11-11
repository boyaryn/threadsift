import random

import pytest

from threadsift.collection.browser import (
    HOME_URL,
    Control,
    ControlNotVisible,
    PageDriver,
)
from threadsift.collection.budget import ActionBudget, BudgetExhausted
from threadsift.collection.controls import ForbiddenControl
from threadsift.collection.guard import BlockedError
from threadsift.collection.monitor import EarlyWarning, Monitor
from threadsift.collection.pacing import Pacer
from threadsift.config import EarlyWarningConfig, PacingConfig

from tests.fakes import FakePage, Timeline, reveal_element

POST = "https://www.threads.com/@someone/post/ABC123"


def make_driver(elements=None, run_budget=1200, url_budget=150, monitor=None, **page_args):
    timeline = Timeline()
    page = FakePage(timeline, elements=elements, **page_args)
    pacer = Pacer(PacingConfig(), random.Random(1), timeline.sleep)
    budget = ActionBudget(run_budget, url_budget)
    driver = PageDriver(page, budget, pacer, monitor, timeline.clock, timeline.sleep, random.Random(2))
    return driver, page, budget, timeline


# --- goto -----------------------------------------------------------------

def test_goto_opens_the_page_counts_one_action_and_pauses() -> None:
    driver, page, budget, timeline = make_driver(text_length=2000)
    result = driver.goto(POST)
    assert page.calls == [("goto", POST)]
    assert budget.run_spent == 1
    assert result.status == 200 and result.load_seconds == pytest.approx(1.5)
    assert timeline.slept and timeline.slept[-1] >= 4  # the pause after the action


def test_a_longer_page_means_a_longer_stay() -> None:
    long_driver, _, _, long_time = make_driver(text_length=4000)
    short_driver, _, _, short_time = make_driver(text_length=100)
    long_driver.goto(POST)
    short_driver.goto(POST)
    assert long_time.slept[-1] > short_time.slept[-1]


def test_load_times_feed_the_monitor_except_for_the_feed_warmup() -> None:
    monitor = Monitor(EarlyWarningConfig(slowdown_pages=1, slowdown_factor=2.0))
    driver, page, _, _ = make_driver(monitor=monitor)
    driver.goto(HOME_URL, record_load=False)
    driver.goto(POST)  # first recorded: baseline 1.5
    page.load_seconds = 4.0
    with pytest.raises(EarlyWarning):
        driver.goto(POST)


@pytest.mark.parametrize(
    "target", ["https://www.threads.com/login", "https://www.threads.com/challenge/", "https://www.threads.com/accounts/suspended/"]
)
def test_redirect_to_a_blocking_page_raises_blocked(target: str) -> None:
    driver, page, _, _ = make_driver()
    page.redirects[POST] = target
    with pytest.raises(BlockedError):
        driver.goto(POST)


def test_login_form_on_the_page_raises_blocked() -> None:
    driver, page, _, _ = make_driver()
    page.login_form = True
    with pytest.raises(BlockedError, match="login wall"):
        driver.goto(POST)


def test_challenge_dialog_raises_blocked() -> None:
    driver, page, _, _ = make_driver()
    page.dialogs = ["We detected suspicious activity. Confirm it's you."]
    with pytest.raises(BlockedError, match="challenge"):
        driver.goto(POST)


def test_http_429_on_navigation_raises_blocked() -> None:
    driver, page, _, _ = make_driver()
    page.statuses[POST] = 429
    with pytest.raises(BlockedError, match="rate limited"):
        driver.goto(POST)


def test_not_found_status_is_returned_not_treated_as_blocking() -> None:
    driver, page, _, _ = make_driver()
    page.statuses[POST] = 404
    assert driver.goto(POST).status == 404


# --- scroll ---------------------------------------------------------------

def test_scroll_is_one_action_made_of_uneven_wheel_ticks_that_add_up() -> None:
    driver, page, budget, timeline = make_driver()
    driver.scroll(900)
    wheels = [c[2] for c in page.calls if c[0] == "mouse.wheel"]
    assert len(wheels) >= 2 and sum(wheels) == 900
    assert budget.run_spent == 1
    assert any(c[0] == "mouse.move" for c in page.calls)  # the pointer was placed first


def test_pointer_is_placed_only_before_the_first_scroll() -> None:
    driver, page, _, _ = make_driver()
    driver.scroll(500)
    moves_after_first = len([c for c in page.calls if c[0] == "mouse.move"])
    driver.scroll(500)
    assert len([c for c in page.calls if c[0] == "mouse.move"]) == moves_after_first


def test_scroll_checks_for_blocking_afterwards() -> None:
    driver, page, _, _ = make_driver()
    page.on_scroll = lambda: page.dialogs.append("Try again later")
    with pytest.raises(BlockedError, match="rate limited"):
        driver.scroll(500)


# --- clicking -------------------------------------------------------------

def test_reveal_controls_lists_only_content_revealing_controls() -> None:
    elements = [reveal_element("Like"), reveal_element("View more replies"), reveal_element("Reply"),
                reveal_element("View 3 more replies"), reveal_element("Follow")]
    driver, _, _, _ = make_driver(elements)
    assert driver.reveal_controls() == [Control("View more replies", 1), Control("View 3 more replies", 3)]


def test_click_uses_real_pointer_events_along_a_path() -> None:
    driver, page, budget, _ = make_driver([reveal_element("View more replies", y=300)])
    (control,) = driver.reveal_controls()
    driver.click(control)

    clicks = [c for c in page.calls if c[0] == "mouse.click"]
    moves = [c for c in page.calls if c[0] == "mouse.move"]
    assert len(clicks) == 1 and len(moves) >= 12
    _, x, y = clicks[0]
    assert 100 <= x <= 300 and 300 <= y <= 330  # inside the control
    assert (moves[-1][1], moves[-1][2]) == (x, y)  # the path ends where the click happens
    assert budget.run_spent == 1


def test_clicking_an_action_control_is_refused_and_does_nothing() -> None:
    driver, page, budget, _ = make_driver([reveal_element("Like"), reveal_element("Follow"), reveal_element("Reply")])
    for index, label in enumerate(["Like", "Follow", "Reply"]):
        with pytest.raises(ForbiddenControl) as refused:
            driver.click(Control("View more replies", index))  # even if someone claims it is a reveal control
        assert refused.value.label == label
    assert page.calls == []
    assert budget.run_spent == 0


def test_a_control_whose_label_changed_since_it_was_listed_is_refused() -> None:
    elements = [reveal_element("View more replies")]
    driver, page, budget, _ = make_driver(elements)
    (control,) = driver.reveal_controls()
    elements[0]["label"] = "Repost"  # the page changed under us
    with pytest.raises(ForbiddenControl):
        driver.click(control)
    assert page.calls == [] and budget.run_spent == 0


def test_control_outside_the_window_must_be_scrolled_to_first() -> None:
    driver, page, budget, _ = make_driver([reveal_element(y=900)])  # window is 800 high
    (control,) = driver.reveal_controls()
    with pytest.raises(ControlNotVisible):
        driver.click(control)
    assert page.calls == [] and budget.run_spent == 0


def test_scripted_clicks_are_impossible() -> None:
    driver, page, _, _ = make_driver([reveal_element()])
    (control,) = driver.reveal_controls()
    driver.click(control)  # the fake locator raises if element.click() or dispatch_event is used


# --- the budget cannot be bypassed ---------------------------------------

def test_every_action_is_refused_when_the_budget_is_gone_and_touches_nothing() -> None:
    driver, page, budget, _ = make_driver([reveal_element()], run_budget=1, url_budget=1)
    (control,) = driver.reveal_controls()
    budget.spend(1)
    for action in (lambda: driver.goto(POST), lambda: driver.scroll(500), lambda: driver.click(control)):
        with pytest.raises(BudgetExhausted):
            action()
    assert page.action_calls == []
    assert [c for c in page.calls if c[0].startswith("mouse")] == []


def test_every_kind_of_action_is_counted() -> None:
    driver, page, budget, _ = make_driver([reveal_element()])
    budget.start_url()
    driver.goto(POST)
    driver.scroll(600)
    (control,) = driver.reveal_controls()
    driver.click(control)
    assert budget.url_spent == 3 == budget.run_spent
    assert len(page.action_calls) >= 3


def test_the_raw_page_is_not_exposed() -> None:
    driver, page, _, _ = make_driver()
    public = [name for name in dir(driver) if not name.startswith("_")]
    assert set(public) == {
        "current_url", "visible_text_length", "at_bottom", "remaining_px", "goto", "scroll", "scroll_screen",
        "reveal_controls", "click", "warm_up_home_feed",
    }
    assert not any(getattr(driver, name, None) is page for name in public)


def test_at_bottom_is_a_read_not_an_action() -> None:
    driver, page, budget, _ = make_driver()
    page.at_bottom = False
    assert driver.at_bottom() is False
    page.at_bottom = True
    assert driver.at_bottom() is True
    assert budget.run_spent == 0 and page.calls == []


# --- scrolling about a screen at a time ------------------------------------------

def screen_scroll_distance(viewport_height: int, remaining: int, seed: int = 2) -> int:
    timeline = Timeline()
    page = FakePage(timeline, viewport=(1280, viewport_height))
    page.remaining = remaining
    pacer = Pacer(PacingConfig(), random.Random(1), timeline.sleep)
    driver = PageDriver(page, ActionBudget(100, 100), pacer, None, timeline.clock, timeline.sleep, random.Random(seed))
    driver.scroll_screen()
    return sum(c[2] for c in page.calls if c[0] == "mouse.wheel")


def test_a_screen_scroll_is_seventy_five_to_a_hundred_percent_of_the_window_height() -> None:
    for seed in range(40):
        assert 0.75 * 800 - 1 <= screen_scroll_distance(800, 10_000, seed) <= 800


def test_a_taller_window_scrolls_further_per_gesture() -> None:
    small = [screen_scroll_distance(600, 10_000, s) for s in range(30)]
    large = [screen_scroll_distance(1400, 10_000, s) for s in range(30)]
    assert max(small) <= 600 and min(large) >= 0.75 * 1400 - 1
    assert sum(large) / 30 > 2 * sum(small) / 30


def test_a_screen_scroll_never_goes_past_the_end_of_the_page() -> None:
    assert all(screen_scroll_distance(800, 300, s) <= 300 for s in range(20))
    assert screen_scroll_distance(800, 300, 1) in range(120, 301)


def test_a_screen_scroll_is_never_a_twitch_even_at_the_bottom() -> None:
    assert screen_scroll_distance(800, 0) == 120
    assert screen_scroll_distance(800, 50) == 120


def test_a_screen_scroll_costs_one_action_and_reading_the_size_costs_none() -> None:
    driver, page, budget, _ = make_driver()
    driver.scroll_screen()
    assert budget.run_spent == 1
