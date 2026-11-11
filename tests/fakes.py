"""Fakes for the browser automation API. Nothing here touches a real browser."""
from typing import Any


class FakeContext:
    def __init__(self, calls: list, page=None) -> None:
        self.calls = calls
        self.pages: list = []
        self.closed = False
        self._page = page

    def new_page(self):
        if self._page is None:
            raise AssertionError("tests must not open pages unless they expect to")
        self.pages.append(self._page)
        return self._page

    def close(self) -> None:
        self.closed = True
        self.calls.append(("context.close",))

    def __getattr__(self, name: str) -> Any:
        # Anything the app should never use (init scripts, request routing, ...)
        # fails loudly instead of silently working.
        if name in ("add_init_script", "route", "set_extra_http_headers", "set_geolocation",
                    "set_default_timeout_override"):
            raise AssertionError(f"context.{name} must not be used")
        raise AttributeError(name)


class FakeChromium:
    def __init__(self, calls: list, page=None) -> None:
        self.calls = calls
        self.context = FakeContext(calls, page)

    def launch_persistent_context(self, **kwargs):
        self.calls.append(("launch_persistent_context", kwargs))
        return self.context


class FakePlaywright:
    """What the real start() returns: it has chromium and stop()."""

    def __init__(self, calls: list, page=None) -> None:
        self.calls = calls
        self.chromium = FakeChromium(calls, page)

    def stop(self) -> None:
        self.calls.append(("stop",))


class FakePlaywrightManager:
    """What the real sync_playwright() returns: it only has start() (and the context
    manager protocol), no stop()."""

    def __init__(self, backend: str, calls: list, page=None) -> None:
        self.backend = backend
        self.calls = calls
        calls.append(("factory", backend))
        self.playwright = FakePlaywright(calls, page)

    def start(self):
        self.calls.append(("start",))
        return self.playwright


class FakePlaywrightFactory:
    def __init__(self, page=None) -> None:
        self.calls: list = []
        self.manager: FakePlaywrightManager | None = None
        self.page = page

    def __call__(self, backend: str) -> FakePlaywrightManager:
        self.manager = FakePlaywrightManager(backend, self.calls, self.page)
        return self.manager

    @property
    def context(self) -> FakeContext:
        assert self.manager is not None
        return self.manager.playwright.chromium.context


# --- a fake page ------------------------------------------------------------

from threadsift.collection import browser as _browser  # noqa: E402


class Timeline:
    """A fake clock whose sleep advances time instantly."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept.append(seconds)


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class FakeMouse:
    def __init__(self, page: "FakePage") -> None:
        self.page = page

    def move(self, x: float, y: float) -> None:
        self.page.calls.append(("mouse.move", x, y))

    def wheel(self, dx: float, dy: float) -> None:
        self.page.calls.append(("mouse.wheel", dx, dy))
        self.page.after_scroll()

    def click(self, x: float, y: float) -> None:
        self.page.calls.append(("mouse.click", x, y))
        self.page.after_click(x, y)


class FakeLocator:
    def __init__(self, page: "FakePage", selector: str, index: int | None = None) -> None:
        self.page, self.selector, self.index = page, selector, index

    def _elements(self) -> list[dict]:
        return self.page.elements if self.selector == _browser.CONTROL_SELECTOR else []

    def count(self) -> int:
        if self.selector == _browser.PASSWORD_SELECTOR:
            return 1 if self.page.login_form else 0
        return len(self._elements())

    def all_inner_texts(self) -> list[str]:
        return list(self.page.dialogs) if self.selector == _browser.DIALOG_SELECTOR else []

    def evaluate_all(self, js: str) -> list[str]:
        assert js == _browser.JS_LABELS
        return [e["label"] for e in self._elements()]

    def nth(self, index: int) -> "FakeLocator":
        return FakeLocator(self.page, self.selector, index)

    def evaluate(self, js: str) -> str:
        assert js == _browser.JS_LABEL
        return self._elements()[self.index]["label"]

    def bounding_box(self) -> dict | None:
        return self._elements()[self.index].get("box")

    def click(self, *args, **kwargs) -> None:
        raise AssertionError("locator.click() is a scripted click and must never be used")

    def dispatch_event(self, *args, **kwargs) -> None:
        raise AssertionError("dispatch_event must never be used")


class FakePage:
    """Records every call so tests can assert exactly what was done to the page."""

    def __init__(
        self,
        timeline: Timeline,
        elements: list[dict] | None = None,
        viewport: tuple[int, int] = (1280, 800),
        text_length: int = 500,
        load_seconds: float = 1.5,
    ) -> None:
        self.timeline = timeline
        self.elements = elements if elements is not None else []
        self.viewport = viewport
        self.text_length = text_length
        self.load_seconds = load_seconds
        self.login_form = False
        self.at_bottom = True
        self.remaining = 10_000
        self.dialogs: list[str] = []
        self.url = "about:blank"
        self.redirects: dict[str, str] = {}
        self.statuses: dict[str, int] = {}
        self.calls: list[tuple] = []
        self.mouse = FakeMouse(self)
        self.listeners: dict[str, list] = {}
        self.on_goto = None
        self.on_scroll = None
        self.on_click = None

    def goto(self, url: str, wait_until: str | None = None) -> FakeResponse:
        self.calls.append(("goto", url))
        self.timeline.now += self.load_seconds
        self.url = self.redirects.get(url, url)
        if self.on_goto:
            self.on_goto(url)
        return FakeResponse(self.statuses.get(url, 200))

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def evaluate(self, js: str):
        if js == _browser.JS_VIEWPORT:
            return list(self.viewport)
        if js == _browser.JS_TEXT_LENGTH:
            return self.text_length
        if js == _browser.JS_REMAINING:
            return self.remaining
        if js == _browser.JS_AT_BOTTOM:
            return self.at_bottom() if callable(self.at_bottom) else self.at_bottom
        raise AssertionError(f"unexpected script: {js}")

    def on(self, event: str, handler) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def emit(self, event: str, payload) -> None:
        for handler in self.listeners.get(event, []):
            handler(payload)

    def after_scroll(self) -> None:
        if self.on_scroll:
            self.on_scroll()

    def after_click(self, x: float, y: float) -> None:
        if self.on_click:
            self.on_click(x, y)

    @property
    def action_calls(self) -> list[tuple]:
        return [c for c in self.calls if c[0] in ("goto", "mouse.wheel", "mouse.click")]


def reveal_element(label: str = "View more replies", y: float = 300) -> dict:
    return {"label": label, "box": {"x": 100, "y": y, "width": 200, "height": 30}}


class FakeRequest:
    def __init__(self, resource_type: str, headers: dict | None = None, post_data: str | None = None) -> None:
        self.resource_type = resource_type
        self.headers = headers or {}
        self.post_data = post_data


class FakeNetResponse:
    def __init__(self, url: str, status: int = 200, body=None, broken_json: bool = False,
                 html: str | None = None, headers: dict | None = None, request_headers: dict | None = None,
                 post_data: str | None = None, raw: bytes = b"") -> None:
        self.url, self.status, self._body, self._broken = url, status, body, broken_json
        self._html = html
        self.headers = headers or {}
        self._raw = raw
        self.request = FakeRequest("document" if html is not None else "fetch", request_headers, post_data)

    def body(self) -> bytes:
        return self._raw

    def json(self):
        if self._broken:
            raise ValueError("not json")
        return self._body

    def text(self) -> str:
        if self._html is None:
            raise ValueError("not a document")
        return self._html
