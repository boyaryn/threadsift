"""Refreshing a top-level post that was collected in full before (task 14.2): the post's
count covers its whole thread, so an unchanged count means nothing else is opened, and a
grown count means looking only until that many new replies have been found."""
from dataclasses import replace

from threadsift.collection.expansion import ExpansionResult
from threadsift.domain import ThreadBaseline

from tests.fake_threads import FakeThreads
from tests.test_expansion import Run


def baseline_after_full_collection(site: FakeThreads, code: str = "A") -> ThreadBaseline:
    first = Run(site)
    first.collect(code)
    latest = {item.id: item for item in first.stored}
    post = latest[site.pk(code)]
    return ThreadBaseline(post.id, post.reply_count, {i: item.reply_count for i, item in latest.items()})


def refresh(site: FakeThreads, baseline: ThreadBaseline | None, code: str = "A", **limits) -> tuple[Run, ExpansionResult]:
    site.visits.clear()
    site.scrolled.clear()
    run = Run(site, **limits)
    return run, run.expander.collect(site.url(code), code, baseline)


def branching_site() -> FakeThreads:
    """A with three replies B, C, D, each with two replies of its own; two replies per page."""
    site = FakeThreads(page_size=2).add("A")
    for top in "BCD":
        site.add(top, "A")
        for n in (1, 2):
            site.add(f"{top}{n}", top)
    return site


# --- nothing new --------------------------------------------------------------

def test_an_unchanged_count_costs_one_page_action_and_no_scrolling() -> None:
    site = FakeThreads(page_size=2).add("A")
    for n in range(6):
        site.add(f"R{n}", "A")  # three pages of replies
    baseline = baseline_after_full_collection(site)
    run, result = refresh(site, baseline)
    assert (result.state, result.reason) == ("done", None)
    assert site.visits == ["A"] and site.scrolled.get("A", 0) == 0
    assert run.budget.url_spent == 1 and result.pages_opened == 1


def test_the_counts_on_the_one_page_are_still_updated() -> None:
    site = FakeThreads(page_size=2).add("A").add("R0", "A").add("R1", "A")
    baseline = baseline_after_full_collection(site)
    site.nodes["R0"]["text"] = "Змінений текст"
    run, result = refresh(site, baseline)
    assert run.budget.url_spent == 1
    assert [i.text for i in run.stored if i.code == "R0"] == ["Змінений текст"]


def test_a_count_that_went_down_is_not_growth() -> None:
    site = branching_site()
    baseline = baseline_after_full_collection(site)
    site.deleted.add("B2")
    run, result = refresh(site, baseline)
    assert result.state == "done" and site.visits == ["A"] and run.budget.url_spent == 1


# --- something new --------------------------------------------------------------

def test_a_new_reply_in_one_branch_opens_only_that_branch() -> None:
    site = branching_site()
    baseline = baseline_after_full_collection(site)
    site.add("C3", "C")  # C's own count goes 2 -> 3
    run, result = refresh(site, baseline)
    assert result.state == "done" and result.reason is None
    assert site.visits == ["A", "C"]
    assert "C3" in run.codes
    assert run.budget.url_spent == 3  # A's page, C's page, and one scroll there: C now has a second page of replies


def test_a_new_direct_reply_is_found_on_the_posts_own_page() -> None:
    site = FakeThreads().add("A").add("B", "A").add("C", "A")
    baseline = baseline_after_full_collection(site)
    site.add("N", "A")
    run, result = refresh(site, baseline)
    assert result.state == "done" and site.visits == ["A"] and "N" in run.codes


def test_scrolling_stops_once_the_new_replies_are_found() -> None:
    site = FakeThreads(page_size=2).add("A")
    for n in range(6):
        site.add(f"R{n}", "A")
    baseline = baseline_after_full_collection(site)
    site.add("R0x", "R0").add("R0y", "R0")  # R0 is on the first page, and its count changed
    run, result = refresh(site, baseline)
    assert result.state == "done" and site.visits == ["A", "R0"]
    assert site.scrolled.get("A", 0) == 0  # the other two pages of replies were not loaded


def test_a_new_reply_inside_a_chain_on_the_posts_own_page_needs_no_further_page() -> None:
    site = FakeThreads(page_size=2).add("A")
    for n in range(4):
        site.add(f"R{n}", "A")
    baseline = baseline_after_full_collection(site)
    site.add("R0x", "R0")  # R0 has exactly one reply, so it continues R0's chain on A's page
    run, result = refresh(site, baseline)
    assert result.state == "done" and "R0x" in run.codes
    assert site.visits == ["A"] and run.budget.url_spent == 1


def test_scrolling_continues_while_the_new_replies_have_not_been_found() -> None:
    site = FakeThreads(page_size=2).add("A")
    for n in range(6):
        site.add(f"R{n}", "A")
    baseline = baseline_after_full_collection(site)
    site.add("R6", "A")  # a new direct reply, on a page that is not loaded yet
    run, result = refresh(site, baseline)
    assert result.state == "done" and "R6" in run.codes
    assert site.scrolled["A"] == 3  # loaded page by page until it appeared


def test_an_unchanged_branch_is_only_opened_after_the_changed_ones_are_exhausted() -> None:
    site = branching_site()
    baseline = baseline_after_full_collection(site)
    site.add("C2a", "C2")  # two levels down: no count that is visible on A's page changed
    run, result = refresh(site, baseline)
    assert result.state == "done" and "C2a" in run.codes
    assert site.visits == ["A", "B", "C"]  # the others in breadth-first order, stopping once found


def test_growth_that_cannot_be_found_ends_done_after_looking_everywhere_once() -> None:
    site = branching_site()
    baseline = baseline_after_full_collection(site)
    site.nodes["A"]["hidden"] += 2  # the count grows, but no new reply is visible (hidden or restricted)
    run, result = refresh(site, baseline)
    assert (result.state, result.reason) == ("done", None)
    assert sorted(site.visits) == ["A", "B", "C", "D"]


def test_running_out_of_budget_before_finding_them_is_partial() -> None:
    site = branching_site()
    baseline = baseline_after_full_collection(site)
    site.add("D2a", "D2")  # found only on D's page, the third one opened
    run, result = refresh(site, baseline, url_budget=3)
    assert (result.state, result.reason) == ("partial", "cap: action budget")
    assert "D2a" not in run.codes


# --- when the baseline does not apply ----------------------------------------------

def test_without_a_baseline_the_whole_tree_is_collected() -> None:
    site = branching_site()
    baseline_after_full_collection(site)
    run, result = refresh(site, None)
    assert result.state == "done" and sorted(site.visits) == ["A", "B", "C", "D"]


def test_a_baseline_for_another_post_is_ignored() -> None:
    site = branching_site()
    baseline = replace(baseline_after_full_collection(site), post_id="not-this-post")
    run, result = refresh(site, baseline)
    assert result.state == "done" and sorted(site.visits) == ["A", "B", "C", "D"]
