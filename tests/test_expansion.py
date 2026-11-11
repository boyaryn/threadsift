import random

import pytest
from sqlalchemy import text

from threadsift.collection.browser import PageDriver
from threadsift.collection.budget import ActionBudget
from threadsift.collection.capture import ResponseCapture
from threadsift.collection.expansion import Expander
from threadsift.collection.guard import BlockedError
from threadsift.collection.monitor import EarlyWarning, Monitor
from threadsift.collection.pacing import Pacer
from threadsift.config import EarlyWarningConfig, LimitsConfig, PacingConfig
from threadsift.domain import StoreOutcome
from threadsift.intake.queue import UrlQueue

from tests.fake_threads import GRAPHQL, FakeThreads
from tests.fakes import FakeNetResponse, FakePage, Timeline


class Run:
    """Everything wired together the way a run wires it, around a fake site."""

    def __init__(self, site: FakeThreads, url_budget: int = 150, with_monitor: bool = True, **limits) -> None:
        self.site = site
        self.timeline = Timeline()
        self.page = FakePage(self.timeline)
        site.attach(self.page)
        self.budget = ActionBudget(run_budget=10_000, url_budget=url_budget)
        self.monitor = Monitor(EarlyWarningConfig()) if with_monitor else None
        capture = ResponseCapture(self.page, self.monitor)
        pacer = Pacer(PacingConfig(), random.Random(1), self.timeline.sleep)
        driver = PageDriver(self.page, self.budget, pacer, self.monitor, self.timeline.clock,
                            self.timeline.sleep, random.Random(2), on_check=capture.raise_pending)
        self.stored: list = []
        self.expander = Expander(driver, capture, self._store, LimitsConfig(**limits), self.monitor, random.Random(3))
        self.budget.start_url()

    def _store(self, item) -> StoreOutcome:
        self.stored.append(item)
        return StoreOutcome.NEW

    def collect(self, code: str):
        return self.expander.collect(self.site.url(code), code)

    @property
    def codes(self) -> set[str]:
        return {i.code for i in self.stored}


def simple_tree() -> FakeThreads:
    return FakeThreads().add("A").add("B", "A").add("C", "A").add("D", "C")


# --- the basics ---------------------------------------------------------------

def test_a_tree_that_fits_on_one_page_is_collected_with_one_navigation() -> None:
    run = Run(simple_tree())
    result = run.collect("A")
    assert result.state == "done" and result.reason is None
    assert run.codes == {"A", "B", "C", "D"}
    assert run.site.visits == ["A"]
    assert run.budget.url_spent == 1 and result.pages_opened == 1
    assert result.post_id == run.site.pk("A")
    assert result.summary.new == 4


def test_items_keep_their_parents_and_depths() -> None:
    run = Run(simple_tree())
    run.collect("A")
    items = {i.code: i for i in run.stored}
    assert (items["A"].parent_id, items["A"].depth) == (None, 0)
    assert items["B"].parent_id == items["A"].id and items["B"].depth == 1
    assert items["D"].parent_id == items["C"].id and items["D"].depth == 2
    assert {i.root_id for i in run.stored} == {items["A"].id}


def test_a_reply_with_unseen_replies_is_opened_on_its_own_page_breadth_first() -> None:
    site = (FakeThreads().add("A").add("B", "A").add("C", "A")
            .add("B1", "B").add("B2", "B").add("C1", "C").add("C2", "C")
            .add("B1a", "B1").add("B1b", "B1"))
    run = Run(site)
    result = run.collect("A")
    assert site.visits == ["A", "B", "C", "B1"]  # shallowest first, then in the order seen
    assert result.state == "done"
    assert run.codes == {"A", "B", "C", "B1", "B2", "C1", "C2", "B1a", "B1b"}
    assert run.budget.url_spent == 4


def test_replies_load_in_pages_when_scrolling() -> None:
    site = FakeThreads(page_size=20).add("A")
    for n in range(40):
        site.add(f"R{n}", "A")
    run = Run(site)
    result = run.collect("A")
    assert result.state == "done"
    assert len(run.codes) == 41
    assert run.budget.url_spent == 2  # one navigation, one scroll gesture
    assert site.scrolled["A"] == 1


def test_a_url_pointing_to_a_reply_collects_its_parents_but_does_not_open_them() -> None:
    site = (FakeThreads().add("A").add("B", "A").add("X", "A")  # X is B's sibling: not our business
            .add("C", "B").add("C1", "C").add("C2", "C"))
    run = Run(site)
    result = run.collect("C")
    assert site.visits == ["C"]
    assert result.state == "done"
    assert run.codes == {"A", "B", "C", "C1", "C2"}
    items = {i.code: i for i in run.stored}
    assert items["C"].depth == 2 and items["C"].root_id == items["A"].id


