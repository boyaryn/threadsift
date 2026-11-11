import re
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL, make_url

REPO_ROOT = Path(__file__).resolve().parents[2]


def sqlalchemy_url(url: str) -> URL:
    parsed = make_url(url)
    if parsed.drivername in ("postgresql", "postgres"):
        parsed = parsed.set(drivername="postgresql+psycopg")
    return parsed


def make_engine(url: str) -> Engine:
    return create_engine(sqlalchemy_url(url))


def alembic_config(url: str) -> AlembicConfig:
    config = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    rendered = sqlalchemy_url(url).render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", rendered.replace("%", "%%"))
    return config


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str = "base") -> None:
    command.downgrade(alembic_config(url), revision)


def stray_migration_files(directory: Path | None = None) -> list[str]:
    """Python files in the migrations folder that are not migrations. Alembic would
    try to run them; typically they are macOS copy metadata files named like ._0001_x.py."""
    folder = directory or REPO_ROOT / "migrations" / "versions"
    return sorted(
        p.name for p in folder.iterdir()
        if p.is_file() and p.suffix == ".py" and not re.fullmatch(r"\d{4}_\w+\.py", p.name)
    )


def head_revision(url: str) -> str | None:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config(url)).get_current_head()


def current_revision(url: str) -> str | None:
    from alembic.runtime.migration import MigrationContext

    engine = make_engine(url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()
