"""The whole pipeline with real wiring (browser launch, capture, extractor, expansion,
runner, archive, queue, review views) around a fake Threads site. Nothing here
contacts Threads."""
import json
import random
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from threadsift.archive.repository import ArchiveRepository
from threadsift.collection.collector import open_browser_collector
from threadsift.collection.pacing import Pacer
from threadsift.collection.runner import Runner
from threadsift.config import BrowserConfig, Config
from threadsift.intake.queue import UrlQueue
from threadsift.review.traits import set_active_trait

from tests.fake_threads import FakeThreads
from tests.fakes import FakePage, FakePlaywrightFactory, Timeline


class Pipeline:
    def __init__(self, engine, tmp_path: Path, site: FakeThreads, **limits) -> None:
        self.engine, self.tmp_path, self.site = engine, tmp_path, site
        base = Config()
        self.config = replace(
            base,
            browser=BrowserConfig(session_dir=str(tmp_path / "session")),
            limits=replace(base.limits, **limits),
        )
        self.timeline = Timeline()
        self.page = FakePage(self.timeline)
        site.attach(self.page)
        self.factory = FakePlaywrightFactory(self.page)
        self.queue = UrlQueue(engine)
        self.capture_dir: Path | None = None

    def import_urls(self, *codes: str):
        path = self.tmp_path / f"urls-{len(list(self.tmp_path.glob('urls-*.txt')))}.txt"
        path.write_text("".join(self.site.url(c) + "\n" for c in codes))
        return self.queue.import_file(path)

    def run(self, **kwargs):
        @contextmanager
        def open_collector(budget, store):
            with open_browser_collector(
                self.config, budget, store, capture_dir=self.capture_dir,
                baselines=ArchiveRepository(self.engine).baseline_for,
                playwright_factory=self.factory, sleep=self.timeline.sleep,
                clock=self.timeline.clock, rng=random.Random(5),
            ) as collector:
                yield collector

        pacer = Pacer(self.config.pacing, random.Random(1), self.timeline.sleep)
        return Runner(self.engine, self.config, open_collector, pacer=pacer).run(**kwargs)

    def rows(self, sql: str) -> list:
        with self.engine.connect() as c:
            return list(c.execute(text(sql)).mappings())

    def execute(self, sql: str) -> None:
        with self.engine.begin() as c:
            c.execute(text(sql))


def small_site() -> FakeThreads:
    return (FakeThreads().add("A", text="Нарешті встановив сонячні панелі ☀️")
            .add("B", "A", text="Газ дешевший").add("C", "A", text="Окупність 6 років")
            .add("D", "C", text="А як узимку?").add("E", "A").add("E1", "E").add("E2", "E"))


@pytest.fixture
def pipeline(clean_engine, tmp_path):
    return Pipeline(clean_engine, tmp_path, small_site())


def test_import_run_review_refresh(pipeline: Pipeline) -> None:
    site = pipeline.site
    set_active_trait(pipeline.engine, "pro-renewables", "positive attitude towards renewable energy")

    # import and run
    report = pipeline.import_urls("A")
    assert (report.added, report.already_pending) == (1, 0)
    first = pipeline.run()
    assert first.end_reason == "completed" and (first.done, first.partial) == (1, 0)

    posts = {r["code"]: r for r in pipeline.rows("SELECT * FROM posts")}
    assert set(posts) == {"A", "B", "C", "D", "E", "E1", "E2"}
    assert (posts["A"]["parent_id"], posts["A"]["depth"]) == (None, 0)
    assert posts["D"]["parent_id"] == posts["C"]["id"] and posts["D"]["depth"] == 2
    assert {p["root_id"] for p in posts.values()} == {posts["A"]["id"]}
    assert posts["A"]["first_text"] == "Нарешті встановив сонячні панелі ☀️"
    assert pipeline.rows("SELECT state::text AS s, post_id FROM url_queue")[0] == {"s": "done", "post_id": posts["A"]["id"]}
    assert site.visits.count("A") == 1 and "E" in site.visits  # E has replies not in A's chains: opened on its own page

    # every item starts unreviewed; review through the views
    assert len(pipeline.rows("SELECT 1 FROM review_queue")) == 7
    pipeline.execute(f"UPDATE review_queue SET is_evidence = true WHERE post_id = '{posts['B']['id']}'")
    pipeline.execute(f"UPDATE review_queue SET is_lead = true, is_evidence = false WHERE post_id = '{posts['C']['id']}'")
    review_before = pipeline.rows("SELECT post_id, is_evidence, is_lead, reviewed_at FROM post_reviews ORDER BY post_id")
    assert len(review_before) == 2

    # the post gets a new reply and an edit; the operator lists the URL again
    site.add("N", "A", text="Нова відповідь")
    site.nodes["B"]["text"] = "Газ дешевший, але нестабільний"
    report = pipeline.import_urls("A")
    assert (report.added, report.refresh_requested) == (0, 1)
    second = pipeline.run()
    assert second.end_reason == "completed"

    posts = {r["code"]: r for r in pipeline.rows("SELECT * FROM posts")}
    assert "N" in posts and posts["N"]["parent_id"] == posts["A"]["id"] and posts["N"]["root_id"] == posts["A"]["id"]
    assert posts["B"]["first_text"] == "Газ дешевший" and posts["B"]["latest_text"] == "Газ дешевший, але нестабільний"
    assert posts["B"]["text_changed_at"] is not None and posts["A"]["text_changed_at"] is None
    assert posts["A"]["last_seen_at"] > posts["A"]["first_seen_at"]

    # nothing was deleted and no review data changed; the new reply starts unreviewed
    assert pipeline.rows("SELECT post_id, is_evidence, is_lead, reviewed_at FROM post_reviews ORDER BY post_id") == review_before
    queue = {r["post_id"] for r in pipeline.rows("SELECT post_id FROM review_queue")}
    assert posts["N"]["id"] in queue and posts["B"]["id"] not in queue and posts["C"]["id"] not in queue

    # the run log and the effort per URL
    runs = pipeline.rows("SELECT end_reason::text AS r, urls_done FROM runs ORDER BY id")
    assert [(r["r"], r["urls_done"]) for r in runs] == [("completed", 1), ("completed", 1)]
    attempts = pipeline.rows("SELECT items_new, items_updated, actions_spent FROM url_attempts ORDER BY id")
    assert attempts[0]["items_new"] == 7 and attempts[1]["items_new"] == 1 and attempts[1]["items_updated"] >= 2
    assert all(a["actions_spent"] >= 1 for a in attempts)
    yield_row = pipeline.rows("SELECT attempts, actions_spent, items, evidence FROM collection_yield")[0]
    assert yield_row["attempts"] == 2 and yield_row["items"] == 8 and yield_row["evidence"] == 1
    assert yield_row["actions_spent"] == sum(a["actions_spent"] for a in attempts)


