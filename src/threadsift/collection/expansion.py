"""Collecting a post and its reply tree, within the action budget and the tree caps.

Threads shows a post's replies as chains and pages them. A page therefore carries
only part of the tree, and a reply whose own replies are not all in view is opened
on its own page (a paced, counted navigation, never a click on a "Reply" control).
Pages are opened breadth first, so running out of budget loses the deepest and
latest replies rather than whole top-level branches.

A refresh of a top-level post that was collected in full before does less. The
post's reply count covers its whole thread, so if it has not grown since the last
collection nothing else is opened, and if it has grown the expansion stops as soon
as that many new replies have been found, looking first where a reply count changed.
"""
import random
from collections.abc import Callable
from dataclasses import dataclass, field

from threadsift.collection.browser import PageDriver
from threadsift.collection.budget import BudgetExhausted
from threadsift.collection.capture import Payload, ResponseCapture
from threadsift.collection.extract import Extraction, ExtractionError, extract
from threadsift.collection.monitor import Monitor
from threadsift.config import LimitsConfig
from threadsift.domain import CollectedItem, StoreOutcome, StoreSummary, ThreadBaseline

# A scroll that brings nothing new this many times in a row ends paging a post's replies.
MAX_EMPTY_SCROLLS = 3


class NotFound(Exception):
    pass


@dataclass
class ExpansionResult:
    state: str  # "done", "partial" or "failed"
    reason: str | None
    post_id: str | None
    summary: StoreSummary = field(default_factory=StoreSummary)
    pages_opened: int = 0


