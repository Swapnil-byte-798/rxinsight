"""Generate synthetic source extracts for RxInsight.

Writes three CSVs that mimic what a commercial data vendor would hand a pharma
brand team: an HCP master, a prescription feed, and a sales-call log.

Two things matter here beyond volume:

1.  **The data has to have structure the analytics can find.** If prescriptions
    were uniform noise, every decile would look identical, month-over-month
    brand share would be flat, and the call-effectiveness query would correctly
    report "calls do nothing" — a working query returning a meaningless answer.
    So decile genuinely predicts volume, and a sales call genuinely lifts
    prescriptions for ~14 days afterwards.

2.  **It has to be dirty in the ways real feeds are dirty.** A handful of rows
    carry 'N/A' in numeric columns and unknown natural keys, so the reject path
    and the data-quality assertions have something real to catch.

Everything is vectorized: a Python loop over two million rows is not a
demonstration of anything except patience.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from etl import config as C

LIFT_WINDOW_DAYS = 14


def _territories(rng: np.random.Generator) -> pd.DataFrame:
    codes = [f"T-{i:03d}" for i in range(1, C.N_TERRITORIES + 1)]
    return pd.DataFrame({
        "territory_code": codes,
        "territory_name": [f"Territory {c[2:]}" for c in codes],
        "region": rng.choice(C.REGIONS, size=C.N_TERRITORIES),
        "country": "India",
    })


def _hcps(rng: np.random.Generator, territories: pd.DataFrame) -> pd.DataFrame:
    """HCP master, including the mid-period changes that exercise SCD Type 2."""
    n = C.N_HCPS
    hcp_ids = [f"HCP-{i:05d}" for i in range(1, n + 1)]

    # Decile is skewed: most prescribers are mid-value, a few are very high.
    decile = np.clip(rng.normal(5.0, 2.2, size=n).round(), 1, 10).astype(int)

    base = pd.DataFrame({
        "hcp_id": hcp_ids,
        "full_name": [f"Dr. {a} {b}"
                      for a, b in zip(rng.choice(_FIRST, n), rng.choice(_LAST, n))],
        "specialty": rng.choice(C.SPECIALTIES, size=n),
        "decile": decile,
        "territory_code": rng.choice(territories["territory_code"].to_numpy(), size=n),
        "effective_date": C.START_DATE,
    })

    # ~2% of HCPs change territory or decile partway through. These become the
    # second version of the same hcp_id, and they are the whole reason dim_hcp
    # is Type 2 rather than a plain lookup table.
    n_changed = max(1, int(n * C.SCD_CHANGE_RATE))
    changed_idx = rng.choice(n, size=n_changed, replace=False)
    changes = base.iloc[changed_idx].copy()

    change_day = rng.integers(120, 500, size=n_changed)
    changes["effective_date"] = (
        pd.Timestamp(C.START_DATE) + pd.to_timedelta(change_day, unit="D")
    ).strftime("%Y-%m-%d")

    move_territory = rng.random(n_changed) < 0.6
    changes.loc[move_territory, "territory_code"] = rng.choice(
        territories["territory_code"].to_numpy(), size=int(move_territory.sum()))
    changes.loc[~move_territory, "decile"] = np.clip(
        changes.loc[~move_territory, "decile"] + rng.choice([-2, -1, 1, 2],
                                                            size=int((~move_territory).sum())),
        1, 10)

    out = pd.concat([base, changes], ignore_index=True)
    return out.sort_values(["hcp_id", "effective_date"]).reset_index(drop=True)


def _sales_calls(rng, hcp_ids, own_products, dates) -> pd.DataFrame:
    """Rep visits. Only own brands get detailed — nobody details a competitor."""
    n_days = len(dates)
    # Roughly 2 calls per HCP per month across the own-brand portfolio.
    n_calls = int(C.N_HCPS * C.N_MONTHS * 2.0)

    hcp_idx = rng.integers(0, C.N_HCPS, size=n_calls)
    prod_idx = rng.integers(0, len(own_products), size=n_calls)
    # Calls land on weekdays; reps do not work Sundays.
    day_idx = rng.integers(0, n_days, size=n_calls)
    weekday_ok = dates[day_idx].dayofweek < 5
    hcp_idx, prod_idx, day_idx = hcp_idx[weekday_ok], prod_idx[weekday_ok], day_idx[weekday_ok]

    df = pd.DataFrame({
        "call_date": dates[day_idx].strftime("%Y-%m-%d"),
        "hcp_id": np.asarray(hcp_ids)[hcp_idx],
        "product_code": np.asarray(own_products)[prod_idx],
        "call_type": rng.choice(C.CALL_TYPES, size=len(day_idx), p=[0.55, 0.25, 0.20]),
        "duration_minutes": rng.integers(5, 41, size=len(day_idx)),
        "samples_dropped": rng.integers(0, 15, size=len(day_idx)),
    })
    df.attrs["_idx"] = (hcp_idx, prod_idx, day_idx)
    return df


def _call_lift_window(hcp_idx, prod_idx, day_idx, n_prod, n_days):
    """For every (hcp, own-product, day), how many calls happened in the prior 14 days.

    Built with a cumulative-sum difference rather than a loop over calls: the
    running total at day d minus the running total at day d-14 is exactly the
    count inside the window, in one pass.
    """
    counts = np.zeros((C.N_HCPS, n_prod, n_days), dtype=np.int16)
    np.add.at(counts, (hcp_idx, prod_idx, day_idx), 1)
    csum = np.cumsum(counts, axis=2, dtype=np.int32)
    shifted = np.zeros_like(csum)
    shifted[:, :, LIFT_WINDOW_DAYS:] = csum[:, :, :-LIFT_WINDOW_DAYS]
    return (csum - shifted).astype(np.int16)


def generate() -> None:
    rng = np.random.default_rng(C.RANDOM_SEED)
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)

    dates = pd.date_range(C.START_DATE, periods=C.N_MONTHS * 30 + 10, freq="D")
    n_days = len(dates)

    territories = _territories(rng)
    hcp = _hcps(rng, territories)
    hcp.to_csv(C.HCP_CSV, index=False)

    products = pd.DataFrame(C.PRODUCTS, columns=[
        "product_code", "brand_name", "molecule", "therapeutic_area", "is_competitor"])
    own_products = products.loc[~products["is_competitor"], "product_code"].tolist()
    hcp_ids = [f"HCP-{i:05d}" for i in range(1, C.N_HCPS + 1)]

    calls = _sales_calls(rng, hcp_ids, own_products, dates)
    c_hcp, c_prod, c_day = calls.attrs["_idx"]
    calls.drop(columns=[]).to_csv(C.CALLS_CSV, index=False)

    lift = _call_lift_window(c_hcp, c_prod, c_day, len(own_products), n_days)

    # ---- prescriptions -----------------------------------------------------
    # Sampling (hcp, product, day) triples with weights and then aggregating to
    # the grain gives both a realistic row distribution AND realistic trx counts
    # from one draw: a high-decile prescriber is picked more often, so they end
    # up with both more rows and higher counts per row.
    draws = int(C.TARGET_RX_ROWS * 1.35)

    hcp_w = hcp.drop_duplicates("hcp_id").set_index("hcp_id").loc[hcp_ids, "decile"].to_numpy()
    hcp_w = (hcp_w ** 1.8).astype(float)
    hcp_w /= hcp_w.sum()

    prod_w = np.where(products["is_competitor"].to_numpy(), 1.0, 1.6)
    prod_w /= prod_w.sum()

    # Seasonality plus a mild upward drift, so month-over-month share moves.
    t = np.arange(n_days)
    day_w = 1.0 + 0.18 * np.sin(2 * np.pi * t / 365.0) + 0.0004 * t
    day_w[dates.dayofweek >= 5] *= 0.35          # weekends are quiet
    day_w = np.clip(day_w, 0.05, None)
    day_w /= day_w.sum()

    d_hcp = rng.choice(C.N_HCPS, size=draws, p=hcp_w)
    d_prod = rng.choice(len(products), size=draws, p=prod_w)
    d_day = rng.choice(n_days, size=draws, p=day_w)

    rx = pd.DataFrame({"h": d_hcp, "p": d_prod, "d": d_day})
    rx = rx.groupby(["h", "p", "d"], sort=False).size().reset_index(name="trx_count")

    # Apply the post-call lift to own-brand rows only.
    own_pos = {code: i for i, code in enumerate(own_products)}
    prod_codes = products["product_code"].to_numpy()
    is_own = ~products["is_competitor"].to_numpy()
    own_mask = is_own[rx["p"].to_numpy()]
    if own_mask.any():
        own_prod_slot = np.array([own_pos.get(prod_codes[p], 0) for p in rx.loc[own_mask, "p"]])
        recent_calls = lift[rx.loc[own_mask, "h"].to_numpy(), own_prod_slot,
                            rx.loc[own_mask, "d"].to_numpy()]
        rx.loc[own_mask, "trx_count"] += (recent_calls > 0) * rng.integers(
            1, 4, size=int(own_mask.sum()))

    n = len(rx)
    trx = rx["trx_count"].to_numpy()
    out = pd.DataFrame({
        "rx_date": dates[rx["d"].to_numpy()].strftime("%Y-%m-%d"),
        "hcp_id": np.asarray(hcp_ids)[rx["h"].to_numpy()],
        "product_code": prod_codes[rx["p"].to_numpy()],
        "trx_count": trx,
        "nrx_count": rng.binomial(trx, 0.32),
        "units": (trx * rng.normal(30, 4, size=n)).round(2),
        "gross_sales": (trx * rng.normal(30, 4, size=n) * rng.uniform(8, 22, size=n)).round(2),
    })
    out["units"] = out["units"].clip(lower=0.01)
    out["gross_sales"] = out["gross_sales"].clip(lower=0.01)

    # ---- deliberate dirt ---------------------------------------------------
    dirty = rng.choice(len(out), size=min(C.DIRTY_ROW_COUNT, len(out)), replace=False)
    half = len(dirty) // 2
    # pandas 3.0 refuses to silently upcast an int64 column to hold a string, so
    # widen these two to object first. That mirrors reality anyway: the columns
    # are TEXT the moment they leave here, because a CSV has no types.
    out["trx_count"] = out["trx_count"].astype(object)
    out["product_code"] = out["product_code"].astype(object)
    out.loc[out.index[dirty[:half]], "trx_count"] = "N/A"           # uncastable numeric
    out.loc[out.index[dirty[half:]], "product_code"] = "UNKNOWN-99"  # unresolvable natural key

    out.to_csv(C.RX_CSV, index=False)

    print(f"  hcp.csv           {len(hcp):>9,} rows  ({hcp['hcp_id'].nunique():,} distinct HCPs)")
    print(f"  sales_calls.csv   {len(calls):>9,} rows")
    print(f"  prescriptions.csv {len(out):>9,} rows  ({len(dirty)} deliberately malformed)")


_FIRST = np.array(["Anil", "Priya", "Rohan", "Meera", "Vikram", "Sunita", "Arjun",
                   "Kavita", "Rajesh", "Neha", "Sanjay", "Divya", "Amit", "Pooja"])
_LAST = np.array(["Sharma", "Iyer", "Patel", "Reddy", "Nair", "Gupta", "Desai",
                  "Rao", "Menon", "Kulkarni", "Bose", "Chopra", "Verma", "Joshi"])


if __name__ == "__main__":
    print("Generating synthetic source extracts...")
    generate()
