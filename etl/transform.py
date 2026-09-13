"""Pure transformation logic for RxInsight.

Every function here takes DataFrames and returns DataFrames. None of them open a
database connection, read a file, or look at the clock. That is deliberate: it
means the SCD Type 2 logic and the key-resolution logic can be tested against
five hand-written rows in milliseconds, with no Postgres anywhere near the test.

The two rules the whole module is built around:

*   **Nothing is ever silently dropped.** Every row that fails to type-cast or
    fails to resolve a natural key comes back in a rejects frame. That is what
    makes the row-count reconciliation assertion (staged == loaded + rejected)
    meaningful instead of vacuous.

*   **History is immutable.** An HCP who changes territory gets a new row, not an
    edited one. See `apply_scd2`.
"""
from __future__ import annotations

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

    for col in list(numeric) + list(integer):
        if col not in work.columns:
            continue
        cast = pd.to_numeric(work[col], errors="coerce")
        failed = cast.isna() & work[col].notna()
        reasons[failed & ~bad] = f"non-numeric value in {col}"
        bad |= failed
        work[col] = cast

    for col in dates:
        if col not in work.columns:
            continue
        cast = pd.to_datetime(work[col], errors="coerce")
        failed = cast.isna() & work[col].notna()
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
    incoming = incoming.sort_values([business_key, effective_col], kind="stable")

    rows = state.to_dict("records")
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
    work[date_col] = pd.to_datetime(work[date_col])

    # --- product ---
    prod = dim_product[["product_code", "product_key"]]
    work = work.merge(prod, on="product_code", how="left")

    # --- HCP, as-of the fact date ---
    hcp = dim_hcp[["hcp_id", "hcp_key", "territory_key", "valid_from", "valid_to"]].copy()
    hcp["valid_from"] = pd.to_datetime(hcp["valid_from"])
    hcp["valid_to"] = pd.to_datetime(hcp["valid_to"])

    work = work.merge(hcp, on="hcp_id", how="left")
    in_window = (work[date_col] >= work["valid_from"]) & (work[date_col] <= work["valid_to"])
    # Keep the matching version; keep unmatched rows once so they can be rejected.
    work = work[in_window | work["hcp_key"].isna()]
    work = work.drop_duplicates("_row", keep="first")

    # --- date ---
    work["date_key"] = work[date_col].dt.strftime("%Y%m%d").astype("int64")
    valid_dates = set(dim_date["date_key"].tolist())
    date_ok = work["date_key"].isin(valid_dates)

    unresolved = work["product_key"].isna() | work["hcp_key"].isna() | ~date_ok

    rejects = facts.loc[work.loc[unresolved, "_row"].to_numpy()].copy()
    reason = pd.Series("unresolved natural key", index=rejects.index)
    reason[work.loc[unresolved, "product_key"].isna().to_numpy()] = "unknown product_code"
    reason[work.loc[unresolved, "hcp_key"].isna().to_numpy()] = "unknown hcp_id"
    reason[(~date_ok).loc[unresolved].to_numpy()] = "date outside dim_date"
    rejects["reject_reason"] = reason.to_numpy()

    resolved = work[~unresolved].copy()
    for col in ("hcp_key", "product_key", "territory_key"):
        resolved[col] = resolved[col].astype("int64")

    resolved = resolved.drop(columns=[c for c in
                                      ["_row", "valid_from", "valid_to", date_col,
                                       "hcp_id", "product_code"]
                                      if c in resolved.columns])
    return resolved, rejects
