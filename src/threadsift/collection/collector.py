import json
import random
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from threadsift.collection.browser import PageDriver, open_browser
from threadsift.collection.budget import ActionBudget
from threadsift.collection.capture import ResponseCapture
from threadsift.collection.expansion import Expander, ExpansionResult
from threadsift.collection.monitor import Monitor
from threadsift.collection.pacing import Pacer
from threadsift.config import Config
from threadsift.domain import CollectedItem, StoreOutcome, StoreSummary, ThreadBaseline
from threadsift.intake.queue import QueuedUrl


class BrowserCollector:
    """What the run loop drives: the real browser, wired to the expansion strategy."""

    def __init__(
        self, driver: PageDriver, expander: Expander, capture_dir: Path | None = None,
        baselines: Callable[[QueuedUrl], ThreadBaseline | None] | None = None,
    ) -> None:
        self._driver = driver
        self._expander = expander
        self._capture_dir = capture_dir
        self._baselines = baselines

    def warm_up(self) -> None:
        self._driver.warm_up_home_feed()

    def collect(self, queued: QueuedUrl) -> ExpansionResult:
        try:
            baseline = self._baselines(queued) if self._baselines else None  # before anything is stored
            return self._expander.collect(queued.canonical_url, queued.code, baseline)
        finally:
            self._save_scroll_log(queued.code)

    def _save_scroll_log(self, code: str) -> None:
        """With --capture, keep how scrolling loaded the replies, for tuning."""
        if self._capture_dir is None or not self._expander.scroll_log:
            return
        folder = self._capture_dir / code
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "scroll-log.json").write_text(
            json.dumps(self._expander.scroll_log, indent=1), encoding="utf-8"
        )

    @property
    def last_summary(self) -> StoreSummary:
        return self._expander.summary


@contextmanager
def open_browser_collector(
    config: Config,
    budget: ActionBudget,
    store: Callable[[CollectedItem], StoreOutcome],
    *,
    capture_dir: Path | None = None,
    baselines: Callable[[QueuedUrl], ThreadBaseline | None] | None = None,
    playwright_factory: Callable[[str], Any] | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    rng: random.Random | None = None,
) -> Iterator[BrowserCollector]:
    """Open the persistent browser and everything that drives it. The browser stays
    open for the whole run, including the long breaks."""
    options = {} if playwright_factory is None else {"playwright_factory": playwright_factory}
    with open_browser(config.browser, **options) as context:
        page = context.pages[0] if context.pages else context.new_page()
        monitor = Monitor(config.early_warning)
        capture = ResponseCapture(page, monitor, save_dir=capture_dir)
        pacer = Pacer(config.pacing, rng, sleep)
        driver = PageDriver(page, budget, pacer, monitor, clock, sleep, rng, on_check=capture.raise_pending)
        expander = Expander(driver, capture, store, config.limits, monitor, rng)
        yield BrowserCollector(driver, expander, capture_dir, baselines)
