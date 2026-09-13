# RxInsight

**A pharmaceutical commercial analytics warehouse.** PostgreSQL star schema, Python ETL
with Slowly Changing Dimensions, and window-function SQL over ~2.2 million prescription
rows.

Every number on this page was measured on the machine that built it. Where a change did
*not* help, it says so.

---

## The business question

A brand team's dashboard tells you how many prescriptions were written this month. That is
rarely the question anyone actually has. The questions are:

> *Which prescribers are actually driving our brand?*
> *Which territories are behind plan, and by how much?*
> *Do our sales calls move prescriptions, or are we calling on doctors who would have
> written anyway?*

Four queries, one schema, built to answer exactly those.

---

## Quickstart

```bash
make venv        # create .venv and install dependencies
make up          # start Postgres on :5544 and wait for health
make generate    # synthesise the source extracts (~2.2M rows, seeded)
make load        # run the ETL
make report      # run the four analytics queries
make tune        # measure what the indexes actually do
make test        # 34 tests
```

Port **5544**, not 5432/5433 — those are usually already taken, and a port clash is the
most boring possible reason for a demo to fail.

---

## Schema

Four conformed dimensions shared by two fact tables at different grains. *Conformed* means
one `WHERE` clause slices both facts identically — the reason a star schema beats a pile of
purpose-built report tables.

```
                          ┌──────────────┐
                          │   dim_date   │  730 rows
                          └──────┬───────┘
                                 │
   ┌──────────────┐   ┌──────────┴───────────┐   ┌─────────────────┐
   │ dim_product  │───│  fact_prescriptions  │───│  dim_territory  │
   │    8 rows    │   │    2,229,747 rows    │   │     50 rows     │
   │              │   └──────────────────────┘   │                 │
   │              │   ┌──────────────────────┐   │                 │
   │              │───│  fact_sales_calls    │───│                 │
   └──────────────┘   │      68,619 rows     │   └─────────────────┘
                      └──────────┬───────────┘
                                 │
                         ┌───────┴────────┐
                         │    dim_hcp     │  2,038 rows / 2,000 HCPs
                         └────────────────┘  ← SCD Type 2
```

### Why `dim_hcp` is Slowly Changing Dimension Type 2

If Dr. Sharma moves from territory T-12 to T-19 in June and you *overwrite* her row, every
prescription she wrote in January retroactively belongs to T-19. The rep who actually
covered T-12 in January loses credit for work they did, and last year's territory numbers
change silently every time somebody transfers. History becomes a lie.

Type 2 closes the old row and opens a new one, so a January fact keeps pointing at the
version that was current in January. This load produced **38 closed history rows** across
2,000 HCPs.

The invariant is enforced by Postgres, not by convention:

```sql
CREATE UNIQUE INDEX uq_hcp_current ON warehouse.dim_hcp (hcp_id) WHERE is_current;
```

And the ETL's key resolution is *temporal* — a fact dated 2024-03-04 resolves to the
`hcp_key` that was valid on 2024-03-04, not to whichever version is live now. A test
asserts it across all 2.2M rows:

```sql
-- zero rows, or the temporal join silently degraded to "latest version wins"
SELECT count(*) FROM warehouse.fact_prescriptions f
JOIN warehouse.dim_hcp h USING (hcp_key)
JOIN warehouse.dim_date d USING (date_key)
WHERE d.full_date < h.valid_from OR d.full_date > h.valid_to;
```

### Why staging is all `TEXT`

Source extracts lie. A real feed will hand you `'N/A'` in a numeric column eventually.
Casting in the transform step means one bad row is quarantined and counted, rather than
killing the load at the `COPY` boundary. Of 2,230,247 staged rows, **500 were rejected**
(250 uncastable numerics, 250 unknown product codes) and 2,229,747 loaded —
`staged == loaded + rejected`, asserted on every run.

---

## The load: foreign keys are the bottleneck, not the bytes

First attempt, with all eight foreign keys enabled: the `COPY` into `fact_prescriptions`
was still running after **14 minutes 48 seconds** and was killed, not finished.

2.2M rows × 4 foreign keys is ~8.9 million per-row index lookups. Dropping the constraints
around the load and re-adding them afterwards gets the same guarantee from one validation
pass per constraint:

```python
with constraints_suspended(conn, fact_tables):
    copy_dataframe(conn, rx_resolved,   "warehouse.fact_prescriptions", cols)
    copy_dataframe(conn, calls_resolved, "warehouse.fact_sales_calls",  cols)
# constraints re-added here, inside the same transaction
```

| | Fact load |
|---|---|
| Foreign keys enabled | **> 14m 48s** (killed, never completed) |
| Foreign keys suspended, revalidated after | **41–66 s** |

The guarantee is unchanged, not weakened: the constraints are restored inside the same
transaction, so a violating row fails the `ADD CONSTRAINT` and rolls the whole load back.

Full pipeline, end to end: **150–246 s**.