def test_replies_the_page_claims_but_cannot_show_are_tried_once() -> None:
    site = FakeThreads().add("A").add("H", "A", hidden_replies=2)
    run = Run(site)
    result = run.collect("A")
    assert result.state == "done"
    assert site.visits == ["A", "H"]


# --- limits -------------------------------------------------------------------

def test_action_budget_stops_expansion_and_keeps_what_was_collected() -> None:
    site = FakeThreads().add("A")
    for n in range(6):
        site.add(f"B{n}", "A")
        site.add(f"B{n}x", f"B{n}")
        site.add(f"B{n}y", f"B{n}")
    run = Run(site, url_budget=4)
    result = run.collect("A")
    assert result.state == "partial" and result.reason == "cap: action budget"
    assert run.budget.url_spent == 4  # never over the budget
    assert site.visits == ["A", "B0", "B1", "B2"]  # shallowest and earliest first
    assert {"A", "B0", "B0x", "B0y", "B2y"} <= run.codes
    assert "B3x" not in run.codes


def test_depth_cap_keeps_five_levels_and_marks_the_url_partial() -> None:
    site = FakeThreads().add("A")
    for n in range(1, 8):
        site.add(f"D{n}", "A" if n == 1 else f"D{n - 1}")
    run = Run(site, max_depth=5)
    result = run.collect("A")
    assert result.state == "partial" and result.reason == "cap: depth 5"
    assert run.codes == {"A", "D1", "D2", "D3", "D4", "D5"}


def test_replies_below_the_depth_cap_with_no_replies_of_their_own_are_not_a_cut() -> None:
    site = FakeThreads().add("A")
    for n in range(1, 6):
        site.add(f"D{n}", "A" if n == 1 else f"D{n - 1}")
    result = Run(site, max_depth=5).collect("A")
    assert result.state == "done"


def test_depth_cap_is_configurable() -> None:
    site = FakeThreads().add("A").add("B", "A").add("C", "B").add("D", "C")
    run = Run(site, max_depth=2)
    result = run.collect("A")
    assert run.codes == {"A", "B", "C"}
    assert result.reason == "cap: depth 2"


def test_reply_cap_stops_expansion_and_names_the_limit() -> None:
    site = FakeThreads(page_size=10).add("A")
    for n in range(30):
        site.add(f"R{n}", "A")
    run = Run(site, max_replies=15)
    result = run.collect("A")
    assert result.state == "partial" and result.reason == "cap: 15 replies"
    assert 15 <= len(run.codes) - 1 < 30


def test_reply_cap_prevents_opening_more_pages() -> None:
    site = FakeThreads().add("A")
    for n in range(10):
        site.add(f"B{n}", "A")
        site.add(f"B{n}x", f"B{n}")
        site.add(f"B{n}y", f"B{n}")
    run = Run(site, max_replies=12)
    result = run.collect("A")
    assert result.reason == "cap: 12 replies"
    assert len(site.visits) < 11


def test_a_tree_within_all_limits_is_done() -> None:
    site = FakeThreads(page_size=20).add("A")
    for n in range(40):
        site.add(f"R{n}", "A")
    for n in range(0, 40, 4):
        site.add(f"R{n}c", f"R{n}")
        site.add(f"R{n}c2", f"R{n}c")
    run = Run(site, max_depth=5)
    assert run.collect("A").state == "done"
    assert len(run.codes) == 1 + 40 + 20


# --- stops propagate, data is kept --------------------------------------------

def test_three_empty_reveals_in_a_row_raise_an_early_warning_and_keep_the_data() -> None:
    site = FakeThreads().add("A")
    for n in range(3):
        site.add(f"H{n}", "A", hidden_replies=1)
    run = Run(site)
    with pytest.raises(EarlyWarning, match="empty_reveals"):
        run.collect("A")
    assert run.codes == {"A", "H0", "H1", "H2"}  # stored before the stop


def test_normal_gaps_do_not_raise_a_warning() -> None:
    site = FakeThreads().add("A")
    site.add("H0", "A", hidden_replies=1)
    site.add("H1", "A", hidden_replies=1)
    site.add("X", "A")
    site.add("X1", "X").add("X2", "X")
    run = Run(site)
    result = run.collect("A")
    assert result.state == "done"
    assert {"X1", "X2"} <= run.codes


