import re
import shlex
from pathlib import Path

import pytest
from typer.testing import CliRunner

from threadsift.cli import app

from tests.test_cli_run import env  # noqa: F401  (the CLI test environment with a fake collector)

README = Path(__file__).resolve().parent.parent / "README.md"
runner = CliRunner()


def running_section() -> str:
    return README.read_text().split("## Running collection", 1)[1].split("\n## ", 1)[0]


def documented_commands() -> list[list[str]]:
    blocks = re.findall(r"```\n(.*?)```", running_section(), flags=re.S)
    lines = [line.strip() for block in blocks for line in block.splitlines() if line.strip().startswith("uv run threadsift")]
    return [shlex.split(line)[3:] for line in lines]


def test_the_section_documents_run_runs_the_override_and_capture() -> None:
    commands = documented_commands()
    assert ["run"] in commands and ["run", "--override-cooldown"] in commands
    assert ["runs"] in commands and ["runs", "--limit", "5"] in commands
    assert ["run", "--capture", ".capture"] in commands


def test_every_documented_command_and_option_is_in_the_help() -> None:
    top = runner.invoke(app, ["--help"]).output
    for args in documented_commands():
        assert args[0] in top
        sub = runner.invoke(app, [args[0], "--help"]).output
        for option in (a for a in args[1:] if a.startswith("--")):
            assert option in sub, (args, option)


@pytest.mark.parametrize("args", documented_commands(), ids=lambda a: " ".join(a))
def test_every_documented_example_runs_against_a_fake_collector(env, args) -> None:  # noqa: F811
    env.add("A", "B", "C")
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output


def test_every_ending_in_the_table_is_one_the_run_can_report() -> None:
    from threadsift.collection.runner import END_REASON_TEXT

    endings = set(re.findall(r"^\| `(\w+)` \|", running_section(), flags=re.M))
    assert endings == set(END_REASON_TEXT)
