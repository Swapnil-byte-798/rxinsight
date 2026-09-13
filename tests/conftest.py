"""Shared fixtures.

The DB fixture *skips* rather than fails when Postgres is not running, so the
pure unit tests in test_transform.py stay runnable with no database at all —
which is the entire reason the transform logic was written as pure functions.
"""
from __future__ import annotations

import pytest

from etl import config as C


@pytest.fixture(scope="session")
def conn():
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        c = psycopg2.connect(C.DATABASE_URL, connect_timeout=3)
    except Exception as exc:                                   # pragma: no cover
        pytest.skip(f"Postgres not reachable at {C.DATABASE_URL}: {exc}")
    c.autocommit = True
    yield c
    c.close()


@pytest.fixture(scope="session")
def loaded(conn):
    """Skip warehouse assertions unless the pipeline has actually been run."""
    with conn.cursor() as cur:
        try:
            cur.execute("SELECT count(*) FROM warehouse.fact_prescriptions")
            n = cur.fetchone()[0]
        except Exception:                                      # pragma: no cover
            pytest.skip("warehouse schema not present — run `make load` first")
    if not n:
        pytest.skip("warehouse is empty — run `make load` first")
    return n