def test_the_run_only_reads(pipeline: Pipeline) -> None:
    pipeline.import_urls("A")
    pipeline.run()
    clicks = [c for c in pipeline.page.calls if c[0] == "mouse.click"]
    assert clicks == []  # reply expansion scrolls and navigates; it never clicks
    gotos = {c[1] for c in pipeline.page.calls if c[0] == "goto"}
    assert all(u.startswith("https://www.threads.com/") for u in gotos)


def test_the_browser_is_opened_once_stays_open_through_the_pauses_and_is_closed(pipeline: Pipeline) -> None:
    pipeline.import_urls("A", "B")
    pipeline.run()
    names = [c[0] for c in pipeline.factory.calls]
    assert names.count("launch_persistent_context") == 1
    assert names[-2:] == ["context.close", "stop"]
    assert pipeline.factory.context.closed
    assert any(30 <= s <= 90 for s in pipeline.timeline.slept)  # the pause between the two URLs happened while open


def test_the_home_feed_is_browsed_first_and_nothing_from_it_is_stored(pipeline: Pipeline) -> None:
    pipeline.import_urls("A")
    pipeline.run()
    first = next(c for c in pipeline.page.calls if c[0] == "goto")
    assert first[1] == "https://www.threads.com/"
    assert [r["code"] for r in pipeline.rows("SELECT code FROM posts WHERE code = ''")] == []


def test_capture_collects_one_url_and_writes_its_payload_files(pipeline: Pipeline, tmp_path: Path) -> None:
    pipeline.capture_dir = tmp_path / "capture"
    pipeline.import_urls("A", "B")
    result = pipeline.run(url_limit=1)
    assert result.end_reason == "url_limit"
    files = sorted((tmp_path / "capture" / "A").glob("*.json"))
    assert files and files[0].name.startswith("001-")
    saved = json.loads(files[0].read_text(encoding="utf-8"))
    assert set(saved) == {"url", "kind", "data"} and saved["kind"] == "embedded"
    assert not (tmp_path / "capture" / "B").exists()
    assert pipeline.rows("SELECT state::text AS s FROM url_queue WHERE code = 'B'")[0]["s"] == "pending"


def test_a_deleted_post_fails_only_its_url(pipeline: Pipeline) -> None:
    pipeline.site.add("GONE")
    pipeline.site.deleted.add("GONE")
    pipeline.import_urls("GONE", "A")
    result = pipeline.run()
    assert (result.done, result.failed) == (1, 1)
    states = {r["code"]: (r["s"], r["reason"]) for r in pipeline.rows("SELECT code, state::text AS s, reason FROM url_queue")}
    assert states["GONE"] == ("failed", "not found") and states["A"][0] == "done"


def test_a_challenge_mid_run_stops_everything_and_keeps_what_was_saved(pipeline: Pipeline) -> None:
    pipeline.import_urls("A", "B")
    pipeline.page.redirects[pipeline.site.url("E")] = "https://www.threads.com/challenge/"  # met while expanding A
    result = pipeline.run()
    assert result.end_reason == "blocked" and result.stop_details["kind"] == "challenge"
    states = {r["code"]: (r["s"], r["reason"]) for r in pipeline.rows("SELECT code, state::text AS s, reason FROM url_queue")}
    assert states["A"] == ("pending", "blocked: challenge") and states["B"] == ("pending", None)
    assert {r["code"] for r in pipeline.rows("SELECT code FROM posts")} >= {"A", "B", "C", "D", "E"}  # saved before the stop
    assert result.next_run_allowed is not None


