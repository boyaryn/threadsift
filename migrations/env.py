from alembic import context
from sqlalchemy import create_engine, pool

config = context.config

if context.is_offline_mode():
    raise RuntimeError("Offline migrations are not supported; run against a database.")

url = config.get_main_option("sqlalchemy.url")
if not url:
    # Plain `alembic ...` on the command line: use threadsift's own configuration.
    from threadsift.config import load_config
    from threadsift.db import sqlalchemy_url

    url = sqlalchemy_url(load_config().database.url)

engine = create_engine(url, poolclass=pool.NullPool)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