def test_a_blocking_response_stops_everything_without_being_caught() -> None:
    site = FakeThreads().add("A").add("B", "A").add("B1", "B").add("B2", "B")
    run = Run(site)
    serve = run.page.on_goto

    def block_on_b(url: str) -> None:
        serve(url)
        if site.code_of(url) == "B":
            run.page.emit("response", FakeNetResponse(GRAPHQL, 429, None))

    run.page.on_goto = block_on_b
    with pytest.raises(BlockedError, match="rate limited"):
        run.collect("A")
    assert run.codes == {"A", "B"}


def test_a_redirect_to_a_challenge_stops_everything() -> None:
    site = FakeThreads().add("A").add("B", "A").add("B1", "B").add("B2", "B")
    run = Run(site)
    run.page.redirects[site.url("B")] = "https://www.threads.com/challenge/"
    with pytest.raises(BlockedError, match="challenge"):
        run.collect("A")


def test_scrolling_that_loads_nothing_ends_in_an_early_warning() -> None:
    site = FakeThreads(page_size=5).add("A")
    for n in range(20):
        site.add(f"R{n}", "A")
    site.scrolling_loads_nothing = True
    run = Run(site)
    with pytest.raises(EarlyWarning):
        run.collect("A")
    assert len(run.codes) == 6


def test_without_a_monitor_repeated_empty_scrolls_still_end() -> None:
    site = FakeThreads(page_size=5).add("A")
    for n in range(20):
        site.add(f"R{n}", "A")
    site.scrolling_loads_nothing = True
    run = Run(site, with_monitor=False)
    result = run.collect("A")
    assert result.state == "partial" and result.reason == "cap: replies stopped loading"
    assert run.budget.url_spent == 1 + 3


# --- failures of the URL itself --------------------------------------------------

def test_a_deleted_post_fails_with_not_found_and_stores_nothing() -> None:
    site = simple_tree()
    site.deleted.add("A")
    run = Run(site)
    result = run.collect("A")
    assert (result.state, result.reason) == ("failed", "not found")
    assert run.stored == []


def test_a_reply_deleted_since_it_was_seen_is_skipped() -> None:
    site = FakeThreads().add("A").add("B", "A").add("C", "A").add("B1", "B").add("B2", "B").add("C1", "C").add("C2", "C")
    run = Run(site)
    serve = run.page.on_goto

    def delete_b_after_a(url: str) -> None:
        serve(url)
        if site.code_of(url) == "A":
            site.deleted.add("B")

    run.page.on_goto = delete_b_after_a
    result = run.collect("A")
    assert result.state == "done"
    assert {"C1", "C2"} <= run.codes and "B1" not in run.codes


def parse_failing_run() -> Run:
    run = Run(simple_tree())

    def garbage(url: str) -> None:
        html = '<html><script type="application/json">{"data": {"something": [1, 2, 3]}}</script></html>'
        run.page.emit("response", FakeNetResponse(url, 200, html=html))

    run.page.on_goto = garbage
    return run


def test_data_with_no_posts_fails_the_url_with_parse_in_the_reason() -> None:
    run = parse_failing_run()
    result = run.collect("A")
    assert result.state == "failed"
    assert "parse" in result.reason
    assert run.stored == []


def test_a_parse_failure_is_recorded_as_failed_not_as_an_empty_done(clean_engine, tmp_path) -> None:
    run = parse_failing_run()
    queue = UrlQueue(clean_engine)
    path = tmp_path / "f.txt"
    path.write_text("https://www.threads.com/@user_000/post/A\n")
    queue.import_file(path)
    (queued,) = queue.pending(limit=5)

    result = run.collect("A")
    queue.record_result(queued.id, result.state, reason=result.reason)

    with clean_engine.connect() as connection:
        row = connection.execute(text("SELECT state, reason FROM url_queue")).one()
    assert row.state == "failed" and "parse" in row.reason
    assert queue.pending(limit=5) == []  # not retried by itself either


def test_a_parse_failure_on_a_sub_page_keeps_the_data_and_marks_the_url_partial() -> None:
    site = FakeThreads().add("A").add("B", "A").add("B1", "B").add("B2", "B")
    run = Run(site)
    serve = run.page.on_goto

    def garbage_on_b(url: str) -> None:
        if site.code_of(url) == "B":
            html = '<html><script type="application/json">{"data": {}}</script></html>'
            run.page.emit("response", FakeNetResponse(url, 200, html=html))
        else:
            serve(url)

    run.page.on_goto = garbage_on_b
    result = run.collect("A")
    assert result.state == "partial" and result.reason.startswith("parse")
    assert run.codes == {"A", "B"}


# --- storing ---------------------------------------------------------------------

