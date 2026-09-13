"""Unit tests for the pure transformation logic. No database required."""
from __future__ import annotations

import pandas as pd
import pytest

from etl.transform import (OPEN_ENDED, apply_scd2, build_dim_date,
                           coerce_columns, resolve_surrogate_keys)


# --------------------------------------------------------------------------
# dim_date
# --------------------------------------------------------------------------
def test_build_dim_date_row_count_and_keys():
    d = build_dim_date("2024-01-01", "2024-01-31")
    assert len(d) == 31
    assert d["date_key"].iloc[0] == 20240101
    assert d["date_key"].iloc[-1] == 20240131
    assert d["year_month"].unique().tolist() == ["2024-01"]


def test_build_dim_date_spans_leap_day():
    d = build_dim_date("2024-02-27", "2024-03-02")
    assert 20240229 in set(d["date_key"])       # 2024 is a leap year


# --------------------------------------------------------------------------
# SCD Type 2
# --------------------------------------------------------------------------
def _incoming(**kw):
    base = {"hcp_id": "HCP-1", "full_name": "Dr. A", "specialty": "Cardiologist",
            "decile": 5, "territory_key": 10, "effective_date": "2024-01-01"}
    base.update(kw)
    return pd.DataFrame([base])


def test_scd2_first_load_creates_one_current_row():
    out = apply_scd2(pd.DataFrame(), _incoming())
    assert len(out) == 1
    assert out["is_current"].tolist() == [True]
    assert out["valid_to"].iloc[0] == OPEN_ENDED


def test_scd2_decile_change_closes_old_and_opens_new():
    state = apply_scd2(pd.DataFrame(), _incoming())
    state = apply_scd2(state, _incoming(decile=8, effective_date="2024-06-01"))

    assert len(state) == 2, "a tracked change must add a version, not overwrite"
    closed = state[~state["is_current"]].iloc[0]
    live = state[state["is_current"]].iloc[0]

    assert closed["decile"] == 5
    assert closed["valid_to"] == pd.Timestamp("2024-05-31"), "old row ends the day before"
    assert live["decile"] == 8
    assert live["valid_from"] == pd.Timestamp("2024-06-01")
    assert live["valid_to"] == OPEN_ENDED


def test_scd2_territory_change_is_tracked():
    state = apply_scd2(pd.DataFrame(), _incoming())
    state = apply_scd2(state, _incoming(territory_key=99, effective_date="2024-04-15"))
    assert len(state) == 2
    assert state[state["is_current"]]["territory_key"].iloc[0] == 99


def test_scd2_untracked_change_is_a_noop():
    """full_name is not tracked, so a rename must not create a new version."""
    state = apply_scd2(pd.DataFrame(), _incoming())
    state = apply_scd2(state, _incoming(full_name="Dr. A. Renamed",
                                        effective_date="2024-06-01"))
    assert len(state) == 1
    assert state["is_current"].tolist() == [True]


def test_scd2_identical_reload_is_a_noop():
    state = apply_scd2(pd.DataFrame(), _incoming())
    state = apply_scd2(state, _incoming())
    assert len(state) == 1


def test_scd2_invariant_one_current_row_per_key():
    inc = pd.concat([
        _incoming(),
        _incoming(decile=7, effective_date="2024-03-01"),
        _incoming(decile=9, effective_date="2024-09-01"),
        _incoming(hcp_id="HCP-2"),
    ], ignore_index=True)
    state = apply_scd2(pd.DataFrame(), inc)
    current = state[state["is_current"]]
    assert len(current) == current["hcp_id"].nunique() == 2
    assert len(state) == 4


# --------------------------------------------------------------------------
# type coercion / quarantine
# --------------------------------------------------------------------------
def test_coerce_quarantines_non_numeric_without_raising():
    df = pd.DataFrame({"trx_count": ["3", "N/A", "7"]})
    clean, rejects = coerce_columns(df, integer=["trx_count"])
    assert len(clean) == 2 and len(rejects) == 1
    assert rejects["reject_reason"].iloc[0] == "non-numeric value in trx_count"
    assert clean["trx_count"].tolist() == [3, 7]


