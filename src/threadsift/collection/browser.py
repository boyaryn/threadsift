import os
import random
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from threadsift.collection.budget import ActionBudget
from threadsift.collection.controls import ForbiddenControl, is_reveal_label
from threadsift.collection.guard import BlockedError, check_page
from threadsift.collection.monitor import Monitor
from threadsift.collection.pacing import Pacer, Point, real_sleep
from threadsift.config import BrowserConfig

SESSION_MARKER = ".threadsift-session"
LOGIN_URL = "https://www.threads.com/login"


class NoSessionError(Exception):
    """There is no saved session; the operator must run `threadsift login` first."""


@dataclass(frozen=True)
class LaunchSpec:
    backend: str
    kwargs: dict[str, Any] = field(default_factory=dict)


def build_launch_spec(config: BrowserConfig, headless: bool | None = None) -> LaunchSpec:
    """Browser launch settings.

    The real, installed Chrome with a persistent profile and a real window size.
    No custom user agent, viewport, locale, time zone or proxy, no blocked
    resources and no JavaScript patches: an honest browser is less suspicious
    than an inconsistent one.
    """
    kwargs: dict[str, Any] = {
        "user_data_dir": str(Path(config.session_dir).resolve()),
        "channel": config.channel,
        "headless": config.headless if headless is None else headless,
        "no_viewport": True,
    }
    if config.backend == "playwright":
        # Patchright removes these automation traces itself; plain Playwright needs help.
        kwargs["ignore_default_args"] = ["--enable-automation"]
        kwargs["args"] = ["--disable-blink-features=AutomationControlled"]
    return LaunchSpec(config.backend, kwargs)


def session_exists(config: BrowserConfig) -> bool:
    return (Path(config.session_dir) / SESSION_MARKER).is_file()


def mark_session_saved(config: BrowserConfig) -> None:
    (Path(config.session_dir) / SESSION_MARKER).write_text("login completed\n")


def require_session(config: BrowserConfig) -> None:
    if not session_exists(config):
        raise NoSessionError(
            "No saved Threads session. Run `threadsift login` first and log in by hand."
        )


def _default_playwright_factory(backend: str) -> Any:
    if backend == "patchright":
        from patchright.sync_api import sync_playwright
    else:
        from playwright.sync_api import sync_playwright
    return sync_playwright()


@contextmanager
def open_browser(
    config: BrowserConfig,
    headless: bool | None = None,
    playwright_factory: Callable[[str], Any] = _default_playwright_factory,
) -> Iterator[Any]:
    """Open the persistent Chrome profile and yield its browser context."""
    spec = build_launch_spec(config, headless)
    Path(spec.kwargs["user_data_dir"]).mkdir(parents=True, exist_ok=True)
    # sync_playwright() returns a manager whose start() returns the Playwright object;
    # it is that object, not the manager, that has stop().
    playwright = playwright_factory(spec.backend).start()
    try:
        context = playwright.chromium.launch_persistent_context(**spec.kwargs)
        try:
            yield context
        finally:
            context.close()
            wait_for_chrome_exit(spec.kwargs["user_data_dir"])
    finally:
        playwright.stop()


