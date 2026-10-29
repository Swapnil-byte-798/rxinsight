"""Shared fixtures.

The DB fixture *skips* rather than fails when Postgres is not running, so the
pure unit tests in test_transform.py stay runnable with no database at all —
which is the entire reason the transform logic was written as pure functions.
"""
from __future__ import annotations

import os
import pytest

from etl import config as C


def pytest_addoption(parser):
    parser.addoption("--require-db", action="store_true",
                     help="fail instead of skipping unavailable/empty warehouse checks")


@pytest.fixture(scope="session")
def conn(request):
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        c = psycopg2.connect(C.DATABASE_URL, connect_timeout=int(
            os.environ.get("RXINSIGHT_TEST_CONNECT_TIMEOUT", "3")))
    except Exception as exc:                                   # pragma: no cover
        message = f"PostgreSQL unavailable ({type(exc).__name__}); check DATABASE_URL"
        if request.config.getoption("--require-db"):
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    c.autocommit = True
    yield c
    c.close()


@pytest.fixture(scope="session")
def loaded(conn, request):
    """Skip warehouse assertions unless the pipeline has actually been run."""
    with conn.cursor() as cur:
        try:
            cur.execute("SELECT count(*) FROM warehouse.fact_prescriptions")
            n = cur.fetchone()[0]
        except Exception:                                      # pragma: no cover
            if request.config.getoption("--require-db"):
                pytest.fail("Warehouse schema not present; run make load first")
            pytest.skip("warehouse schema not present — run `make load` first")
    if not n:
        if request.config.getoption("--require-db"):
            pytest.fail("Warehouse is empty; run make load first")
        pytest.skip("warehouse is empty — run `make load` first")
    return n
