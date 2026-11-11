"""The browser layer against a real Chrome and a local web server (never Threads).

Opt-in because it needs Google Chrome and starts a browser:

    THREADSIFT_BROWSER_TESTS=1 uv run pytest tests/test_real_browser.py

It exists because test doubles can disagree with the real library about its shape
(they once did: stop() lives on the object that start() returns).
"""
import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from threadsift.collection.browser import PageDriver, open_browser
from threadsift.collection.budget import ActionBudget
from threadsift.collection.capture import ResponseCapture
from threadsift.collection.controls import ForbiddenControl
from threadsift.collection.guard import BlockedError
from threadsift.collection.monitor import EarlyWarning, Monitor
from threadsift.collection.pacing import Pacer
from threadsift.config import BrowserConfig, Config, EarlyWarningConfig, PacingConfig

pytestmark = pytest.mark.skipif(
    not os.environ.get("THREADSIFT_BROWSER_TESTS"), reason="set THREADSIFT_BROWSER_TESTS=1 to run"
)

POST = """<html><head>
<script type="application/json">{"data": {"media": {"pk": "1", "code": "ABC"}}}</script>
</head><body style="margin:0">
<button aria-label="Like" onclick="document.body.dataset.likes = +(document.body.dataset.likes || 0) + 1">Like</button>
<button onclick="document.body.dataset.reveals = +(document.body.dataset.reveals || 0) + 1">View more replies</button>
<p>%s</p>
<div style="height:6000px">tall</div>
<script>fetch('/api/graphql', {method: 'POST', body: 'x'}).then(r => r.json()).then(j => window.fetched = j);</script>
</body></html>""" % ("Нарешті встановив сонячні панелі ☀️ " * 30)

LOGIN = '<html><body><form><input type="password"></form></body></html>'