def test_items_are_stored_as_they_arrive_and_only_once_unless_they_change() -> None:
    site = FakeThreads(page_size=2).add("A")
    for n in range(5):
        site.add(f"R{n}", "A")
    run = Run(site)
    run.collect("A")
    codes = [i.code for i in run.stored]
    assert len(codes) == len(set(codes)) == 6


def test_into_the_real_archive(clean_engine) -> None:
    from threadsift.archive.repository import ArchiveRepository

    site = (FakeThreads().add("A").add("B", "A").add("C", "A").add("B1", "B").add("B2", "B"))
    run = Run(site)
    run.expander._store = ArchiveRepository(clean_engine).store_item
    result = run.collect("A")
    assert result.state == "done" and result.summary.new == 5
    with clean_engine.connect() as connection:
        rows = connection.execute(text("SELECT code, parent_id, root_id, depth FROM posts ORDER BY depth, code")).all()
    assert [(r.code, r.depth) for r in rows] == [("A", 0), ("B", 1), ("C", 1), ("B1", 2), ("B2", 2)]
    assert {r.root_id for r in rows} == {site.pk("A")}


def test_deep_thread_that_reaches_the_default_150_action_budget_is_partial() -> None:
    site = FakeThreads().add("A")
    for n in range(200):
        site.add(f"B{n}", "A")
        site.add(f"B{n}x", f"B{n}")
        site.add(f"B{n}y", f"B{n}")
    run = Run(site, url_budget=LimitsConfig().url_action_budget, max_replies=100_000)
    result = run.collect("A")
    assert result.state == "partial" and result.reason == "cap: action budget"
    assert run.budget.url_spent == 150
    assert "B0x" in run.codes  # what was collected is kept


# --- paging as Threads really does it: the next batch loads once the end is in view ---

def many_replies(count: int, page_size: int, gestures: int) -> FakeThreads:
    site = FakeThreads(page_size=page_size, gestures_per_page=gestures).add("A")
    for n in range(count):
        site.add(f"R{n}", "A")
    return site


def test_several_scroll_gestures_per_batch_are_needed_and_not_mistaken_for_empty_reveals() -> None:
    site = many_replies(20, page_size=5, gestures=3)
    run = Run(site)  # a monitor is watching: 2 gestures in a row that load nothing must not worry it
    result = run.collect("A")
    assert result.state == "done"
    assert len(run.codes) == 21
    assert run.budget.url_spent == 1 + 3 * 3  # the page, then 3 batches of 3 gestures


def test_scrolling_that_loads_nothing_counts_only_once_the_page_is_at_its_bottom() -> None:
    site = many_replies(20, page_size=5, gestures=5)
    site.scrolling_loads_nothing = True
    run = Run(site, with_monitor=False)
    result = run.collect("A")
    assert result.reason == "cap: replies stopped loading"
    assert run.budget.url_spent == 1 + 5 + 2  # 4 gestures on the way down, 1 + 2 more at the bottom


def test_the_early_warning_still_fires_when_the_bottom_is_reached_and_nothing_loads() -> None:
    site = many_replies(20, page_size=5, gestures=5)
    site.scrolling_loads_nothing = True
    run = Run(site)
    with pytest.raises(EarlyWarning, match="empty_reveals"):
        run.collect("A")
    assert run.budget.url_spent == 1 + 5 + 2


def test_paging_stops_by_the_action_budget_and_keeps_what_loaded() -> None:
    site = many_replies(200, page_size=4, gestures=3)
    run = Run(site, url_budget=20, max_replies=10_000)
    result = run.collect("A")
    assert result.state == "partial" and result.reason == "cap: action budget"
    assert run.budget.url_spent == 20
    assert 1 + 4 * 6 <= len(run.codes) < 201


# --- the scroll log the live check relies on -----------------------------------------

def test_the_scroll_log_shows_scrolls_per_batch_and_the_distance_to_the_end() -> None:
    site = many_replies(12, page_size=4, gestures=3)
    run = Run(site)
    run.page.remaining = 2500
    run.collect("A")
    log = run.expander.scroll_log
    assert [e["scroll_in_batch"] for e in log] == [1, 2, 3, 1, 2, 3]  # three scrolls per batch of replies
    assert [e["new_items"] > 0 for e in log] == [False, False, True, False, False, True]
    assert all(e["post"] == "A" and e["px_to_end_before"] == 2500 for e in log)


def test_the_scroll_log_starts_afresh_for_each_url() -> None:
    site = many_replies(8, page_size=4, gestures=1)
    run = Run(site)
    run.collect("A")
    assert len(run.expander.scroll_log) == 1
    run.budget.start_url()
    run.collect("A")
    assert len(run.expander.scroll_log) == 1
