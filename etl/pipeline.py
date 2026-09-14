"""RxInsight ETL orchestrator.

    python -m etl.pipeline [--skip-schema] [--skip-generate]

Stages: schema -> COPY into staging -> build dimensions -> SCD2 the HCP
dimension -> resolve surrogate keys -> append facts -> reconcile.

The reconciliation at the end is the point of the design: every staged row is
accounted for as either loaded or rejected. A pipeline that cannot tell you
where its rows went is not a pipeline, it is a hope.
"""
from __future__ import annotations

import argparse
import sys
import time

import pandas as pd
from sqlalchemy import create_engine, text

from etl import config as C
from etl import load_staging as ls
from etl import transform as T


def _apply_sql_file(conn, path) -> None:
    with conn.cursor() as cur, open(path, "r", encoding="utf-8") as fh:
        cur.execute(fh.read())


# pandas reads go through a SQLAlchemy engine rather than the raw psycopg2
# connection: pandas only officially supports SQLAlchemy connectables and warns
# loudly otherwise. Writes still use the psycopg2 connection directly, because
# COPY is a psycopg2 feature and it is the whole reason the load is fast.
# Every read below happens after a commit, so the separate connection sees
# exactly the same committed state.
_ENGINE = None


def _engine():
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = create_engine(C.DATABASE_URL, future=True)
    return _ENGINE


def _read_staging(conn, table: str) -> pd.DataFrame:
    """Load a whole staging table. Only safe for the small ones (stg_hcp)."""
    return pd.read_sql(f"SELECT * FROM {table}", _engine())


def _iter_staging(table: str, chunk_rows: int = None):
    """Yield a staging table in batches, streamed from the server.

    `stream_results=True` makes psycopg2 open a named (server-side) cursor, so
    rows arrive in batches instead of the driver buffering the entire result set
    client-side first — without it, chunksize alone would still pull all 2.2M
    rows into memory before handing out the first batch.
    """
    chunk_rows = chunk_rows or C.CHUNK_ROWS
    with _engine().connect().execution_options(
            stream_results=True, max_row_buffer=chunk_rows) as c:
        for frame in pd.read_sql(text(f"SELECT * FROM {table}"), c, chunksize=chunk_rows):
            yield frame


def _date_bounds(conn):
    """Min and max fact date, computed in the database.

    Building dim_date used to mean pulling both fact staging tables into pandas
    purely to take a min and a max — millions of rows read to produce two values.
    """
    with conn.cursor() as cur:
        cur.execute(r"""
            SELECT min(d), max(d) FROM (
                SELECT rx_date::date   AS d FROM staging.stg_prescriptions
                 WHERE rx_date   ~ '^\d{4}-\d{2}-\d{2}$'
                UNION ALL
                SELECT call_date::date     FROM staging.stg_sales_calls
                 WHERE call_date ~ '^\d{4}-\d{2}-\d{2}$'
            ) x
        """)
        return cur.fetchone()


def _fetch(conn, sql: str) -> pd.DataFrame:
    return pd.read_sql(sql, _engine())