ODD = """<html><body><script>
fetch('/api/graphql', {method: 'POST', headers: {'X-FB-Friendly-Name': 'OddQuery'}, body: 'odd'});
fetch('/api/graphql', {method: 'POST', headers: {'X-FB-Friendly-Name': 'BadQuery'}, body: 'bad'});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body, kind="text/html; charset=utf-8", headers=()):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        if body == b"odd":  # a reply that is not JSON, as some background calls are
            return self._send(200, "for (;;);{}", "text/plain")
        if body == b"bad":
            return self._send(502, "Bad gateway", "text/plain")
        self._send(200, json.dumps({"data": {"ok": 1}}), "application/json")

    do_GET = lambda self: {  # noqa: E731
        "/@u/post/ABC": lambda: self._send(200, POST),
        "/@u/post/ODD": lambda: self._send(200, ODD),
        "/@u/post/REDIR": lambda: self._send(302, "", headers=[("Location", "/login")]),
        "/@u/post/MISSING": lambda: self._send(404, "<html>gone</html>"),
        "/login": lambda: self._send(200, LOGIN),
    }.get(self.path, lambda: self._send(404, "nope"))()


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def context():
    config = BrowserConfig(session_dir=tempfile.mkdtemp(prefix="realbrowser"), headless=True)
    with open_browser(config) as ctx:
        yield ctx


class Rig:
    def __init__(self, context, budget=150) -> None:
        self.context = context
        self.page = context.new_page()
        self.budget = ActionBudget(10_000, budget)
        self.monitor = Monitor(EarlyWarningConfig())
        self.capture = ResponseCapture(self.page, self.monitor)
        pacer = Pacer(PacingConfig(), sleep=lambda seconds: None)
        self.driver = PageDriver(self.page, self.budget, pacer, self.monitor,
                                 sleep=lambda seconds: None, on_check=self.capture.raise_pending)

    def close(self) -> None:
        self.page.close()


@pytest.fixture
def rig(context):
    r = Rig(context)
    yield r
    r.close()


def test_navigation_captures_the_page_data_and_the_apps_own_requests(server, rig) -> None:
    rig.capture.begin_url("ABC")
    navigation = rig.driver.goto(f"{server}/@u/post/ABC")
    assert navigation.status == 200 and navigation.load_seconds > 0
    time.sleep(0.7)  # the page's own request is still on its way when 'load' fires
    rig.capture.raise_pending()
    payloads = rig.capture.end_url()
    assert [(p.kind, p.data) for p in payloads] == [
        ("embedded", {"data": {"media": {"pk": "1", "code": "ABC"}}}),
        ("graphql", {"data": {"ok": 1}}),
    ]
    assert rig.driver.current_url == f"{server}/@u/post/ABC"
    assert rig.driver.visible_text_length() > 300
    assert rig.budget.run_spent == 1


def test_a_failing_graphql_reply_is_described_and_a_non_json_one_is_not_counted(server, rig) -> None:
    from threadsift.collection.capture import ResponseCapture

    monitor = Monitor(EarlyWarningConfig(failure_window=1, failure_ratio=0.5))
    capture = ResponseCapture(rig.page, monitor)
    capture.begin_url("ODD")
    rig.page.goto(f"{server}/@u/post/ODD")
    rig.page.wait_for_timeout(700)  # time.sleep would block the events from being delivered
    with pytest.raises(EarlyWarning) as caught:
        capture.raise_pending()
    # only the 502 counted; the 200 text/plain reply "OddQuery" is neither a failure nor a success
    assert caught.value.details()["failed_responses"] == [{
        "status": 502, "reason": "HTTP 502", "operation": "BadQuery", "content_type": "text/plain", "bytes": 11,
    }]


def test_a_missing_page_is_reported_not_treated_as_blocking(server, rig) -> None:
    assert rig.driver.goto(f"{server}/@u/post/MISSING").status == 404


def test_a_redirect_to_the_login_page_is_blocking(server, rig) -> None:
    with pytest.raises(BlockedError, match="login wall"):
        rig.driver.goto(f"{server}/@u/post/REDIR")


def test_scrolling_by_a_screen_moves_down_until_the_bottom_is_reached(server, rig) -> None:
    rig.driver.goto(f"{server}/@u/post/ABC")
    start = rig.driver.remaining_px()
    assert start > 3000 and not rig.driver.at_bottom()
    rig.driver.scroll_screen()
    after = rig.driver.remaining_px()
    assert 0 < start - after <= start  # it moved, by at most about a screen
    gestures = 1
    while not rig.driver.at_bottom() and gestures < 40:
        rig.driver.scroll_screen()
        gestures += 1
    assert rig.driver.at_bottom() and rig.driver.remaining_px() <= 300
    assert rig.budget.run_spent == 1 + gestures  # the navigation and every gesture were counted


def test_only_reveal_controls_are_found_and_clicked_with_real_pointer_events(server, rig) -> None:
    rig.driver.goto(f"{server}/@u/post/ABC")
    controls = rig.driver.reveal_controls()
    assert [c.label for c in controls] == ["View more replies"]
    rig.driver.click(controls[0])
    # The DOM is shared with the page's own scripts (Patchright's evaluate runs in an isolated world).
    assert rig.page.evaluate("document.body.dataset.reveals") == "1"
    assert rig.page.evaluate("document.body.dataset.likes || '0'") == "0"


def test_a_control_that_acts_on_the_post_is_refused_and_not_clicked(server, rig) -> None:
    rig.driver.goto(f"{server}/@u/post/ABC")
    spent = rig.budget.run_spent
    # index 0 is the Like button; claiming it is a reveal control does not help
    with pytest.raises(ForbiddenControl):
        rig.driver.click(type(rig.driver.reveal_controls()[0])("View more replies", 0))
    assert rig.page.evaluate("document.body.dataset.likes || '0'") == "0"
    assert rig.budget.run_spent == spent


def test_the_browser_context_has_no_stealth_scripts_and_the_real_window_size(context) -> None:
    page = context.new_page()
    try:
        page.goto("about:blank")
        assert page.evaluate("[window.innerWidth, window.innerHeight]")[0] > 0
        assert page.evaluate("navigator.webdriver") in (False, None)  # removed by the browser setup, not patched in
    finally:
        page.close()


def test_the_browser_closes_cleanly_when_the_user_closes_it_while_we_wait() -> None:
    """Like `login`: wait for the close event, the browser goes away, the context manager
    must exit without an error. In a separate process, as only one Playwright runs per process."""
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent("""
        import pathlib, subprocess, tempfile, threading
        from threadsift.config import BrowserConfig
        from threadsift.collection.browser import open_browser
        config = BrowserConfig(session_dir=tempfile.mkdtemp(prefix="realclose"), headless=True)
        profile = str(pathlib.Path(config.session_dir).resolve())
        threading.Timer(3, lambda: subprocess.run(["pkill", "-f", "user-data-dir=" + profile])).start()
        with open_browser(config) as ctx:
            ctx.new_page().goto("about:blank")
            ctx.wait_for_event("close", timeout=30000)
        print("clean exit")
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=90)
    assert result.returncode == 0 and "clean exit" in result.stdout, result.stderr[-600:]


def test_an_ordinary_close_leaves_the_profile_marked_as_cleanly_exited_with_no_lock() -> None:
    """After the context manager returns, Chrome has finished shutting down: its lock is gone and
    the profile says it exited normally, so the next start shows no "Restore pages?" bubble."""
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent("""
        import json, pathlib, tempfile
        from threadsift.config import BrowserConfig
        from threadsift.collection.browser import open_browser
        config = BrowserConfig(session_dir=tempfile.mkdtemp(prefix="realexit"), headless=True)
        profile = pathlib.Path(config.session_dir).resolve()
        with open_browser(config) as ctx:
            ctx.new_page().goto("about:blank")
        lock = profile / "SingletonLock"
        prefs = json.loads((profile / "Default" / "Preferences").read_text())
        print("lock:", lock.is_symlink() or lock.exists(), "exit_type:", prefs["profile"]["exit_type"])
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=90)
    assert "lock: False exit_type: Normal" in result.stdout, result.stdout + result.stderr[-600:]
