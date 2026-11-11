from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from threadsift.cli import app
from threadsift.collection import browser

runner = CliRunner()


class FakePage:
    def __init__(self, visited: list) -> None:
        self.visited = visited

    def goto(self, url: str, **kwargs) -> None:
        self.visited.append(url)


class FakeLoginContext:
    def __init__(self, visited: list, interrupt: bool = False) -> None:
        self.visited = visited
        self.interrupt = interrupt
        self.pages: list = []

    def new_page(self) -> FakePage:
        return FakePage(self.visited)

    def wait_for_event(self, event: str, timeout: int = 0) -> None:
        assert event == "close" and timeout == 0
        if self.interrupt:
            raise KeyboardInterrupt


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def patch_browser(monkeypatch: pytest.MonkeyPatch, **context_args):
    opened: list = []
    visited: list = []

    @contextmanager
    def fake_open_browser(config, headless=None, **kwargs):
        opened.append({"headless": headless, "session_dir": config.session_dir})
        Path(config.session_dir).mkdir(parents=True, exist_ok=True)
        yield FakeLoginContext(visited, **context_args)

    monkeypatch.setattr(browser, "open_browser", fake_open_browser)
    return opened, visited


def test_login_opens_a_visible_window_and_saves_the_session(workdir: Path, monkeypatch) -> None:
    opened, visited = patch_browser(monkeypatch)
    result = runner.invoke(app, ["login"])
    assert result.exit_code == 0, result.output
    assert opened == [{"headless": False, "session_dir": ".session"}]
    assert visited == [browser.LOGIN_URL]
    assert (workdir / ".session" / browser.SESSION_MARKER).exists()
    assert "Session saved" in result.output


def test_login_is_visible_even_if_config_says_headless(workdir: Path, monkeypatch) -> None:
    (workdir / "config.toml").write_text("[browser]\nheadless = true\n")
    opened, _ = patch_browser(monkeypatch)
    assert runner.invoke(app, ["login"]).exit_code == 0
    assert opened[0]["headless"] is False


def test_interrupted_login_saves_nothing(workdir: Path, monkeypatch) -> None:
    patch_browser(monkeypatch, interrupt=True)
    result = runner.invoke(app, ["login"])
    assert result.exit_code == 1
    assert not (workdir / ".session" / browser.SESSION_MARKER).exists()


def test_run_without_a_saved_session_opens_nothing_and_says_to_log_in(workdir: Path, monkeypatch) -> None:
    opened, visited = patch_browser(monkeypatch)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 1
    assert "threadsift login" in result.output
    assert opened == [] and visited == []


def test_run_then_login_then_run_gets_past_the_session_check(workdir: Path, monkeypatch) -> None:
    patch_browser(monkeypatch)
    assert "threadsift login" in runner.invoke(app, ["run"]).output
    runner.invoke(app, ["login"])
    after = runner.invoke(app, ["run"])
    assert "threadsift login" not in after.output
