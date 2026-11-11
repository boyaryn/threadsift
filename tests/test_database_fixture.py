from sqlalchemy import Engine, text


def test_connects_to_migrated_database(clean_engine: Engine) -> None:
    with clean_engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar() == 1
        tables = connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ).scalars().all()
    assert "alembic_version" in tables
