"""What a refresh may compare a page against (tasks 14.1): read from the archive and the
run log, before the new collection stores anything."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from threadsift.archive.repository import ArchiveRepository
from threadsift.archive.runs import RunLog
from threadsift.domain import CollectedAuthor, CollectedItem, ThreadBaseline
from threadsift.intake.queue import QueuedUrl, UrlQueue

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
AUTHOR = CollectedAuthor(id="u1", username="sun_fan")


def item(code: str, parent: str | None = None, reply_count: int = 0, depth: int | None = None) -> CollectedItem:
    return CollectedItem(
        id=f"id-{code}", code=code, url=f"https://www.threads.com/@sun_fan/post/{code}", author=AUTHOR,
        text=f"text {code}", parent_id=f"id-{parent}" if parent else None,
        root_id="id-A" if parent else f"id-{code}", depth=(1 if parent else 0) if depth is None else depth,
        reply_count=reply_count,
    )


class World:
    def __init__(self, engine, tmp_path) -> None:
        self.engine, self.tmp_path = engine, tmp_path
        self.now = T0
        self.repo = ArchiveRepository(engine, clock=lambda: self.now)
        self.log = RunLog(engine)
        self.queue = UrlQueue(engine)

    def url(self, code: str = "A") -> QueuedUrl:
        path = self.tmp_path / f"u-{code}.txt"
        path.write_text(f"https://www.threads.com/@sun_fan/post/{code}\n")
        self.queue.import_file(path)
        return next(q for q in self.queue.pending(50) if q.code == code)

    def attempt(self, queued: QueuedUrl, outcome: str, at: datetime | None = None, run_id: int | None = None) -> int:
        run_id = run_id or self.log.start_run(self.now, False)
        attempt = self.log.start_attempt(run_id, queued.id, self.now)
        self.log.finish_attempt(attempt, at or self.now, 10, 0, 0, outcome, None)
        return attempt

    def collect_thread(self) -> None:
        for it in (item("A", reply_count=3), item("B", "A", reply_count=1), item("C", "A"), item("D", "B")):
            self.repo.store_item(it)


@pytest.fixture
def world(clean_engine, tmp_path) -> World:
    return World(clean_engine, tmp_path)


def test_a_url_whose_last_attempt_was_done_has_a_baseline_with_the_stored_counts(world: World) -> None:
    queued = world.url()
    world.collect_thread()
    world.now += timedelta(minutes=5)
    world.attempt(queued, "done")
    baseline = world.repo.baseline_for(queued)
    assert baseline == ThreadBaseline("id-A", 3, {"id-A": 3, "id-B": 1, "id-C": 0, "id-D": 0})


def test_a_url_that_was_never_collected_has_no_baseline(world: World) -> None:
    queued = world.url()
    assert world.repo.baseline_for(queued) is None
    world.collect_thread()  # the post is stored (as an ancestor, say) but this URL has no finished attempt
    assert world.repo.baseline_for(queued) is None


@pytest.mark.parametrize("outcome", ["partial", "failed", "pending"])
def test_a_url_whose_last_attempt_did_not_end_done_has_no_baseline(world: World, outcome: str) -> None:
    queued = world.url()
    world.collect_thread()
    world.now += timedelta(minutes=5)
    world.attempt(queued, outcome)  # pending is how an interrupted attempt is recorded
    assert world.repo.baseline_for(queued) is None


def test_only_the_latest_finished_attempt_counts(world: World) -> None:
    queued = world.url()
    world.collect_thread()
    world.now += timedelta(minutes=5)
    world.attempt(queued, "done")
    world.now += timedelta(minutes=5)
    world.attempt(queued, "partial")
    assert world.repo.baseline_for(queued) is None
    world.now += timedelta(minutes=5)
    world.attempt(queued, "done")
    assert world.repo.baseline_for(queued) is not None


def test_an_attempt_still_in_progress_is_not_the_previous_one(world: World) -> None:
    queued = world.url()
    world.collect_thread()
    world.now += timedelta(minutes=5)
    world.attempt(queued, "done")
    world.log.start_attempt(world.log.start_run(world.now, False), queued.id, world.now)  # this run's own attempt
    assert world.repo.baseline_for(queued) is not None


def test_a_post_touched_by_another_collection_since_has_no_baseline(world: World) -> None:
    queued = world.url()
    world.collect_thread()
    world.now += timedelta(minutes=5)
    world.attempt(queued, "done")
    world.now += timedelta(hours=1)
    # a reply URL's page carried A as an ancestor, with a newer count, and no new replies
    world.repo.store_item(replace(item("A", reply_count=9)))
    assert world.repo.baseline_for(queued) is None


def test_a_url_pointing_to_a_reply_has_no_baseline(world: World) -> None:
    queued = world.url("B")
    world.collect_thread()
    world.now += timedelta(minutes=5)
    world.attempt(queued, "done")
    assert world.repo.baseline_for(queued) is None


def test_a_post_without_a_stored_reply_count_has_no_baseline(world: World) -> None:
    queued = world.url()
    world.repo.store_item(replace(item("A"), reply_count=None))
    world.now += timedelta(minutes=5)
    world.attempt(queued, "done")
    assert world.repo.baseline_for(queued) is None
