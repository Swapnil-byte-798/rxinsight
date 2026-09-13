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
from sqlalchemy import create_engine

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
    return pd.read_sql(f"SELECT * FROM {table}", _engine())


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
        stg_hcp = _read_staging(conn, "staging.stg_hcp")
        stg_rx = _read_staging(conn, "staging.stg_prescriptions")
        stg_calls = _read_staging(conn, "staging.stg_sales_calls")

        products = pd.DataFrame(C.PRODUCTS, columns=[
            "product_code", "brand_name", "molecule", "therapeutic_area", "is_competitor"])
        ls.copy_dataframe(conn, products, "warehouse.dim_product", list(products.columns))

        terr = (stg_hcp[["territory_code"]].drop_duplicates()
                .sort_values("territory_code").reset_index(drop=True))
        terr["territory_name"] = "Territory " + terr["territory_code"].str[2:]
        terr["region"] = [C.REGIONS[i % len(C.REGIONS)] for i in range(len(terr))]
        terr["country"] = "India"
        ls.copy_dataframe(conn, terr, "warehouse.dim_territory", list(terr.columns))

        all_dates = pd.concat([
            pd.to_datetime(stg_rx["rx_date"], errors="coerce"),
            pd.to_datetime(stg_calls["call_date"], errors="coerce"),
        ]).dropna()
        dim_date = T.build_dim_date(all_dates.min(), all_dates.max())
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

        # ---------------- facts ----------------
        print("→ resolving surrogate keys and loading facts")
        rx_clean, rx_type_rejects = T.coerce_columns(
            stg_rx, integer=["trx_count", "nrx_count"],
            numeric=["units", "gross_sales"], dates=["rx_date"])
        rx_resolved, rx_key_rejects = T.resolve_surrogate_keys(
            rx_clean, dim_hcp, dim_product, dim_date, date_col="rx_date")

        calls_clean, calls_type_rejects = T.coerce_columns(
            stg_calls, integer=["duration_minutes", "samples_dropped"], dates=["call_date"])
        calls_resolved, calls_key_rejects = T.resolve_surrogate_keys(
            calls_clean, dim_hcp, dim_product, dim_date, date_col="call_date")

        fact_tables = ["warehouse.fact_prescriptions", "warehouse.fact_sales_calls"]
        copy_started = time.time()
        with ls.constraints_suspended(conn, fact_tables) as n_dropped:
            print(f"    foreign keys suspended for the load ({n_dropped} constraints)")
            ls.copy_dataframe(conn, rx_resolved, "warehouse.fact_prescriptions",
                              ["date_key", "hcp_key", "product_key", "territory_key",
                               "trx_count", "nrx_count", "units", "gross_sales"])
            ls.copy_dataframe(conn, calls_resolved, "warehouse.fact_sales_calls",
                              ["date_key", "hcp_key", "product_key", "territory_key",
                               "call_type", "duration_minutes", "samples_dropped"])
        stats["fact_load_s"] = round(time.time() - copy_started, 1)
        print(f"    facts copied and constraints revalidated in {stats['fact_load_s']}s")
        conn.commit()

        # ---------------- reconciliation ----------------
        rx_rejects = len(rx_type_rejects) + len(rx_key_rejects)
        calls_rejects = len(calls_type_rejects) + len(calls_key_rejects)
        C.REJECTS_DIR.mkdir(parents=True, exist_ok=True)
        pd.concat([rx_type_rejects, rx_key_rejects], ignore_index=True) \
            .to_csv(C.REJECTS_DIR / "prescriptions_rejects.csv", index=False)
        pd.concat([calls_type_rejects, calls_key_rejects], ignore_index=True) \
            .to_csv(C.REJECTS_DIR / "sales_calls_rejects.csv", index=False)

        stats.update(
            staged_rx=staged["staging.stg_prescriptions"],
            loaded_rx=len(rx_resolved), rejected_rx=rx_rejects,
            staged_calls=staged["staging.stg_sales_calls"],
            loaded_calls=len(calls_resolved), rejected_calls=calls_rejects,
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
