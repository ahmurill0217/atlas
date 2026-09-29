"""Create and migrate auxiliary databases (tests, consistency evaluation)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]

GRAPH_TABLES = ["resolution_log", "relationship_evidence", "entity_mentions", "relationships",
                "entity_aliases", "entities", "chunk_extractions", "extraction_runs"]
SOURCE_TABLES = ["chunks", "documents"]


def sibling_database_url(url: str, suffix: str) -> str:
    """postgresql://.../brain -> postgresql://.../brain_<suffix>"""
    parsed = make_url(url)
    return parsed.set(database=f"{parsed.database}_{suffix}").render_as_string(hide_password=False)


def ensure_database(url: str) -> None:
    parsed = make_url(url)
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"),
                              {"d": parsed.database}).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    admin.dispose()


def migrate(url: str) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["database_url"] = url
    command.upgrade(cfg, "head")


def truncate(engine, tables: list[str]) -> None:
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(tables)} CASCADE"))
