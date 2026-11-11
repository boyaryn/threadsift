import os
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pgserver
import pytest
from sqlalchemy import Engine, text

from threadsift import db

EXTERNAL_URL_ENV = "THREADSIFT_TEST_DATABASE_URL"


def _describe_server(url: str) -> str:
    engine = db.make_engine(url)
    try:
        with engine.connect() as connection:
            version = connection.execute(text("SELECT version()")).scalar().split(",")[0]
    finally:
        engine.dispose()
    return version


@pytest.fixture(scope="session")
def server_url(request: pytest.FixtureRequest) -> Iterator[str]:
    external = os.environ.get(EXTERNAL_URL_ENV)
    if external:
        request.config._threadsift_database = f"{_describe_server(external)}, your server ({EXTERNAL_URL_ENV})"
        yield external
        return
    # A short path: Unix socket paths are limited to about 100 characters.
    pgdata = tempfile.mkdtemp(prefix="tsp")
    server = pgserver.get_server(pgdata)
    try:
        url = server.get_uri()
        request.config._threadsift_database = f"{_describe_server(url)}, temporary bundled server ({EXTERNAL_URL_ENV} is not set)"
        yield url
    finally:
        server.cleanup()
        shutil.rmtree(pgdata, ignore_errors=True)


def pytest_terminal_summary(terminalreporter, config) -> None:
    info = getattr(config, "_threadsift_database", None)
    if info:
        terminalreporter.write_sep("-", f"tests ran against: {info}")


@contextmanager
def throwaway_database(server_url: str) -> Iterator[str]:
    """An empty, uniquely named database that is dropped afterwards."""
    name = f"threadsift_test_{uuid.uuid4().hex[:8]}"
    admin = db.make_engine(server_url)
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield db.sqlalchemy_url(server_url).set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


@pytest.fixture(scope="session")
def database_url(server_url: str) -> Iterator[str]:
    with throwaway_database(server_url) as url:
        db.upgrade(url)
        yield url


@pytest.fixture
def empty_database_url(server_url: str) -> Iterator[str]:
    """An empty database for tests that run migrations themselves."""
    with throwaway_database(server_url) as url:
        yield url


@pytest.fixture(scope="session")
def session_engine(database_url: str) -> Iterator[Engine]:
    engine = db.make_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def clean_engine(session_engine: Engine) -> Iterator[Engine]:
    """The migrated test database, emptied of rows after each test."""
    yield session_engine
    with session_engine.begin() as connection:
        tables = connection.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            )
        ).scalars().all()
        if tables:
            names = ", ".join(f'"{name}"' for name in tables)
            connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
