"""Shared fixtures. DB tests run against a separate `<db>_test` database that is
created and migrated automatically; tests/v1/conftest.py truncates it per test."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from atlas.config import get_settings
from atlas.db.admin import ensure_database, migrate, sibling_database_url


def _test_url() -> str:
    return sibling_database_url(get_settings().database_url, "test")


@pytest.fixture(scope="session")
def engine():
    try:
        ensure_database(_test_url())
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available ({exc}); run `docker compose up -d`")
    migrate(_test_url())
    eng = create_engine(_test_url())
    yield eng
    eng.dispose()