class Expander:
    def __init__(
        self,
        driver: PageDriver,
        capture: ResponseCapture,
        store: Callable[[CollectedItem], StoreOutcome],
        limits: LimitsConfig,
        monitor: Monitor | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._driver = driver
        self._capture = capture
        self._store = store
        self._limits = limits
        self._monitor = monitor
        self._rng = rng or random.Random()
        self._summary = StoreSummary()
        self.scroll_log: list[dict] = []

    @property
    def summary(self) -> StoreSummary:
        """What the current (or last) collect() has stored so far, even if it was cut short."""
        return self._summary

    def collect(self, url: str, code: str, baseline: ThreadBaseline | None = None) -> ExpansionResult:
        """Collect one queued URL. Blocking and early-warning stops propagate;
        everything collected before them has already been stored. With a baseline
        (the URL was collected in full before) only what can be new is opened."""
        self._baseline = baseline
        self._growth: int | None = None  # new replies to find; None collects in full
        self._items: dict[str, CollectedItem] = {}
        self._stored: dict[str, CollectedItem] = {}
        self._visited: set[str] = set()
        self._more: dict[str, bool] = {}
        self._summary = StoreSummary()
        self._depth_cut = False
        self._stop: str | None = None
        self._start_id: str | None = None
        self._pages = 0
        self.scroll_log = []
        self._capture.begin_url(code)
        try:
            return self._collect(url, code)
        finally:
            self._capture.end_url()

    # --- the three phases ----------------------------------------------------

    def _collect(self, url: str, code: str) -> ExpansionResult:
        try:
            payloads: list[Payload] = []
            start = self._open(url, code, payloads)
        except NotFound:
            return ExpansionResult("failed", "not found", None, self._summary, self._pages)
        except ExtractionError as error:
            return ExpansionResult("failed", str(error), None, self._summary, self._pages)
        self._start_id = start.target.id
        self._visited.add(start.target.id)
        self._growth = self._growth_since_baseline(start.target)
        if self._growth is not None and self._growth <= 0:
            return ExpansionResult("done", None, start.target.id, self._summary, self._pages)
        try:
            if self._growth is not None:
                self._refresh(start.target, code, payloads)
                return self._finish(start.target.id)
            self._page_through(start.target, code, payloads)
            while (page := self._next_page()) is not None:
                if self._replies_collected() >= self._limits.max_replies:
                    self._stop = f"cap: {self._limits.max_replies} replies"
                    break
                self._visit(page)
        except BudgetExhausted:
            self._stop = "cap: action budget"
        except ExtractionError as error:
            self._stop = str(error)
        return self._finish(start.target.id)

    def _open(self, url: str, code: str, payloads: list[Payload]) -> Extraction:
        """Navigate to a post's page and ingest what it carries."""
        navigation = self._driver.goto(url)
        self._pages += 1
        self._capture.raise_pending()
        if navigation.status == 404:
            raise NotFound(url)
        payloads.extend(self._capture.drain())
        extraction = extract(payloads, code)
        if self._start_id is None:
            self._start_id = extraction.target.id
        self._ingest(extraction)
        return extraction

    def _visit(self, item: CollectedItem) -> None:
        """Open a reply's own page to reach the replies not yet in view."""
        payloads: list[Payload] = []
        known = len(self._items)
        try:
            extraction = self._open(item.url, item.code, payloads)
        except NotFound:
            self._visited.add(item.id)  # deleted since we saw it
            self._reveal(0)
            return
        self._visited.add(item.id)
        self._reveal(len(self._items) - known)
        self._page_through(extraction.target, item.code, payloads, until=self._enough)

    def _growth_since_baseline(self, post: CollectedItem) -> int | None:
        """How many replies the thread has gained since the baseline, or None when the
        page cannot be compared with it."""
        baseline = self._baseline
        if baseline is None or baseline.post_id != post.id or post.reply_count is None:
            return None
        return post.reply_count - baseline.reply_count

    def _refresh(self, start: CollectedItem, code: str, payloads: list[Payload]) -> None:
        """Look for the replies that are new, changed branches first, and stop when
        as many have been found as the thread's count grew."""
        scrolling = True
        while not self._enough():
            if self._replies_collected() >= self._limits.max_replies:
                self._stop = f"cap: {self._limits.max_replies} replies"
                return
            page = self._next_page()
            if page is not None and self._changed(page):
                self._visit(page)
            elif scrolling and self._more.get(start.id):
                self._page_through(start, code, payloads, until=self._found_something_to_open)
                if self._more.get(start.id) and not self._found_something_to_open():
                    scrolling = False  # it gave up (replies stopped loading, or the reply cap)
            elif page is not None:
                self._visit(page)
            else:
                return

    def _changed(self, item: CollectedItem) -> bool:
        """The item is new, or its reply count differs from the one stored before."""
        if self._growth is None or self._baseline is None:
            return False
        known = self._baseline.known
        return item.id not in known or known[item.id] != item.reply_count

    def _new_replies(self) -> int:
        known = self._baseline.known if self._baseline else {}
        return sum(1 for item_id in self._descendants() if item_id not in known)

    def _enough(self) -> bool:
        return self._growth is not None and self._new_replies() >= self._growth

    def _found_something_to_open(self) -> bool:
        if self._enough():
            return True
        page = self._next_page()
        return page is not None and self._changed(page)

    def _page_through(self, owner: CollectedItem, code: str, payloads: list[Payload],
                      until: Callable[[], bool] | None = None) -> None:
        """Scroll to load further pages of a post's direct replies, until `until` holds.

        Threads loads the next batch before the end of the page is reached (how far
        ahead is not known), and many scrolls load nothing, so a scroll that loads
        nothing is only a miss once the page is at its bottom; before that,
        scrolling just continues (each gesture still costs an action).
        """
        empty = 0
        since_batch = 0
        while self._more.get(owner.id):
            if until is not None and until():
                return
            if self._replies_collected() >= self._limits.max_replies:
                self._stop = f"cap: {self._limits.max_replies} replies"
                return
            known = len(self._items)
            before = self._driver.remaining_px()
            self._driver.scroll_screen()
            self._capture.raise_pending()
            payloads.extend(self._capture.drain())
            self._ingest(extract(payloads, code))
            gained = len(self._items) - known
            since_batch += 1
            # What the live check needs: scrolls per batch, and how far from the end of
            # the page a batch arrives.
            self.scroll_log.append({
                "post": code, "scroll_in_batch": since_batch, "new_items": gained,
                "px_to_end_before": before, "px_to_end_after": self._driver.remaining_px(),
            })
            if gained:
                empty = 0
                since_batch = 0
                self._reveal(gained)
            elif self._driver.at_bottom():
                empty += 1
                self._reveal(0)
                if empty >= MAX_EMPTY_SCROLLS:
                    self._stop = "cap: replies stopped loading"
                    return

    # --- bookkeeping -----------------------------------------------------------

    def _reveal(self, new_items: int) -> None:
        if self._monitor is not None:
            self._monitor.record_reveal(new_items, more_expected=True)

    def _ingest(self, extraction: Extraction) -> None:
        for item in extraction.items:
            if (
                item.depth is not None
                and item.depth > self._limits.max_depth
                and item.id != self._start_id
            ):
                self._depth_cut = True
                continue
            self._items[item.id] = item
            if self._stored.get(item.id) != item:
                self._summary.add(self._store(item))
                self._stored[item.id] = item
        self._more.update(extraction.more_replies)

    def _children_count(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self._items.values():
            if item.parent_id is not None:
                counts[item.parent_id] = counts.get(item.parent_id, 0) + 1
        return counts

    def _descendants(self) -> set[str]:
        """Ids of the items below the URL's own post. Its ancestors arrive with the
        page but are never expanded: that would fetch siblings and parents."""
        children: dict[str, list[str]] = {}
        for item in self._items.values():
            if item.parent_id is not None:
                children.setdefault(item.parent_id, []).append(item.id)
        seen: set[str] = set()
        stack = [self._start_id]
        while stack:
            for child in children.get(stack.pop(), []):
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return seen

    def _replies_collected(self) -> int:
        return len(self._descendants())

    def _unresolved(self) -> list[CollectedItem]:
        """Items below the URL's post that have replies we have not seen (and have
        not tried to see)."""
        counts = self._children_count()
        below = self._descendants()
        return [
            item
            for item in self._items.values()
            if item.id in below
            and item.id not in self._visited
            and item.depth is not None
            and (item.reply_count or 0) > counts.get(item.id, 0)
        ]

    def _next_page(self) -> CollectedItem | None:
        """The shallowest, earliest item whose replies can still be opened."""
        candidates = [i for i in self._unresolved() if i.depth < self._limits.max_depth]
        if not candidates:
            return None
        order = {item_id: n for n, item_id in enumerate(self._items)}
        return min(candidates, key=lambda i: (not self._changed(i), i.depth, order[i.id]))

    def _finish(self, post_id: str) -> ExpansionResult:
        if self._enough():  # a refresh that found every new reply is complete, whatever it left unopened
            return ExpansionResult("done", None, post_id, self._summary, self._pages)
        paging = any(more and owner in self._items for owner, more in self._more.items())
        unresolved = self._unresolved()
        if (unresolved or paging) and self._stop:
            state, reason = "partial", self._stop
        elif unresolved or paging or self._depth_cut:
            state, reason = "partial", f"cap: depth {self._limits.max_depth}"
        else:
            state, reason = "done", None
        return ExpansionResult(state, reason, post_id, self._summary, self._pages)