def test_coerce_accounts_for_every_row():
    df = pd.DataFrame({"trx_count": ["1", "x", "3", "y"]})
    clean, rejects = coerce_columns(df, integer=["trx_count"])
    assert len(clean) + len(rejects) == len(df)


# --------------------------------------------------------------------------
# surrogate key resolution
# --------------------------------------------------------------------------
@pytest.fixture
def dims():
    dim_hcp = pd.DataFrame([
        # Two versions of HCP-1: territory 10 until May, territory 20 after.
        {"hcp_id": "HCP-1", "hcp_key": 1, "territory_key": 10,
         "valid_from": "2024-01-01", "valid_to": "2024-05-31"},
        {"hcp_id": "HCP-1", "hcp_key": 2, "territory_key": 20,
         "valid_from": "2024-06-01", "valid_to": "9999-12-31"},
    ])
    dim_product = pd.DataFrame([{"product_code": "RX-1", "product_key": 7}])
    dim_date = build_dim_date("2024-01-01", "2024-12-31")
    return dim_hcp, dim_product, dim_date


def test_resolve_picks_the_version_current_on_the_fact_date(dims):
    dim_hcp, dim_product, dim_date = dims
    facts = pd.DataFrame([
        {"rx_date": "2024-03-04", "hcp_id": "HCP-1", "product_code": "RX-1", "trx_count": 5},
        {"rx_date": "2024-08-04", "hcp_id": "HCP-1", "product_code": "RX-1", "trx_count": 9},
    ])
    resolved, rejects = resolve_surrogate_keys(facts, dim_hcp, dim_product, dim_date)
    assert rejects.empty
    assert resolved.sort_values("date_key")["hcp_key"].tolist() == [1, 2]
    # territory follows the version, which is the whole point of SCD2
    assert resolved.sort_values("date_key")["territory_key"].tolist() == [10, 20]


def test_resolve_sends_unknown_product_to_rejects(dims):
    dim_hcp, dim_product, dim_date = dims
    facts = pd.DataFrame([
        {"rx_date": "2024-03-04", "hcp_id": "HCP-1", "product_code": "NOPE", "trx_count": 1}])
    resolved, rejects = resolve_surrogate_keys(facts, dim_hcp, dim_product, dim_date)
    assert resolved.empty and len(rejects) == 1
    assert rejects["reject_reason"].iloc[0] == "unknown product_code"


def test_resolve_sends_unknown_hcp_to_rejects(dims):
    dim_hcp, dim_product, dim_date = dims
    facts = pd.DataFrame([
        {"rx_date": "2024-03-04", "hcp_id": "GHOST", "product_code": "RX-1", "trx_count": 1}])
    resolved, rejects = resolve_surrogate_keys(facts, dim_hcp, dim_product, dim_date)
    assert resolved.empty and len(rejects) == 1
    assert rejects["reject_reason"].iloc[0] == "unknown hcp_id"


def test_resolve_never_loses_a_row(dims):
    """The reconciliation guarantee: loaded + rejected == input, always."""
    dim_hcp, dim_product, dim_date = dims
    facts = pd.DataFrame([
        {"rx_date": "2024-03-04", "hcp_id": "HCP-1", "product_code": "RX-1", "trx_count": 1},
        {"rx_date": "2024-03-05", "hcp_id": "GHOST", "product_code": "RX-1", "trx_count": 2},
        {"rx_date": "2024-07-05", "hcp_id": "HCP-1", "product_code": "NOPE", "trx_count": 3},
    ])
    resolved, rejects = resolve_surrogate_keys(facts, dim_hcp, dim_product, dim_date)
    assert len(resolved) + len(rejects) == len(facts)
