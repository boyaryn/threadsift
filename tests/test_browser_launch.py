import os
from pathlib import Path

import pytest

from threadsift.collection.browser import (
    NoSessionError,
    build_launch_spec,
    mark_session_saved,
    open_browser,
    require_session,
    session_exists,
)
from threadsift.config import BrowserConfig

from tests.fakes import FakePlaywrightFactory

FORBIDDEN_KEYS = {
    "user_agent", "viewport", "screen", "locale", "timezone_id", "proxy", "geolocation",
    "permissions", "extra_http_headers", "color_scheme", "device_scale_factor", "is_mobile",
    "has_touch", "java_script_enabled", "bypass_csp", "offline", "http_credentials",
}


def config(tmp_path: Path, **overrides) -> BrowserConfig:
    return BrowserConfig(session_dir=str(tmp_path / "session"), **overrides)


def test_patchright_launch_options_are_exactly_the_recommended_setup(tmp_path: Path) -> None:
    spec = build_launch_spec(config(tmp_path))
    assert spec.backend == "patchright"
    assert spec.kwargs == {
        "user_data_dir": str((tmp_path / "session").resolve()),
        "channel": "chrome",
        "headless": False,
        "no_viewport": True,
    }


def test_playwright_fallback_removes_the_automation_flag_and_adds_the_blink_switch(tmp_path: Path) -> None:
    spec = build_launch_spec(config(tmp_path, backend="playwright"))
    assert spec.backend == "playwright"
    assert spec.kwargs == {
        "user_data_dir": str((tmp_path / "session").resolve()),
        "channel": "chrome",
        "headless": False,
        "no_viewport": True,
        "ignore_default_args": ["--enable-automation"],
        "args": ["--disable-blink-features=AutomationControlled"],
    }


@pytest.mark.parametrize("backend", ["patchright", "playwright"])
def test_no_fingerprint_overrides(tmp_path: Path, backend: str) -> None:
    spec = build_launch_spec(config(tmp_path, backend=backend))
    assert not (FORBIDDEN_KEYS & spec.kwargs.keys())


def test_headless_follows_config_and_can_be_overridden(tmp_path: Path) -> None:
    assert build_launch_spec(config(tmp_path, headless=True)).kwargs["headless"] is True
    assert build_launch_spec(config(tmp_path, headless=True), headless=False).kwargs["headless"] is False
    assert build_launch_spec(config(tmp_path), headless=True).kwargs["headless"] is True


def test_channel_comes_from_config(tmp_path: Path) -> None:
    assert build_launch_spec(config(tmp_path, channel="chrome-beta")).kwargs["channel"] == "chrome-beta"


def test_open_browser_launches_with_the_spec_and_closes_everything(tmp_path: Path) -> None:
    factory = FakePlaywrightFactory()
    cfg = config(tmp_path)
    with open_browser(cfg, playwright_factory=factory) as context:
        assert context is factory.context
        assert not context.closed
    assert factory.context.closed
    names = [c[0] for c in factory.calls]
    assert names == ["factory", "start", "launch_persistent_context", "context.close", "stop"]
    assert factory.calls[0] == ("factory", "patchright")
    assert factory.calls[2][1] == build_launch_spec(cfg).kwargs
    assert (tmp_path / "session").is_dir()


def test_fallback_backend_is_selected_by_config(tmp_path: Path) -> None:
    factory = FakePlaywrightFactory()
    with open_browser(config(tmp_path, backend="playwright"), playwright_factory=factory):
        pass
    assert factory.calls[0] == ("factory", "playwright")


def test_browser_is_closed_even_if_the_run_fails(tmp_path: Path) -> None:
    factory = FakePlaywrightFactory()
    with pytest.raises(RuntimeError):
        with open_browser(config(tmp_path), playwright_factory=factory):
            raise RuntimeError("boom")
    assert [c[0] for c in factory.calls][-2:] == ["context.close", "stop"]


def test_context_forbids_stealth_scripts_and_resource_blocking(tmp_path: Path) -> None:
    factory = FakePlaywrightFactory()
    with open_browser(config(tmp_path), playwright_factory=factory) as context:
        for forbidden in ("add_init_script", "route"):
            with pytest.raises(AssertionError):
                getattr(context, forbidden)


def test_session_marker(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    assert not session_exists(cfg)
    with pytest.raises(NoSessionError, match="threadsift login"):
        require_session(cfg)
    (tmp_path / "session").mkdir()
    mark_session_saved(cfg)
    assert session_exists(cfg)
    require_session(cfg)


@pytest.mark.parametrize("module", ["patchright.sync_api", "playwright.sync_api"])
def test_the_fakes_have_the_shape_of_the_real_library(module: str) -> None:
    """start() belongs to what sync_playwright() returns; stop() to what start() returns.
    The fakes once had stop() on the wrong object and hid a crash on every real run."""
    import importlib

    api = importlib.import_module(module)
    manager = api.sync_playwright()  # creating it starts nothing
    assert callable(manager.start) and not hasattr(manager, "stop")
    assert callable(api.Playwright.stop)

    from tests.fakes import FakePlaywrightManager

    fake = FakePlaywrightManager("patchright", [])
    assert callable(fake.start) and not hasattr(fake, "stop")
    assert callable(fake.start().stop)


def test_waiting_for_chrome_to_exit_returns_as_soon_as_the_profile_lock_is_gone(tmp_path) -> None:
    from threadsift.collection.browser import wait_for_chrome_exit

    (tmp_path / "SingletonLock").symlink_to(f"host-{os.getpid()}")  # Chrome's lock is a dangling symlink
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds
        if now[0] >= 1.0:
            (tmp_path / "SingletonLock").unlink()

    assert wait_for_chrome_exit(tmp_path, timeout=20, sleep=sleep, clock=lambda: now[0]) is True
    assert 1.0 <= now[0] < 2.0


def test_waiting_for_chrome_to_exit_gives_up_at_the_timeout_without_raising(tmp_path) -> None:
    from threadsift.collection.browser import wait_for_chrome_exit

    (tmp_path / "SingletonLock").symlink_to(f"host-{os.getpid()}")
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    assert wait_for_chrome_exit(tmp_path, timeout=5, sleep=sleep, clock=lambda: now[0]) is False
    assert 5 <= now[0] < 6


def test_there_is_nothing_to_wait_for_when_no_lock_exists(tmp_path) -> None:
    from threadsift.collection.browser import wait_for_chrome_exit

    assert wait_for_chrome_exit(tmp_path, sleep=lambda s: pytest.fail("slept")) is True


def test_a_lock_left_by_a_chrome_that_is_gone_is_not_waited_for(tmp_path) -> None:
    from threadsift.collection.browser import wait_for_chrome_exit

    (tmp_path / "SingletonLock").symlink_to("host-999999999")  # no such process
    assert wait_for_chrome_exit(tmp_path, sleep=lambda s: pytest.fail("slept")) is True