Both ranges are cold-run to warm-run across repeated executions on a 4-core laptop; the
low end is a re-run against a warm Postgres buffer cache. The comparison that matters is
unaffected — the constrained load never finished at all.

---

## Indexing: where it helps, and where it doesn't

`make tune` drops the indexes, measures, restores them, and measures again. Each figure is
the **median of three runs after a discarded warm-up**, because measuring "before" on a
cold cache and "after" on a warm one manufactures a speedup you did not earn.

| Query | No index | Indexed | Change | Plan when indexed |
|---|---|---|---|---|
| Prescriber drill-down (`WHERE hcp_key = …`) | 146.9 ms | **3.5 ms** | **42× faster** | Bitmap Heap Scan |
| Month-over-month brand share (full aggregate) | 2181.1 ms | 2139.6 ms | no change | Parallel Seq Scan |

**The second row is the interesting one.** The composite index does nothing for the brand
share report, and that is correct behaviour, not a failure. The query aggregates every row
in the table — there is no selective predicate, so there is nothing for an index to narrow,
and a parallel sequential scan is the right plan. An earlier version of this benchmark
appeared to show a 1.9× win there; that was entirely cold-vs-warm cache, and it disappeared
under a fair measurement.

Pushed further, the index actively *hurts* a date-and-product-filtered variant — 819 ms to
2079 ms — because the planner switches to a nested loop with 730 index lookups and loses
parallelism.

> An index earns its keep on **selectivity**, not on table size.

The index that does pay off is the selective one:

```sql
CREATE INDEX idx_rx_hcp ON warehouse.fact_prescriptions (hcp_key);
```

Leading column first, most selective filter first. `idx_rx_date_product` carries
`INCLUDE (trx_count)` so a date-ranged aggregate can be answered index-only without
touching the heap.

---

## The queries

| Query | Answers | Technique |
|---|---|---|
| `hcp_decile_ranking.sql` | Which prescribers matter most, per territory? | `NTILE(10)`, `RANK()` partitioned by territory |
| `brand_share_mom.sql` | Is our share growing or shrinking? | `SUM() OVER` for share, `LAG()` for the delta |
| `territory_attainment.sql` | Who is behind plan, and what is the YTD trend? | Running total via `SUM() OVER (ORDER BY …)` |
| `call_effectiveness.sql` | Do calls actually lift prescriptions? | 14-day lookback join, called vs. uncalled cohorts |

Sample output — **do sales calls work?**

```
   stratum   |   cohort   | hcp_months | avg_trx_per_hcp_month | lift_pct
-------------+------------+------------+-----------------------+----------
 ALL DECILES | CALLED     |      39231 |                 39.31 |     65.8
 ALL DECILES | NOT CALLED |       7985 |                 23.71 |      0.0
 DECILE 01   | CALLED     |       1326 |                  3.21 |     94.5
 DECILE 02   | CALLED     |       2853 |                  7.47 |     73.7
```

Called HCP-months average 39.31 TRx against 23.71 for uncalled — and the lift is largest in
the *low* deciles, which is the commercially interesting read: the high-volume prescribers
were going to write anyway.

*(The generator plants a 14-day post-call lift, so this measures that the pipeline and query
recover a known signal. It is a correctness check, not a claim about real medicine.)*

---

## Tests

```
34 passed
```

`tests/test_transform.py` — 14 pure unit tests, no database. This is why the transform
logic is written as pure functions over DataFrames:

* SCD2 closes the old row and opens exactly one new current row on a tracked change
* SCD2 is a no-op when an *untracked* attribute (a rename) changes
* unresolvable natural keys go to rejects, never silently dropped
* `loaded + rejected == input`, always

`tests/test_data_quality.py` — 20 assertions against the loaded warehouse:

* row-count reconciliation against the reject files
* no orphan foreign keys, across 7 fact→dimension relationships
* null thresholds and `nrx_count <= trx_count`
* the SCD2 invariant, and that no two versions of one HCP claim the same day
* every fact resolves to the *temporally correct* HCP version

They skip cleanly rather than failing when Postgres is not running.

---

## Stack

Python 3.14 · pandas 3.0 · SQLAlchemy 2.0 · psycopg2 · PostgreSQL 16 · Docker Compose · pytest

## What I would do next

* **Partition `fact_prescriptions` by month** — at 2.2M rows a seq scan is fine; at 200M the
  brand-share aggregate needs partition pruning, which is the real fix where an index was not.
* **Incremental loads** — today the pipeline is full-refresh. A watermark on `date_key` plus
  an upsert would make it restartable.
* **Airflow or dbt** for scheduling and lineage, once there is more than one pipeline.
* **A late-arriving-facts path** — a prescription that arrives after an HCP's territory
  change currently resolves correctly by date, but there is no backfill process.

---

## Licence

MIT. The data is entirely synthetic — generated by `etl/generate_data.py` with a fixed
seed. No real patient, prescriber or commercial data is present anywhere in this repo.