def run(skip_schema: bool = False, skip_generate: bool = False) -> dict:
    started = time.time()
    stats: dict[str, object] = {}

    if not skip_generate and not C.RX_CSV.exists():
        from etl import generate_data
        print("→ generating source extracts")
        generate_data.generate()

    conn = ls.connect()
    conn.autocommit = False
    try:
        if not skip_schema:
            print("→ applying sql/01_schema.sql")
            _apply_sql_file(conn, C.SQL_DIR / "01_schema.sql")
            conn.commit()

        print("→ COPY source extracts into staging")
        staged = ls.load_all_staging(conn)
        conn.commit()
        for k, v in staged.items():
            print(f"    {k:<28} {v:>9,}")

        # ---------------- dimensions ----------------
        print("→ building dimensions")
        # Only the HCP master is small enough to hold whole (a couple of thousand
        # rows). The two fact extracts are streamed in batches further down.
        stg_hcp = _read_staging(conn, "staging.stg_hcp")

        products = pd.DataFrame(C.PRODUCTS, columns=[
            "product_code", "brand_name", "molecule", "therapeutic_area", "is_competitor"])
        ls.copy_dataframe(conn, products, "warehouse.dim_product", list(products.columns))

        terr = (stg_hcp[["territory_code"]].drop_duplicates()
                .sort_values("territory_code").reset_index(drop=True))
        terr["territory_name"] = "Territory " + terr["territory_code"].str[2:]
        terr["region"] = [C.REGIONS[i % len(C.REGIONS)] for i in range(len(terr))]
        terr["country"] = "India"
        ls.copy_dataframe(conn, terr, "warehouse.dim_territory", list(terr.columns))

        lo, hi = _date_bounds(conn)
        dim_date = T.build_dim_date(lo, hi)
        ls.copy_dataframe(conn, dim_date, "warehouse.dim_date", list(dim_date.columns))
        conn.commit()

        dim_product = _fetch(conn, "SELECT product_key, product_code FROM warehouse.dim_product")
        dim_terr = _fetch(conn, "SELECT territory_key, territory_code FROM warehouse.dim_territory")

        # ---------------- dim_hcp, SCD Type 2 ----------------
        print("→ applying SCD Type 2 to dim_hcp")
        hcp_clean, hcp_rejects = T.coerce_columns(
            stg_hcp, integer=["decile"], dates=["effective_date"])
        hcp_clean = hcp_clean.merge(dim_terr, on="territory_code", how="left")
        unmapped = hcp_clean["territory_key"].isna()
        if unmapped.any():
            extra = hcp_clean[unmapped].copy()
            extra["reject_reason"] = "unknown territory_code"
            hcp_rejects = pd.concat([hcp_rejects, extra], ignore_index=True)
            hcp_clean = hcp_clean[~unmapped]
        hcp_clean["territory_key"] = hcp_clean["territory_key"].astype("int64")

        versions = T.apply_scd2(
            pd.DataFrame(),
            hcp_clean[["hcp_id", "full_name", "specialty", "decile",
                       "territory_key", "effective_date"]],
            business_key="hcp_id",
            tracked=("decile", "territory_key"),
        )
        ls.copy_dataframe(conn, versions, "warehouse.dim_hcp",
                          ["hcp_id", "full_name", "specialty", "decile",
                           "territory_key", "valid_from", "valid_to", "is_current"])
        conn.commit()

        dim_hcp = _fetch(conn, """
            SELECT hcp_key, hcp_id, territory_key, valid_from, valid_to
            FROM warehouse.dim_hcp
        """)
        stats["dim_hcp_rows"] = len(dim_hcp)
        stats["scd2_history_rows"] = int((versions["is_current"] == False).sum())  # noqa: E712

        # ---------------- facts, in bounded batches ----------------
        print(f"→ resolving surrogate keys and loading facts "
              f"(batches of {C.CHUNK_ROWS:,} rows)")

        FACT_SPECS = [
            dict(table="staging.stg_prescriptions", target="warehouse.fact_prescriptions",
                 date_col="rx_date", integer=["trx_count", "nrx_count"],
                 numeric=["units", "gross_sales"],
                 cols=["date_key", "hcp_key", "product_key", "territory_key",
                       "trx_count", "nrx_count", "units", "gross_sales"],
                 reject_file="prescriptions_rejects.csv", key="rx"),
            dict(table="staging.stg_sales_calls", target="warehouse.fact_sales_calls",
                 date_col="call_date", integer=["duration_minutes", "samples_dropped"],
                 numeric=[],
                 cols=["date_key", "hcp_key", "product_key", "territory_key",
                       "call_type", "duration_minutes", "samples_dropped"],
                 reject_file="sales_calls_rejects.csv", key="calls"),
        ]

        C.REJECTS_DIR.mkdir(parents=True, exist_ok=True)
        loaded_counts, reject_counts = {}, {}

        fact_tables = [f["target"] for f in FACT_SPECS]
        copy_started = time.time()
        with ls.constraints_suspended(conn, fact_tables) as n_dropped:
            print(f"    foreign keys suspended for the load ({n_dropped} constraints)")
            for spec in FACT_SPECS:
                loaded = rejected = 0
                rejects_out = []
                for batch in _iter_staging(spec["table"]):
                    clean, type_rej = T.coerce_columns(
                        batch, integer=spec["integer"], numeric=spec["numeric"],
                        dates=[spec["date_col"]])
                    resolved, key_rej = T.resolve_surrogate_keys(
                        clean, dim_hcp, dim_product, dim_date, date_col=spec["date_col"])
                    ls.copy_dataframe(conn, resolved, spec["target"], spec["cols"])
                    loaded += len(resolved)
                    rejected += len(type_rej) + len(key_rej)
                    for frame in (type_rej, key_rej):
                        if len(frame):
                            rejects_out.append(frame)
                    # Release the batch before the next one is fetched.
                    del batch, clean, resolved
                loaded_counts[spec["key"]] = loaded
                reject_counts[spec["key"]] = rejected
                out = (pd.concat(rejects_out, ignore_index=True) if rejects_out
                       else pd.DataFrame(columns=["reject_reason"]))
                out.to_csv(C.REJECTS_DIR / spec["reject_file"], index=False)
                print(f"    {spec['target']:<32} {loaded:>9,} loaded  {rejected:>5,} rejected")

        stats["fact_load_s"] = round(time.time() - copy_started, 1)
        print(f"    facts copied and constraints revalidated in {stats['fact_load_s']}s")
        conn.commit()

        # ---------------- reconciliation ----------------
        rx_rejects = reject_counts["rx"]
        calls_rejects = reject_counts["calls"]

        stats.update(
            staged_rx=staged["staging.stg_prescriptions"],
            loaded_rx=loaded_counts["rx"], rejected_rx=rx_rejects,
            staged_calls=staged["staging.stg_sales_calls"],
            loaded_calls=loaded_counts["calls"], rejected_calls=calls_rejects,
            elapsed_s=round(time.time() - started, 1),
        )

        print("\n  entity          staged      loaded    rejected")
        print("  " + "-" * 46)
        print(f"  prescriptions {stats['staged_rx']:>10,} {stats['loaded_rx']:>11,} {rx_rejects:>11,}")
        print(f"  sales_calls   {stats['staged_calls']:>10,} {stats['loaded_calls']:>11,} {calls_rejects:>11,}")
        print(f"  dim_hcp       {'':>10} {stats['dim_hcp_rows']:>11,} "
              f"({stats['scd2_history_rows']:,} closed history rows)")

        ok_rx = stats["staged_rx"] == stats["loaded_rx"] + rx_rejects
        ok_calls = stats["staged_calls"] == stats["loaded_calls"] + calls_rejects
        print(f"\n  reconciliation: prescriptions {'OK' if ok_rx else 'MISMATCH'}, "
              f"sales_calls {'OK' if ok_calls else 'MISMATCH'}")
        if not (ok_rx and ok_calls):
            raise SystemExit("row-count reconciliation failed — rows went missing")

        print(f"  completed in {stats['elapsed_s']}s")
        return stats
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Run the RxInsight ETL pipeline.")
    p.add_argument("--skip-schema", action="store_true",
                   help="do not drop/recreate the schema before loading")
    p.add_argument("--skip-generate", action="store_true",
                   help="do not generate source extracts even if they are missing")
    a = p.parse_args(argv)
    run(skip_schema=a.skip_schema, skip_generate=a.skip_generate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
