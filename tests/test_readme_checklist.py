from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"


def checklist() -> str:
    text = README.read_text()
    return text.split("## Operator checklist", 1)[1].split("\n## ", 1)[0].lower()


def test_checklist_covers_every_point_of_the_ban_risk_design() -> None:
    body = checklist()
    expected = {
        "warm up by hand for one to two weeks": "warm the account up by hand for one to two weeks",
        "complete and verify profile, follow accounts, scroll feed": "complete and verify the profile, follow some accounts",
        "keep using by hand": "keep using it by hand occasionally",
        "same machine and connection": "same machine and home connection",
        "no vpn": "no vpn",
        "login only on collection machine": "only on the machine that runs collection",
        "generous gaps between runs": "leave generous gaps between runs",
        "leave the window alone": "leave the browser window alone during a run",
        "session is private and git-ignored": ".session/",
        "terms prohibit collection": "terms prohibit automated collection",
        "risk cannot be removed": "no measure removes the risk",
        "headless is less safe": "headless",
        "cool-downs": "cool-down",
    }
    missing = [name for name, phrase in expected.items() if phrase not in body]
    assert not missing, missing


def test_the_importing_section_explains_what_a_refresh_skips() -> None:
    section = README.read_text(encoding="utf-8").split("### What a refresh does and what it skips", 1)[1].split("\n## ", 1)[0]
    for phrase in (
        "one page action",                          # an unchanged count costs a single page action
        "Edits and like counts in the parts that are not opened are not refreshed",
        "A deletion balanced by a new reply can hide it",
        "There is no option to force a full rescan",
        "partial, failed or interrupted",           # what is always collected in full
        "points to a reply",
    ):
        assert phrase in section, phrase
