"""Measure what indexing actually does to this warehouse.

Method matters more than the headline here. Two things bias a naive
before/after benchmark badly enough to invert the result:

*   **Cold cache.** The first execution of a query pulls pages from disk. If you
    measure "before" cold and "after" warm, you will report a speedup you did not
    earn. Every measurement below discards a warm-up run and takes the median of
    three.

*   **Assuming the index helps.** It frequently does not. An aggregate over the
    whole fact table has no selective predicate, so a sequential scan is the
    correct plan and an index can only make things worse by tempting the planner
    into a nested loop. That result is reported here as found, not hidden.

Run with:  python -m etl.measure
"""
from __future__ import annotations

import re
import statistics

from etl import config as C
from etl import load_staging as ls

RUNS = 3

FULL_AGGREGATE = """
WITH monthly AS (
    SELECT d.year_month, p.brand_name, SUM(f.trx_count) AS trx
    FROM warehouse.fact_prescriptions f
    JOIN warehouse.dim_date d    USING (date_key)
    JOIN warehouse.dim_product p USING (product_key)
    GROUP BY d.year_month, p.brand_name
)
SELECT * FROM monthly
"""

SELECTIVE_DRILLDOWN = """
SELECT d.year_month, SUM(f.trx_count)
FROM warehouse.fact_prescriptions f
JOIN warehouse.dim_date d USING (date_key)
WHERE f.hcp_key = 742
GROUP BY d.year_month
"""

SCAN_RE = re.compile(
    r"(Parallel Seq Scan|Seq Scan|Index Only Scan|Index Scan|Bitmap Index Scan|"
    r"Bitmap Heap Scan) on (\w+)")


def _run(conn, sql: str):
    """Median execution time over RUNS, after a discarded warm-up."""
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (ANALYZE) " + sql)          # warm-up, discarded
        cur.fetchall()
        times, plan = [], ""
        for _ in range(RUNS):
            cur.execute("EXPLAIN (ANALYZE) " + sql)
            plan = "\n".join(r[0] for r in cur.fetchall())
            m = re.search(r"Execution Time: ([\d.]+) ms", plan)
            if m:
                times.append(float(m.group(1)))
    nodes = [f"{kind} on {tbl}" for kind, tbl in SCAN_RE.findall(plan)
             if tbl.startswith("fact")]
    return statistics.median(times), (nodes[0] if nodes else "n/a")


def _set_indexes(conn, present: bool, names: list[str]):
    with conn.cursor() as cur:
        for n in names:
            cur.execute(f"DROP INDEX IF EXISTS warehouse.{n}")
        if present:
            cur.execute(C.SQL_DIR.joinpath("02_indexes.sql").read_text(encoding="utf-8"))
        cur.execute("ANALYZE warehouse.fact_prescriptions")


ALL_INDEXES = ["idx_rx_date_product", "idx_rx_hcp", "idx_rx_territory", "idx_calls_hcp_date"]


def main() -> dict:
    conn = ls.connect()
    conn.autocommit = True
    try:
        results = {}
        for label, sql in (("full_aggregate", FULL_AGGREGATE),
                           ("selective_drilldown", SELECTIVE_DRILLDOWN)):
            _set_indexes(conn, False, ALL_INDEXES)
            before_ms, before_plan = _run(conn, sql)
            _set_indexes(conn, True, ALL_INDEXES)
            after_ms, after_plan = _run(conn, sql)
            results[label] = dict(before_ms=before_ms, after_ms=after_ms,
                                  before_plan=before_plan, after_plan=after_plan,
                                  ratio=before_ms / after_ms if after_ms else float("nan"))

        print(f"\n  {'query':<22}{'no index':>12}{'indexed':>12}{'change':>11}   plan after")
        print("  " + "-" * 78)
        for label, r in results.items():
            verdict = (f"{r['ratio']:.0f}x faster" if r["ratio"] >= 1.15 else
                       f"{1/r['ratio']:.1f}x SLOWER" if r["ratio"] <= 0.87 else "no change")
            print(f"  {label:<22}{r['before_ms']:>10.1f}ms{r['after_ms']:>10.1f}ms"
                  f"{verdict:>13}   {r['after_plan']}")
        print()
        print("  Read that second row honestly: the composite index does not help an")
        print("  aggregate with no selective predicate, and can lose to a parallel seq")
        print("  scan. An index earns its keep on selectivity, not on table size.\n")
        return results
    finally:
        conn.close()


if __name__ == "__main__":
    main()
