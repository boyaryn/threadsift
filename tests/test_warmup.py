import random

import pytest

from threadsift.collection.browser import HOME_URL, PageDriver
from threadsift.collection.budget import ActionBudget, BudgetExhausted
from threadsift.collection.capture import ResponseCapture
from threadsift.collection.guard import BlockedError
from threadsift.collection.pacing import Pacer
from threadsift.config import PacingConfig

from tests.fakes import FakeNetResponse, FakePage, Timeline

FEED = "https://www.threads.com/api/graphql"
FEED_BODY = {"data": {"feedData": {"edges": [{"node": {"post": {"pk": "feed1", "caption": {"text": "from the feed"}}}}]}}}


def setup(run_budget=1200, seed=1):
    timeline = Timeline()
    page = FakePage(timeline)
    pacer = Pacer(PacingConfig(), random.Random(seed), timeline.sleep)
    budget = ActionBudget(run_budget, 150)
    capture = ResponseCapture(page)
    driver = PageDriver(page, budget, pacer, None, timeline.clock, timeline.sleep, random.Random(seed),
                        on_check=capture.raise_pending)
    return driver, page, budget, capture, timeline


def test_warmup_opens_the_home_feed_first_and_scrolls_for_one_to_three_minutes() -> None:
    for seed in range(8):
        driver, page, budget, _, timeline = setup(seed=seed)
        driver.warm_up_home_feed()
        assert page.calls[0] == ("goto", HOME_URL)
        assert any(c[0] == "mouse.wheel" for c in page.calls)
        assert 60 <= timeline.now <= 180 + 30  # the last action may overshoot by one pause
        scrolls = len([c for c in page.calls if c[0] == "mouse.wheel"])
        assert scrolls >= 1


def test_warmup_actions_are_counted_against_the_run_budget_only() -> None:
    driver, page, budget, _, _ = setup()
    driver.warm_up_home_feed()
    assert budget.run_spent > 1
    assert budget.url_spent == 0
    wheel_ticks = len([c for c in page.calls if c[0] == "mouse.wheel"])
    gestures = budget.run_spent - 1  # one navigation to the feed, the rest are scroll gestures
    assert 1 <= gestures <= wheel_ticks  # a gesture is at least one wheel tick


def test_warmup_is_cut_short_by_the_run_budget() -> None:
    driver, _, budget, _, _ = setup(run_budget=3)
    with pytest.raises(BudgetExhausted):
        driver.warm_up_home_feed()
    assert budget.run_spent == 3


def test_nothing_from_the_feed_is_captured() -> None:
    driver, page, _, capture, _ = setup()
    page.on_goto = lambda url: page.emit("response", FakeNetResponse(FEED, 200, FEED_BODY))
    page.on_scroll = lambda: page.emit("response", FakeNetResponse(FEED, 200, FEED_BODY))
    driver.warm_up_home_feed()
    assert capture.end_url() == []

    capture.begin_url()
    assert capture.end_url() == []  # starting a URL does not resurrect feed responses


def test_responses_after_the_warmup_are_captured_for_the_url() -> None:
    driver, page, _, capture, _ = setup()
    driver.warm_up_home_feed()
    capture.begin_url()
    page.emit("response", FakeNetResponse(FEED, 200, {"data": {"post": 1}}))
    page.emit("response", FakeNetResponse("https://www.threads.com/static/app.js", 200, None))
    payloads = capture.end_url()
    assert [p.data for p in payloads] == [{"data": {"post": 1}}]


def test_a_blocking_response_during_the_feed_warmup_still_stops_the_run() -> None:
    driver, page, _, capture, _ = setup()
    page.on_scroll = lambda: page.emit("response", FakeNetResponse(FEED, 429, None))
    with pytest.raises(BlockedError, match="rate limited"):
        driver.warm_up_home_feed()


def test_capture_does_not_leak_between_urls() -> None:
    _, page, _, capture, _ = setup()
    capture.begin_url()
    page.emit("response", FakeNetResponse(FEED, 200, {"a": 1}))
    capture.end_url()
    capture.begin_url()
    page.emit("response", FakeNetResponse(FEED, 200, {"b": 2}))
    assert [p.data for p in capture.end_url()] == [{"b": 2}]


def test_responses_between_urls_are_dropped() -> None:
    _, page, _, capture, _ = setup()
    capture.begin_url()
    capture.end_url()
    page.emit("response", FakeNetResponse(FEED, 200, {"late": True}))
    capture.begin_url()
    assert capture.end_url() == []


def test_unreadable_responses_are_not_stored() -> None:
    _, page, _, capture, _ = setup()
    capture.begin_url()
    page.emit("response", FakeNetResponse(FEED, 200, broken_json=True))
    page.emit("response", FakeNetResponse(FEED, 500, {"x": 1}))
    assert capture.end_url() == []