def wait_for_chrome_exit(
    profile_dir: str | Path,
    timeout: float = 20.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Wait until Chrome has finished shutting down and released the profile.

    Chrome keeps a SingletonLock in the profile directory while it runs and removes it
    as its last step, after it has written its clean-exit mark. Stopping Playwright
    earlier kills the browser mid-shutdown, and the next start then offers to
    "Restore pages?" as after a crash. Returns False if Chrome was still running when
    the timeout ran out (nothing is raised: the session itself is safe).
    """
    lock = Path(profile_dir) / "SingletonLock"
    deadline = clock() + timeout
    while _lock_is_held(lock):
        if clock() >= deadline:
            return False
        sleep(0.2)
    return True


def _lock_is_held(lock: Path) -> bool:
    """True while the lock exists and the Chrome process named in it (`host-PID`) is alive;
    a lock left behind by a killed Chrome is stale and must not make us wait."""
    if not (lock.is_symlink() or lock.exists()):
        return False
    try:
        pid = int(os.readlink(lock).rsplit("-", 1)[1])
        os.kill(pid, 0)
    except (ValueError, IndexError, OSError) as error:
        return isinstance(error, PermissionError)  # alive but not ours; anything else: unreadable or dead
    return True


# --- Driving a page ---------------------------------------------------------

HOME_URL = "https://www.threads.com/"
MIN_SCROLL = 120
CONTROL_SELECTOR = "button, [role='button'], a"
PASSWORD_SELECTOR = "input[type='password']"
DIALOG_SELECTOR = "[role='dialog'], [role='alert']"
JS_VIEWPORT = "() => [window.innerWidth, window.innerHeight]"
JS_AT_BOTTOM = "() => window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 300"
JS_REMAINING = "() => document.documentElement.scrollHeight - window.scrollY - window.innerHeight"
JS_TEXT_LENGTH = "() => document.body ? document.body.innerText.length : 0"
JS_LABELS = "els => els.map(e => (e.getAttribute('aria-label') || e.innerText || '').trim())"
JS_LABEL = "e => (e.getAttribute('aria-label') || e.innerText || '').trim()"


@dataclass(frozen=True)
class Control:
    """A content-revealing control found on the page (an index into the page's
    clickable elements; its label is read again at click time)."""

    label: str
    index: int


@dataclass(frozen=True)
class Navigation:
    status: int | None
    load_seconds: float


class ControlNotVisible(Exception):
    """The control is not fully inside the window; scroll to it first."""


class PageDriver:
    """The only way collection acts on a page.

    Every action is counted against the action budget before anything happens,
    followed by a human-like pause, and the page is checked for blocking signs
    afterwards. Only reveal controls can be clicked, with real pointer events
    and never with a script. The raw page is deliberately not exposed.
    """

    def __init__(
        self,
        page: Any,
        budget: ActionBudget,
        pacer: Pacer,
        monitor: Monitor | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
        rng: random.Random | None = None,
        on_check: Callable[[], None] | None = None,
    ) -> None:
        self._on_check = on_check
        self._page = page
        self._budget = budget
        self._pacer = pacer
        self._monitor = monitor
        self._clock = clock
        self._sleep = sleep or real_sleep
        self._rng = rng or random.Random()
        self._pointer: Point | None = None

    @property
    def current_url(self) -> str:
        return self._page.url

    def visible_text_length(self) -> int:
        return int(self._page.evaluate(JS_TEXT_LENGTH) or 0)

    def remaining_px(self) -> int:
        """Pixels left to scroll before the end of the page. A read, not an action."""
        return int(self._page.evaluate(JS_REMAINING))

    def at_bottom(self) -> bool:
        """Whether the page is scrolled to (nearly) its end. A read, not an action."""
        return bool(self._page.evaluate(JS_AT_BOTTOM))

    def goto(self, url: str, record_load: bool = True) -> Navigation:
        self._budget.spend()
        started = self._clock()
        response = self._page.goto(url, wait_until="load")
        load_seconds = self._clock() - started
        status = response.status if response is not None else None
        if status == 429:
            raise BlockedError("rate limited", "HTTP 429")
        self._check_page()
        if record_load and self._monitor is not None:
            self._monitor.record_page_load(load_seconds)
        self._pause()
        return Navigation(status, load_seconds)

    def scroll(self, distance: int) -> None:
        """One scroll gesture (several wheel ticks) counts as one action."""
        self._budget.spend()
        self._ensure_pointer()
        for tick in self._pacer.scroll_gesture(distance):
            self._sleep(tick.delay_before)
            self._page.mouse.wheel(0, tick.delta_y)
        self._check_page()
        self._pause()

    def scroll_screen(self) -> None:
        """One scroll gesture of about a screen (75 to 100% of the window's height, like
        Page Down), never past the end of the page and never a mere twitch."""
        _, height = self._viewport()
        remaining = float(self.remaining_px())
        step = height * self._rng.uniform(0.75, 1.0)
        self.scroll(int(max(min(step, remaining), MIN_SCROLL)))

    def reveal_controls(self) -> list[Control]:
        labels = self._page.locator(CONTROL_SELECTOR).evaluate_all(JS_LABELS)
        return [Control(label, i) for i, label in enumerate(labels) if is_reveal_label(label)]

    def click(self, control: Control) -> None:
        locator = self._page.locator(CONTROL_SELECTOR).nth(control.index)
        label = locator.evaluate(JS_LABEL)
        if not is_reveal_label(label):
            raise ForbiddenControl(label)
        box = locator.bounding_box()
        width, height = self._viewport()
        if (
            box is None
            or box["x"] < 0
            or box["y"] < 0
            or box["x"] + box["width"] > width
            or box["y"] + box["height"] > height
        ):
            raise ControlNotVisible(label)
        self._budget.spend()
        target = (
            box["x"] + box["width"] * self._rng.uniform(0.25, 0.75),
            box["y"] + box["height"] * self._rng.uniform(0.25, 0.75),
        )
        self._move_pointer(target)
        self._page.mouse.click(*target)
        self._check_page()
        self._pause()

    def warm_up_home_feed(self) -> None:
        """Browse the home feed for a short random time, like a person opening the app."""
        seconds = self._pacer.warmup_seconds()
        started = self._clock()
        self.goto(HOME_URL, record_load=False)
        while self._clock() - started < seconds:
            self.scroll_screen()

    def _viewport(self) -> tuple[float, float]:
        width, height = self._page.evaluate(JS_VIEWPORT)
        return float(width), float(height)

    def _ensure_pointer(self) -> None:
        if self._pointer is None:
            width, height = self._viewport()
            self._move_pointer(
                (width * self._rng.uniform(0.35, 0.65), height * self._rng.uniform(0.35, 0.65))
            )

    def _move_pointer(self, target: Point) -> None:
        for x, y in self._pacer.pointer_path(self._pointer or (0.0, 0.0), target):
            self._page.mouse.move(x, y)
            self._sleep(self._rng.uniform(0.004, 0.02))
        self._pointer = target

    def _check_page(self) -> None:
        dialogs = " ".join(self._page.locator(DIALOG_SELECTOR).all_inner_texts())
        check_page(
            self._page.url,
            login_form_present=self._page.locator(PASSWORD_SELECTOR).count() > 0,
            dialog_text=dialogs,
        )
        if self._on_check is not None:
            self._on_check()

    def _pause(self) -> None:
        self._pacer.action_pause(self.visible_text_length())
