"""Pure transformation logic for RxInsight.

Every function here takes DataFrames and returns DataFrames. None of them open a
database connection, read a file, or look at the clock. That is deliberate: it
means the SCD Type 2 logic and the key-resolution logic can be tested against a
handful of hand-written rows in milliseconds, with no Postgres near the test.

The two rules the whole module is built around:

*   **Nothing is ever silently dropped.** Every row that fails to type-cast or
    fails to resolve a natural key comes back in a rejects frame. That is what
    makes the row-count reconciliation assertion (staged == loaded + rejected)
    meaningful instead of vacuous.

*   **History is immutable.** An HCP who changes territory gets a new row, not an
    edited one. See `apply_scd2`.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

OPEN_ENDED = pd.Timestamp("9999-12-31")


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------
def build_dim_date(start, end) -> pd.DataFrame:
    """One row per calendar day in [start, end], keyed YYYYMMDD.

    An integer date_key rather than a date: it is compact in the fact tables,
    sorts naturally, and keeps every calendar concern (month names, fiscal
    grouping) in one dimension instead of scattered across queries.
    """
    days = pd.date_range(start, end, freq="D")
    return pd.DataFrame({
        "date_key": days.strftime("%Y%m%d").astype(int),
        "full_date": days,
        "year": days.year.astype("int16"),
        "quarter": days.quarter.astype("int16"),
        "month": days.month.astype("int16"),
        "month_name": days.strftime("%B"),
        "year_month": days.strftime("%Y-%m"),
    })


# ---------------------------------------------------------------------------
# Typing / quarantine
# ---------------------------------------------------------------------------
def coerce_columns(df: pd.DataFrame, numeric=(), integer=(), dates=()):
    """Cast all-TEXT staging columns to real types, quarantining what won't cast.

    Staging is TEXT on purpose, so this is where a feed's ``'N/A'`` in a numeric
    column gets caught. It returns (clean, rejects) rather than raising, because
    one malformed row should cost you that row, not the entire load.
    """
    work = df.copy()
    bad = pd.Series(False, index=work.index)
    reasons = pd.Series("", index=work.index)

    required = set(numeric) | set(integer) | set(dates)
    if required - set(work.columns):
        raise ValueError(f"Missing source columns: {sorted(required - set(work.columns))}")

    for col in list(numeric) + list(integer):
        cast = pd.to_numeric(work[col], errors="coerce")
        missing = work[col].isna() | work[col].astype(str).str.strip().eq("")
        failed = cast.isna() | ~np.isfinite(cast)
        reasons[failed & ~bad] = f"non-numeric value in {col}"
        reasons[missing & ~bad] = f"missing value in {col}"
        bad |= failed
        if col in integer:
            fractional = cast.notna() & np.isfinite(cast) & cast.mod(1).ne(0)
            reasons[fractional & ~bad] = f"non-integer value in {col}"
            bad |= fractional
            # Use the destination's integer bounds where known. Never let an
            # overflowing or fractional number reach astype/COPY.
            bounds = {"decile": (1, 10), "trx_count": (0, 2**31 - 1),
                      "nrx_count": (0, 2**31 - 1),
                      "duration_minutes": (0, 2**15 - 1),
                      "samples_dropped": (0, 2**15 - 1)}
            lo, hi = bounds.get(col, (-2**63, 2**63 - 1))
            outside = cast.lt(lo) | cast.gt(hi)
            reasons[outside & ~bad] = f"out-of-range value in {col}"
            bad |= outside
        work[col] = cast

    for col in dates:
        cast = pd.to_datetime(work[col], format="%Y-%m-%d", errors="coerce")
        failed = cast.isna()
        reasons[failed & ~bad] = f"unparseable date in {col}"
        bad |= failed
        work[col] = cast

    rejects = df[bad].copy()
    rejects["reject_reason"] = reasons[bad]

    clean = work[~bad].copy()
    for col in integer:
        if col in clean.columns:
            clean[col] = clean[col].astype("int64")
    return clean, rejects


# ---------------------------------------------------------------------------
# Slowly Changing Dimension, Type 2
# ---------------------------------------------------------------------------
def apply_scd2(existing_dim: pd.DataFrame,
               incoming: pd.DataFrame,
               business_key: str = "hcp_id",
               tracked=("decile", "territory_key"),
               effective_col: str = "effective_date") -> pd.DataFrame:
    """Fold new versions into a Type 2 dimension.

    Why Type 2 and not just an UPDATE: if Dr. Sharma moves from territory T-12 to
    T-19 in June and we overwrite her row, every prescription she wrote in
    January retroactively belongs to T-19. The rep who actually covered T-12 in
    January loses credit for work they did, and last year's territory numbers
    silently change every time somebody transfers. Type 2 closes the old row and
    opens a new one, so a January fact keeps pointing at the version that was
    current in January.

    For each incoming row:
      * no current version exists      -> insert it as the current row
      * a tracked attribute changed    -> close the current row
                                          (valid_to = effective_date - 1 day,
                                          is_current = False) and insert a new one
      * nothing tracked changed        -> no-op

    Returns the full new state of the dimension. The invariant that must always
    hold on the way out: exactly one ``is_current`` row per business key.
    """
    tracked = list(tracked)
    cols = [business_key, *[c for c in incoming.columns if c != effective_col]]
    cols = list(dict.fromkeys(cols))

    if existing_dim is None or existing_dim.empty:
        state = pd.DataFrame(columns=[*cols, "valid_from", "valid_to", "is_current"])
    else:
        state = existing_dim.copy()

    incoming = incoming.copy()
    incoming[effective_col] = pd.to_datetime(incoming[effective_col])
    if incoming[effective_col].isna().any():
        raise ValueError("SCD effective dates must be present")
    incoming = incoming.sort_values([business_key, effective_col], kind="stable")

    rows = state.to_dict("records")
    for row in rows:
        if row["valid_from"] > row["valid_to"]:
            raise ValueError("Existing SCD history has an inverted validity window")
    # Index of the live row per business key, so each change is O(1) to find.
    current_at = {r[business_key]: i for i, r in enumerate(rows) if r.get("is_current")}

    for rec in incoming.to_dict("records"):
        key = rec[business_key]
        eff = rec[effective_col]
        idx = current_at.get(key)

        if idx is None:
            new = {c: rec.get(c) for c in cols}
            new.update(valid_from=eff, valid_to=OPEN_ENDED, is_current=True)
            rows.append(new)
            current_at[key] = len(rows) - 1
            continue

        live = rows[idx]
        if all(live.get(t) == rec.get(t) for t in tracked):
            continue                                  # nothing tracked changed

        # Daily history is immutable. Ambiguous same-day corrections and
        # backdated changes need an explicit backfill, not a negative interval.
        if eff <= pd.Timestamp(live["valid_from"]):
            raise ValueError(f"SCD change for {key} must be after its current valid_from")

        live["valid_to"] = eff - pd.Timedelta(days=1)
        live["is_current"] = False

        new = {c: rec.get(c) for c in cols}
        new.update(valid_from=eff, valid_to=OPEN_ENDED, is_current=True)
        rows.append(new)
        current_at[key] = len(rows) - 1

    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=[*cols, "valid_from", "valid_to", "is_current"])
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Natural keys -> surrogate keys
# ---------------------------------------------------------------------------
def resolve_surrogate_keys(facts: pd.DataFrame,
                           dim_hcp: pd.DataFrame,
                           dim_product: pd.DataFrame,
                           dim_date: pd.DataFrame,
                           date_col: str = "rx_date") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Swap natural keys for surrogate keys, quarantining anything unresolvable.

    The HCP lookup is **temporal**, and that is the whole point of the Type 2
    dimension: a fact dated 2024-03-04 must resolve to the ``hcp_key`` that was
    current on 2024-03-04, not to whichever version happens to be live now.
    ``territory_key`` is then taken from that matched version, so a territory
    rollup stays correct across a rep transfer without walking version history at
    query time.

    Returns (resolved, rejects). Every input row appears in exactly one of them.
    """
    work = facts.copy()
    work["_row"] = range(len(work))
    work[date_col] = pd.to_datetime(work[date_col], format="%Y-%m-%d", errors="coerce")

    # --- product ---
    prod = dim_product[["product_code", "product_key"]]
    work = work.merge(prod, on="product_code", how="left", validate="many_to_one")

    # --- HCP, as-of the fact date ---
    hcp = dim_hcp[["hcp_id", "hcp_key", "territory_key", "valid_from", "valid_to"]].copy()
    # Python dates preserve the year-9999 sentinel on pandas 2.x as well as 3.x.
    # A vector to_datetime on pandas 2.x forces nanoseconds and overflows.
    for col in ("valid_from", "valid_to"):
        hcp[col] = hcp[col].map(lambda value: pd.Timestamp(value).date())

    candidates = work.merge(hcp, on="hcp_id", how="left")
    comparable = candidates[date_col].notna() & candidates["hcp_key"].notna()
    in_window = pd.Series(False, index=candidates.index)
    fact_days = candidates.loc[comparable, date_col].dt.date
    in_window.loc[comparable] = (
        (fact_days >= candidates.loc[comparable, "valid_from"])
        & (fact_days <= candidates.loc[comparable, "valid_to"]))
    matches = candidates.loc[in_window, ["_row", "hcp_key", "territory_key"]]
    ambiguous = set(matches.loc[matches.duplicated("_row", keep=False), "_row"])
    matches = matches.drop_duplicates("_row", keep=False)
    # Join matches BACK to every input position: known HCPs with no valid
    # history version must survive long enough to enter the reject path.
    work = work.merge(matches, on="_row", how="left", validate="one_to_one")

    # --- date ---
    work["date_key"] = pd.to_numeric(work[date_col].dt.strftime("%Y%m%d"), errors="coerce").astype("Int64")
    valid_dates = set(dim_date["date_key"].tolist())
    date_ok = work["date_key"].isin(valid_dates)

    unresolved = work["product_key"].isna() | work["hcp_key"].isna() | ~date_ok

    # `_row` holds POSITIONS into `facts`, so select positionally with .iloc.
    #
    # This was a real bug: .loc is label-based, and by the time the pipeline calls
    # this function coerce_columns has already dropped the type-reject rows, so the
    # index has gaps and label != position. Every label past the first gap resolved
    # to the wrong row — clean rows were quarantined while the genuinely malformed
    # ones were loaded. The row COUNTS still reconciled, which is exactly why the
    # reconciliation assertion did not catch it. test_rejects_survive_a_gapped_index
    # pins the behaviour.
    unres = work.loc[unresolved]
    rejects = facts.iloc[unres["_row"].to_numpy()].copy()
    rejects["reject_reason"] = np.select(
        [unres[date_col].isna().to_numpy(), unres["product_key"].isna().to_numpy(),
         (~unres["hcp_id"].isin(dim_hcp["hcp_id"])).to_numpy(),
         unres["_row"].isin(ambiguous).to_numpy(), unres["hcp_key"].isna().to_numpy()],
        [f"missing or invalid {date_col}", "unknown product_code", "unknown hcp_id",
         "overlapping HCP versions", "no valid HCP version"],
        default="date outside dim_date")

    resolved = work[~unresolved].copy()
    for col in ("date_key", "hcp_key", "product_key", "territory_key"):
        resolved[col] = resolved[col].astype("int64")

    resolved = resolved.drop(columns=[c for c in
                                      ["_row", "valid_from", "valid_to", date_col,
                                       "hcp_id", "product_code"]
                                      if c in resolved.columns])
    if len(resolved) + len(rejects) != len(facts):
        raise RuntimeError("Key resolution failed input-row reconciliation")
    return resolved, rejects
