"""Shared fixtures: a throwaway Postgres database per test."""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

# Admin connection used only to create/drop each test's throwaway database.
# Needs `docker compose -f db/docker-compose.yml up -d postgres` running.
ADMIN_DSN = os.environ.get("POSTGRES_TEST_DSN", "postgresql://triage:triage@localhost:5434/postgres")


def _throwaway_db() -> str:
    name = f"test_{uuid.uuid4().hex}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    return ADMIN_DSN.rsplit("/", 1)[0] + f"/{name}"


def _drop_db(dsn: str) -> None:
    name = dsn.rsplit("/", 1)[1]
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def db_dsn():
    dsn = _throwaway_db()
    yield dsn
    _drop_db(dsn)
