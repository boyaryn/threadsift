from pathlib import Path

import pytest

from threadsift.config import Config, ConfigError, load_config

ROOT = Path(__file__).resolve().parent.parent


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_explicit_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml", env={})
    with pytest.raises(ConfigError):
        load_config(env={"THREADSIFT_CONFIG": str(tmp_path / "missing.toml")})


def test_defaults_when_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = load_config(env={})
    assert config == Config()
    assert config.database.url == "postgresql://threadsift@localhost/threadsift"
    assert config.pacing.action_pause_seconds == (4, 12)
    assert config.pacing.url_pause_seconds == (30, 90)
    assert config.pacing.urls_per_break == (10, 15)
    assert config.pacing.break_minutes == (3, 8)
    assert config.pacing.warmup_minutes == (1, 3)
    assert config.pacing.dwell_max_chars == 2000
    assert config.limits.urls_per_run == 60
    assert config.limits.url_action_budget == 150
    assert config.limits.run_action_budget == 1200
    assert config.limits.max_replies == 1000
    assert config.limits.max_depth == 5
    assert config.cooldown.blocked_hours == 24
    assert config.cooldown.early_warning_hours == 6
    assert config.early_warning.empty_reveals == 3
    assert config.early_warning.failure_window == 20
    assert config.early_warning.failure_ratio == 0.3
    assert config.early_warning.slowdown_pages == 5
    assert config.early_warning.slowdown_factor == 3
    assert config.browser.headless is False
    assert config.browser.channel == "chrome"
    assert config.browser.backend == "patchright"


def test_file_values_override_defaults(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
[limits]
urls_per_run = 10
max_depth = 3

[pacing]
action_pause_seconds = [1, 2]

[browser]
backend = "playwright"
headless = true
""",
    )
    config = load_config(path, env={})
    assert config.limits.urls_per_run == 10
    assert config.limits.max_depth == 3
    assert config.limits.url_action_budget == 150
    assert config.pacing.action_pause_seconds == (1, 2)
    assert config.browser.backend == "playwright"
    assert config.browser.headless is True


def test_env_overrides_database_url(tmp_path: Path) -> None:
    path = write(tmp_path, '[database]\nurl = "postgresql:///from_file"\n')
    assert load_config(path, env={}).database.url == "postgresql:///from_file"
    config = load_config(path, env={"THREADSIFT_DATABASE_URL": "postgresql:///from_env"})
    assert config.database.url == "postgresql:///from_env"


def test_env_selects_config_file(tmp_path: Path) -> None:
    path = write(tmp_path, "[limits]\nurls_per_run = 7\n")
    config = load_config(env={"THREADSIFT_CONFIG": str(path)})
    assert config.limits.urls_per_run == 7


@pytest.mark.parametrize(
    "text",
    [
        "[limits]\nurls_per_run = 0\n",
        "[limits]\nurls_per_run = 1.5\n",
        '[limits]\nurls_per_run = "many"\n',
        "[limits]\nurl_action_budget = 500\nrun_action_budget = 100\n",
        "[pacing]\naction_pause_seconds = [12, 4]\n",
        "[pacing]\naction_pause_seconds = [4]\n",
        "[pacing]\nurls_per_break = [0, 5]\n",
        "[pacing]\ndwell_max_chars = -1\n",
        '[browser]\nbackend = "selenium"\n',
        '[browser]\nheadless = "yes"\n',
        "[cooldown]\nblocked_hours = -1\n",
        "[early_warning]\nfailure_ratio = 1.5\n",
        "[early_warning]\nslowdown_factor = 1\n",
        "[limits]\nunknown_key = 1\n",
        "[nonsense]\nx = 1\n",
        "this is not toml",
    ],
)
def test_invalid_values_are_rejected(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text), env={})


def test_example_config_matches_defaults() -> None:
    assert load_config(ROOT / "config.example.toml", env={}) == Config()


def test_readme_documents_only_existing_keys() -> None:
    import re
    import tomllib

    readme = (ROOT / "README.md").read_text()
    section = readme.split("## Configuration", 1)[1].split("\n## ", 1)[0]
    rows = "\n".join(line for line in section.splitlines() if line.startswith("|"))
    documented = set(re.findall(r"`([a-z_]+\.[a-z_]+)`", rows))
    example = tomllib.loads((ROOT / "config.example.toml").read_text())
    existing = {f"{name}.{key}" for name, table in example.items() for key in table}
    assert documented == existing
