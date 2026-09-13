"""Bulk-load helpers built on Postgres COPY.

``pandas.to_sql`` issues parameterised INSERTs. On two million rows that is
minutes of round-trips; ``COPY FROM STDIN`` streams the same data in seconds
because the server parses one buffered stream instead of two million statements.
On a warehouse load that difference is the whole job, so COPY it is.
"""
from __future__ import annotations

import contextlib
import csv
import io

import pandas as pd
import psycopg2

from etl import config as C

STAGING_TABLES = {
    "staging.stg_hcp": ["hcp_id", "full_name", "specialty", "decile",
                        "territory_code", "effective_date"],
    "staging.stg_prescriptions": ["rx_date", "hcp_id", "product_code", "trx_count",
                                  "nrx_count", "units", "gross_sales"],
    "staging.stg_sales_calls": ["call_date", "hcp_id", "product_code", "call_type",
                                "duration_minutes", "samples_dropped"],
}


def connect():
    return psycopg2.connect(C.DATABASE_URL)


def copy_dataframe(conn, df: pd.DataFrame, table: str, columns: list[str],
                   chunk_rows: int = 250_000) -> int:
    """Stream a DataFrame into ``table`` with COPY. Returns the row count.

    Chunked rather than one giant buffer: serialising two million rows into a
    single StringIO costs hundreds of megabytes of process memory for no gain,
    and on a small machine that is the difference between a fast load and one
    that pages to disk.
    """
    if df.empty:
        return 0
    sql = f"COPY {table} ({', '.join(columns)}) FROM STDIN WITH (FORMAT csv, NULL '\\N')"
    with conn.cursor() as cur:
        for start in range(0, len(df), chunk_rows):
            buf = io.StringIO()
            df[columns].iloc[start:start + chunk_rows].to_csv(
                buf, index=False, header=False, na_rep="\\N", quoting=csv.QUOTE_MINIMAL)
            buf.seek(0)
            cur.copy_expert(sql, buf)
    return len(df)


@contextlib.contextmanager
def constraints_suspended(conn, tables: list[str]):
    """Drop the foreign keys on ``tables`` for the duration of a bulk load.

    Loading two million rows with four foreign keys enabled makes Postgres run
    one index lookup per key per row — roughly nine million of them — and the
    COPY crawls. Dropping the constraints and adding them back afterwards gets
    the same guarantee from a single validation pass per constraint (a seq scan
    plus a hash join) instead.

    The integrity guarantee is unchanged: the constraints are re-added inside the
    same transaction, and if any row violated one the ADD CONSTRAINT fails and
    the whole load rolls back. It is faster, not laxer.
    """
    quoted = ", ".join(f"'{t}'" for t in tables)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE contype = 'f' AND conrelid::regclass::text IN ({quoted})
        """)
        saved = cur.fetchall()
        for table, name, _ in saved:
            cur.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
    try:
        yield len(saved)
    finally:
        with conn.cursor() as cur:
            for table, name, definition in saved:
                cur.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} {definition}")


def copy_csv_file(conn, path, table: str, columns: list[str]) -> int:
    """Stream a CSV straight from disk into staging, skipping its header."""
    with open(path, "r", encoding="utf-8") as fh:
        header = fh.readline()  # discard
        with conn.cursor() as cur:
            cur.copy_expert(
                f"COPY {table} ({', '.join(columns)}) FROM STDIN WITH (FORMAT csv)", fh)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0]


def truncate_staging(conn) -> None:
    with conn.cursor() as cur:
        for table in STAGING_TABLES:
            cur.execute(f"TRUNCATE {table}")


def load_all_staging(conn) -> dict[str, int]:
    """Truncate staging and COPY all three source extracts into it."""
    truncate_staging(conn)
    counts = {}
    for table, (path, cols) in {
        "staging.stg_hcp": (C.HCP_CSV, STAGING_TABLES["staging.stg_hcp"]),
        "staging.stg_prescriptions": (C.RX_CSV, STAGING_TABLES["staging.stg_prescriptions"]),
        "staging.stg_sales_calls": (C.CALLS_CSV, STAGING_TABLES["staging.stg_sales_calls"]),
    }.items():
        counts[table] = copy_csv_file(conn, path, table, cols)
    return counts
