"""Independent acceptance cases for the unmodified RxInsight implementation."""

import numpy as np
import pandas as pd
import pytest
from etl import transform as T
from etl import generate_data as G


def dims():
    h = pd.DataFrame(
        [
            dict(
                hcp_id="H1",
                hcp_key=1,
                territory_key=10,
                valid_from="2024-02-01",
                valid_to="2024-04-30",
            ),
            dict(
                hcp_id="H1",
                hcp_key=2,
                territory_key=20,
                valid_from="2024-06-01",
                valid_to="9999-12-31",
            ),
        ]
    )
    return (
        h,
        pd.DataFrame([dict(product_code="P1", product_key=1)]),
        T.build_dim_date("2024-01-01", "2024-12-31"),
    )


@pytest.mark.parametrize("day", ["2024-01-15", "2024-05-15"])
def test_known_hcp_without_valid_version_is_quarantined(day):
    f = pd.DataFrame([dict(rx_date=day, hcp_id="H1", product_code="P1", trx_count=5)])
    clean, rejects = T.resolve_surrogate_keys(f, *dims())
    assert len(clean) + len(rejects) == 1, "Input row silently vanished"
    assert len(rejects) == 1


def test_fractional_integer_is_quarantined():
    clean, rejects = T.coerce_columns(
        pd.DataFrame({"trx_count": ["3.7"]}), integer=["trx_count"]
    )
    assert clean.empty and len(rejects) == 1, clean.to_dict("records")


def test_missing_integer_is_quarantined_without_crash():
    clean, rejects = T.coerce_columns(
        pd.DataFrame({"trx_count": [None]}), integer=["trx_count"]
    )
    assert clean.empty and len(rejects) == 1


def test_missing_date_is_quarantined():
    clean, rejects = T.coerce_columns(
        pd.DataFrame({"rx_date": [None]}), dates=["rx_date"]
    )
    assert clean.empty and len(rejects) == 1


def row(day, decile):
    return pd.DataFrame(
        [
            dict(
                hcp_id="H1",
                full_name="A",
                specialty="GP",
                decile=decile,
                territory_key=10,
                effective_date=day,
            )
        ]
    )


@pytest.mark.parametrize(
    "initial,incoming", [("2024-01-01", "2024-01-01"), ("2024-06-01", "2024-03-01")]
)
def test_scd_changes_never_create_negative_windows(initial, incoming):
    state = T.apply_scd2(pd.DataFrame(), row(initial, 5))
    try:
        out = T.apply_scd2(state, row(incoming, 7))
    except ValueError:
        return
    assert all(out.valid_from <= out.valid_to), out[["valid_from", "valid_to"]].to_dict(
        "records"
    )


def test_generator_lift_excludes_same_day():
    lift = G._call_lift_window(np.array([0]), np.array([0]), np.array([2]), 1, 20)
    assert lift[0, 0, 2] == 0


def test_generator_lift_includes_day_14():
    lift = G._call_lift_window(np.array([0]), np.array([0]), np.array([2]), 1, 20)
    assert lift[0, 0, 16] == 1


def test_valid_integer_values_are_preserved():
    clean, rejects = T.coerce_columns(
        pd.DataFrame({"trx_count": ["0", "3", "42"]}), integer=["trx_count"]
    )
    assert rejects.empty and clean.trx_count.tolist() == [0, 3, 42]


def test_transfer_assigns_historical_and_current_territories():
    f = pd.DataFrame(
        [
            dict(rx_date="2024-03-01", hcp_id="H1", product_code="P1", trx_count=1),
            dict(rx_date="2024-07-01", hcp_id="H1", product_code="P1", trx_count=2),
        ]
    )
    clean, rejects = T.resolve_surrogate_keys(f, *dims())
    assert rejects.empty and clean.territory_key.tolist() == [10, 20]


def test_engine_uses_installed_psycopg2_driver():
    from etl.pipeline import _engine

    assert _engine().driver == "psycopg2"


@pytest.mark.parametrize("value", ["inf", "-inf", "NaN", "", "2147483648", "-1"])
def test_invalid_prescription_count_is_rejected(value):
    clean, rejects = T.coerce_columns(
        pd.DataFrame({"trx_count": [value]}), integer=["trx_count"]
    )
    assert clean.empty and len(rejects) == 1


def test_overlapping_history_does_not_arbitrarily_assign_a_fact():
    h, p, d = dims()
    extra = h.iloc[[0]].copy()
    extra["hcp_key"] = 99
    h = pd.concat([h, extra], ignore_index=True)
    f = pd.DataFrame(
        [dict(rx_date="2024-03-01", hcp_id="H1", product_code="P1", trx_count=1)]
    )
    clean, rejects = T.resolve_surrogate_keys(f, h, p, d)
    assert clean.empty and len(rejects) == 1
    assert rejects.reject_reason.iloc[0] == "overlapping HCP versions"


def test_missing_requested_column_is_a_schema_error():
    with pytest.raises(ValueError, match="Missing source columns"):
        T.coerce_columns(pd.DataFrame({"other": ["1"]}), integer=["trx_count"])


def test_invalid_resolver_date_is_accounted_for():
    f = pd.DataFrame([dict(rx_date=None, hcp_id="H1", product_code="P1", trx_count=1)])
    clean, rejects = T.resolve_surrogate_keys(f, *dims())
    assert clean.empty and len(rejects) == 1
