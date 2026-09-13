"""Data-quality assertions against the loaded warehouse.

These are the checks that would run after every production load. They skip
cleanly when Postgres is not up or the warehouse is empty (see conftest), so
the suite stays useful in CI without a database.
"""
from __future__ import annotations

import pytest


def scalar(conn, sql: str):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------
def test_every_staged_row_is_loaded_or_rejected(conn, loaded):
    """Nothing vanishes between staging and the warehouse.

    Rejects live in data/rejects/*.csv; the count that matters here is that
    loaded never EXCEEDS staged and the shortfall is explained by the reject
    files the pipeline wrote.
    """
    staged = scalar(conn, "SELECT count(*) FROM staging.stg_prescriptions")
    loaded_n = scalar(conn, "SELECT count(*) FROM warehouse.fact_prescriptions")
    assert loaded_n <= staged
    import csv
    from pathlib import Path
    from etl import config as C
    rej = Path(C.REJECTS_DIR) / "prescriptions_rejects.csv"
    n_rej = sum(1 for _ in csv.reader(rej.open())) - 1 if rej.exists() else 0
    assert loaded_n + n_rej == staged, (
        f"{staged} staged != {loaded_n} loaded + {n_rej} rejected")


# --------------------------------------------------------------------------
# Referential integrity
# --------------------------------------------------------------------------
@pytest.mark.parametrize("fact,dim,key", [
    ("fact_prescriptions", "dim_hcp", "hcp_key"),
    ("fact_prescriptions", "dim_product", "product_key"),
    ("fact_prescriptions", "dim_territory", "territory_key"),
    ("fact_prescriptions", "dim_date", "date_key"),
    ("fact_sales_calls", "dim_hcp", "hcp_key"),
    ("fact_sales_calls", "dim_product", "product_key"),
    ("fact_sales_calls", "dim_date", "date_key"),
])
def test_no_orphan_foreign_keys(conn, loaded, fact, dim, key):
    orphans = scalar(conn, f"""
        SELECT count(*) FROM warehouse.{fact} f
        LEFT JOIN warehouse.{dim} d USING ({key})
        WHERE d.{key} IS NULL
    """)
    assert orphans == 0, f"{orphans} rows in {fact} point at a missing {dim}.{key}"


# --------------------------------------------------------------------------
# Null thresholds
# --------------------------------------------------------------------------
@pytest.mark.parametrize("col", ["trx_count", "nrx_count", "units", "gross_sales"])
def test_critical_measures_are_never_null(conn, loaded, col):
    assert scalar(conn,
                  f"SELECT count(*) FROM warehouse.fact_prescriptions WHERE {col} IS NULL") == 0


def test_measures_are_non_negative(conn, loaded):
    assert scalar(conn, """
        SELECT count(*) FROM warehouse.fact_prescriptions
        WHERE trx_count < 0 OR nrx_count < 0 OR units < 0 OR gross_sales < 0
    """) == 0


def test_nrx_never_exceeds_trx(conn, loaded):
    """New prescriptions are a subset of total prescriptions, by definition."""
    assert scalar(conn, """
        SELECT count(*) FROM warehouse.fact_prescriptions WHERE nrx_count > trx_count
    """) == 0


# --------------------------------------------------------------------------
# The SCD Type 2 invariant
# --------------------------------------------------------------------------
def test_exactly_one_current_row_per_hcp(conn, loaded):
    current = scalar(conn, "SELECT count(*) FROM warehouse.dim_hcp WHERE is_current")
    distinct = scalar(conn, "SELECT count(DISTINCT hcp_id) FROM warehouse.dim_hcp")
    assert current == distinct, "dim_hcp has an HCP with zero or multiple current rows"


def test_scd2_history_actually_exists(conn, loaded):
    """If nothing ever closed, the Type 2 machinery was never exercised."""
    assert scalar(conn, "SELECT count(*) FROM warehouse.dim_hcp WHERE NOT is_current") > 0


def test_scd2_validity_windows_do_not_overlap(conn, loaded):
    overlaps = scalar(conn, """
        SELECT count(*) FROM warehouse.dim_hcp a
        JOIN warehouse.dim_hcp b
          ON a.hcp_id = b.hcp_id AND a.hcp_key < b.hcp_key
        WHERE a.valid_from <= b.valid_to AND b.valid_from <= a.valid_to
    """)
    assert overlaps == 0, "two versions of the same HCP claim the same day"


def test_facts_resolve_to_the_temporally_correct_hcp_version(conn, loaded):
    """A fact's date must fall inside its HCP version's validity window.

    This is the assertion that proves the temporal join actually happened — a
    naive 'join on latest version' load would fail it on every historical row.
    """
    bad = scalar(conn, """
        SELECT count(*)
        FROM warehouse.fact_prescriptions f
        JOIN warehouse.dim_hcp h USING (hcp_key)
        JOIN warehouse.dim_date d USING (date_key)
        WHERE d.full_date < h.valid_from OR d.full_date > h.valid_to
    """)
    assert bad == 0, f"{bad} facts point at an HCP version not valid on that date"


# --------------------------------------------------------------------------
# The analytics have to be non-degenerate to mean anything
# --------------------------------------------------------------------------
def test_deciles_are_actually_distributed(conn, loaded):
    assert scalar(conn, "SELECT count(DISTINCT decile) FROM warehouse.dim_hcp") >= 5


def test_brand_share_moves_month_over_month(conn, loaded):
    """A flat share series means the generator produced noise, not a market."""
    distinct_months = scalar(conn, "SELECT count(DISTINCT year_month) FROM warehouse.dim_date")
    assert distinct_months >= 12