def test_a_post_that_hits_the_action_budget_is_partial_with_the_budget_named(clean_engine, tmp_path) -> None:
    site = FakeThreads().add("A")
    for n in range(12):
        site.add(f"B{n}", "A").add(f"B{n}x", f"B{n}").add(f"B{n}y", f"B{n}")
    pipeline = Pipeline(clean_engine, tmp_path, site, url_action_budget=5, run_action_budget=200)
    pipeline.import_urls("A")
    result = pipeline.run()
    assert (result.done, result.partial) == (0, 1)
    state = pipeline.rows("SELECT state::text AS s, reason FROM url_queue")[0]
    assert (state["s"], state["reason"]) == ("partial", "cap: action budget")
    assert pipeline.rows("SELECT actions_spent FROM url_attempts")[0]["actions_spent"] == 5


def test_capture_also_saves_how_scrolling_loaded_the_replies(clean_engine, tmp_path) -> None:
    site = FakeThreads(page_size=4, gestures_per_page=2).add("A")
    for n in range(10):
        site.add(f"R{n}", "A")
    pipeline = Pipeline(clean_engine, tmp_path, site)
    pipeline.capture_dir = tmp_path / "capture"
    pipeline.import_urls("A")
    pipeline.run(url_limit=1)
    log = json.loads((tmp_path / "capture" / "A" / "scroll-log.json").read_text(encoding="utf-8"))
    assert [e["scroll_in_batch"] for e in log] == [1, 2, 1, 2]
    assert set(log[0]) == {"post", "scroll_in_batch", "new_items", "px_to_end_before", "px_to_end_after"}


def test_without_capture_no_scroll_log_is_written(pipeline: Pipeline, tmp_path: Path) -> None:
    pipeline.import_urls("A")
    pipeline.run()
    assert not list(tmp_path.rglob("scroll-log.json"))


def test_a_refresh_skips_what_cannot_be_new_but_still_reads_the_posts_own_page(clean_engine, tmp_path) -> None:
    site = FakeThreads(page_size=3).add("A", text="Пост")
    for top in "BCD":
        site.add(top, "A", text=f"Відповідь {top}")
        for n in (1, 2):
            site.add(f"{top}{n}", top, text=f"{top}{n} початковий текст")
    pipeline = Pipeline(clean_engine, tmp_path, site)

    pipeline.import_urls("A")
    assert pipeline.run().end_reason == "completed"
    first_actions = pipeline.rows("SELECT actions_spent FROM url_attempts ORDER BY id")[0]["actions_spent"]
    assert sorted(site.visits) == ["A", "B", "C", "D"]

    # an edit on A's own page, an edit in a branch the refresh will not open, and one new reply under C
    site.nodes["B"]["text"] = "Відповідь B (змінено)"
    site.nodes["D1"]["text"] = "D1 змінений текст"
    site.add("C3", "C", text="Нова відповідь")
    site.visits.clear()
    assert pipeline.import_urls("A").refresh_requested == 1
    result = pipeline.run()
    assert (result.end_reason, result.done, result.partial) == ("completed", 1, 0)

    posts = {r["code"]: r for r in pipeline.rows("SELECT code, latest_text, first_text FROM posts")}
    assert site.visits == ["A", "C"]                                    # D was never opened
    assert posts["C3"]["latest_text"] == "Нова відповідь"               # the new reply was found
    assert posts["B"]["latest_text"] == "Відповідь B (змінено)"         # an edit on a page that was opened
    assert posts["D1"]["latest_text"] == "D1 початковий текст"          # an edit in a skipped branch is not picked up

    second_actions = pipeline.rows("SELECT actions_spent FROM url_attempts ORDER BY id")[1]["actions_spent"]
    assert second_actions < first_actions

    # nothing new at all: the post's page is the only page action
    site.visits.clear()
    pipeline.import_urls("A")
    pipeline.run()
    assert site.visits == ["A"]
    assert pipeline.rows("SELECT actions_spent FROM url_attempts ORDER BY id")[2]["actions_spent"] == 1


def test_a_url_whose_last_collection_was_partial_is_collected_in_full_again(clean_engine, tmp_path) -> None:
    site = FakeThreads().add("A")
    for top in "BCD":
        site.add(top, "A").add(f"{top}1", top).add(f"{top}2", top)
    cut_short = Pipeline(clean_engine, tmp_path, site, url_action_budget=2, run_action_budget=500)
    cut_short.import_urls("A")
    assert cut_short.run().partial == 1

    full = Pipeline(clean_engine, tmp_path, site)
    full.import_urls("A")
    site.visits.clear()
    assert full.run().done == 1
    assert sorted(site.visits) == ["A", "B", "C", "D"]  # the count had not changed, yet everything was opened
