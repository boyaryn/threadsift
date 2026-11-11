from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import typer
from sqlalchemy.exc import SQLAlchemyError

from threadsift import db
from threadsift.archive.repository import ArchiveRepository
from threadsift.archive.runs import RunLog
from threadsift.collection import browser
from threadsift.collection import collector as collector_module
from threadsift.collection.runner import CooldownActive, Runner, describe, describe_wait
from threadsift.config import Config, ConfigError, load_config
from threadsift.intake.queue import UrlQueue
from threadsift.review.traits import get_active_trait, set_active_trait

app = typer.Typer(
    help="Collect Threads posts and replies and review them for one chosen trait.",
    no_args_is_help=True,
)


def _load_config() -> Config:
    try:
        return load_config()
    except ConfigError as error:
        typer.echo(f"Configuration error: {error}", err=True)
        raise typer.Exit(code=2) from error


@contextmanager
def _database_errors() -> Iterator[None]:
    try:
        yield
    except SQLAlchemyError as error:
        typer.echo(f"Database error: {error}", err=True)
        raise typer.Exit(code=1) from error


def _not_implemented() -> None:
    typer.echo("Not implemented yet.", err=True)
    raise typer.Exit(code=1)


@app.command("db-upgrade")
def db_upgrade() -> None:
    """Create or upgrade the database schema."""
    url = _load_config().database.url
    stray = db.stray_migration_files()
    if stray:
        typer.echo(
            f"Unexpected Python files in migrations/versions: {', '.join(stray)}\n"
            "Alembic would try to run them. Names starting with '._' are macOS copy metadata; "
            "delete them with:\n  find . -name '._*' -not -path './.venv/*' -delete",
            err=True,
        )
        raise typer.Exit(code=1)
    with _database_errors():
        before = db.current_revision(url)
        head = db.head_revision(url)
        if before == head:
            typer.echo(f"Database is already up to date (revision {head}).")
            return
        db.upgrade(url)
    typer.echo(f"Database upgraded to revision {head}.")


trait_app = typer.Typer(help="Define and show the trait being searched for.", no_args_is_help=True)
app.add_typer(trait_app, name="trait")


@trait_app.command("set")
def trait_set(
    name: str = typer.Argument(..., help="Short name, for example pro-renewables."),
    description: str | None = typer.Option(
        None, "--description", "-d", help="What the trait means, in free text."
    ),
) -> None:
    """Create a trait (or pick an existing one) and make it the active trait."""
    if not name.strip():
        typer.echo("The trait name must not be empty.", err=True)
        raise typer.Exit(code=2)
    engine = db.make_engine(_load_config().database.url)
    try:
        with _database_errors():
            trait = set_active_trait(engine, name, description)
    finally:
        engine.dispose()
    typer.echo(f"Active trait: {trait.name}")
    if trait.description:
        typer.echo(f"Description: {trait.description}")


@trait_app.command("show")
def trait_show() -> None:
    """Show the active trait."""
    engine = db.make_engine(_load_config().database.url)
    try:
        with _database_errors():
            trait = get_active_trait(engine)
    finally:
        engine.dispose()
    if trait is None:
        typer.echo("No active trait. Define one with: threadsift trait set NAME --description TEXT")
        return
    typer.echo(f"Active trait: {trait.name}")
    if trait.description:
        typer.echo(f"Description: {trait.description}")


@app.command("login")
def login() -> None:
    """Log in to Threads by hand in a visible Chrome window and save the session."""
    config = _load_config().browser
    typer.echo(
        "A Chrome window will open. Log in to the dedicated Threads account by hand, "
        "then close the window to save the session."
    )
    try:
        with browser.open_browser(config, headless=False) as context:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(browser.LOGIN_URL)
            context.wait_for_event("close", timeout=0)
    except KeyboardInterrupt:
        typer.echo("Login cancelled; nothing was saved.", err=True)
        raise typer.Exit(code=1) from None
    browser.mark_session_saved(config)
    typer.echo(f"Session saved in {config.session_dir}. If you did not finish logging in, run login again.")


@app.command("import")
def import_urls(
    file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True, help="Text file with one post URL per line."
    ),
) -> None:
    """Import post URLs from a text file into the queue."""
    engine = db.make_engine(_load_config().database.url)
    try:
        with _database_errors():
            report = UrlQueue(engine).import_file(file)
    finally:
        engine.dispose()
    typer.echo(f"Added: {report.added}")
    typer.echo(f"Already pending: {report.already_pending}")
    typer.echo(f"Refresh requested: {report.refresh_requested}")
    typer.echo(f"Rejected: {len(report.rejected)}")
    for rejection in report.rejected:
        typer.echo(f"  line {rejection.line_number}: {rejection.text} ({rejection.reason})")


@app.command("run")
def run(
    override_cooldown: bool = typer.Option(
        False,
        "--override-cooldown",
        help="Start even though a cool-down after a blocked or warned run is still running "
        "(not recommended; the run log marks it).",
    ),
    capture: Path | None = typer.Option(
        None,
        "--capture",
        file_okay=False,
        help="Debug: collect only the first pending URL and save the intercepted payloads "
        "to this directory (to build test fixtures).",
    ),
) -> None:
    """Run collection over pending URLs."""
    config = _load_config()
    try:
        browser.require_session(config.browser)
    except browser.NoSessionError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from error

    engine = db.make_engine(config.database.url)

    def open_collector(budget, store):
        return collector_module.open_browser_collector(
            config, budget, store, capture_dir=capture, baselines=ArchiveRepository(engine).baseline_for
        )

    try:
        with _database_errors():
            result = Runner(engine, config, open_collector).run(
                override_cooldown=override_cooldown, url_limit=1 if capture else None
            )
    except CooldownActive as cooldown:
        typer.echo(
            f"Not starting: {cooldown}. Runs are possible again in {describe_wait(cooldown.remaining)} "
            f"(after {cooldown.until:%Y-%m-%d %H:%M} UTC).\n"
            "To start anyway, at a higher risk for the account: threadsift run --override-cooldown",
            err=True,
        )
        raise typer.Exit(code=1) from cooldown
    finally:
        engine.dispose()
    typer.echo(describe(result))
    if result.end_reason == "interrupted":
        raise typer.Exit(code=130)
    if result.end_reason in ("blocked", "early_warning", "error"):
        raise typer.Exit(code=1)


@app.command("runs")
def runs(
    limit: int = typer.Option(20, "--limit", "-n", min=1, help="How many of the latest runs to show."),
) -> None:
    """Show the run log."""
    engine = db.make_engine(_load_config().database.url)
    try:
        with _database_errors():
            rows = RunLog(engine).recent(limit)
    finally:
        engine.dispose()
    if not rows:
        typer.echo("No runs yet.")
        return
    typer.echo("  id  started (UTC)     took    ended               actions  done partial failed pending")
    for row in rows:
        took = ""
        if row["ended_at"] is not None:
            minutes = round((row["ended_at"] - row["started_at"]).total_seconds() / 60)
            took = f"{minutes} min"
        reason = (row["end_reason"] or "unfinished") + (" *" if row["cooldown_overridden"] else "")
        typer.echo(
            f"{row['id']:>4}  {row['started_at']:%Y-%m-%d %H:%M}  {took:<7} {reason:<18}"
            f"{row['actions_spent']:>8}  {row['urls_done']:>4} {row['urls_partial']:>7} "
            f"{row['urls_failed']:>6} {row['urls_pending']:>7}"
        )
    if any(r["cooldown_overridden"] for r in rows):
        typer.echo("* started during a cool-down (overridden)")
