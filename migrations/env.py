from alembic import context
from sqlalchemy import create_engine

from atlas.config import get_settings

config = context.config


def run_migrations_online() -> None:
    # Allow callers (tests, scratch corpora) to target another database.
    url = config.attributes.get("database_url") or get_settings().database_url
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
