# RxInsight

**A pharmaceutical commercial analytics warehouse — star schema, Postgres, window-function SQL.**

> ⚠️ **Work in progress.** The SQL layer is complete and verified. The Python ETL,
> test suite and Docker Compose setup are specified in [BUILD_SPEC.md](BUILD_SPEC.md)
> but not yet written. See [Status](#status) for exactly what does and doesn't exist.

---

## The business question

Open any pharma brand team's dashboard and it tells you total prescriptions this month.
That isn't the question the team actually has. The questions are:

> *Which prescribers are actually driving our brand?*
> *Which territories are behind plan, and by how much?*
> *Are our sales calls moving prescriptions, or are we calling on doctors who'd have
> written anyway?*

Those three questions are what this schema exists to answer. Every table and every query
here traces back to one of them.

---

## Design

Four **conformed dimensions** shared by two fact tables at different grains. Conformed
means one `WHERE` clause slices both facts identically — the reason a star schema beats
a pile of purpose-built tables.

```
                      ┌───────────────┐
                      │   dim_date    │
                      └───────┬───────┘
                              │
  ┌────────────────┐  ┌───────┴────────────┐  ┌──────────────────┐
  │  dim_product   │──│ fact_prescriptions │──│   dim_territory  │
  │                │  │  trx, nrx, units,  │  │                  │
  │                │  │    gross_sales     │  │                  │
  │                │  └────────────────────┘  │                  │
  │                │                          │                  │
  │                │  ┌────────────────────┐  │                  │
  │                │──│ fact_sales_calls   │──│                  │
  │                │  │ call_type, minutes,│  │                  │
  └────────────────┘  │  samples_dropped   │  └──────────────────┘
                      └─────────┬──────────┘
                                │
                        ┌───────┴────────┐
                        │    dim_hcp     │  ← SCD Type 2
                        └────────────────┘
```

### Why `dim_hcp` is Slowly Changing Dimension Type 2

This is the design decision worth defending.

If Dr. Sharma moves from territory T-12 to T-19 in June and you *overwrite* her row,
every prescription she wrote in January retroactively belongs to T-19. T-12's rep loses
credit for work they actually did, and last year's territory performance silently changes
every time someone transfers. History becomes a lie.

Type 2 closes the old row (`valid_to`, `is_current = false`) and opens a new one with a new
surrogate key. A January script still points at the `hcp_key` that was current in January.

The invariant that makes it trustworthy is enforced in the schema, not assumed:

```sql
CREATE UNIQUE INDEX uq_hcp_current ON warehouse.dim_hcp (hcp_id) WHERE is_current;
```

Exactly one current row per HCP, guaranteed by Postgres rather than by convention.

### Why staging is all `TEXT`

Source extracts lie. A real prescription feed will hand you `'N/A'` in a numeric column
eventually. Casting in the transform step means one bad row fails in Python where it can be
quarantined and counted — not at the `COPY` boundary where it kills the entire load.

---

## The queries

All four live in [`sql/analytics/`](sql/analytics) and use CTEs and window functions
rather than nested subqueries.

| Query | What it answers | Technique |
|---|---|---|
| `hcp_decile_ranking.sql` | Which prescribers matter most, per territory? | `NTILE(10)`, `RANK()` partitioned by territory |
| `brand_share_mom.sql` | Is our share growing or shrinking month over month? | `SUM() OVER` for share, `LAG()` for the delta |
| `territory_attainment.sql` | Who's behind plan, and what's the YTD trend? | Running total via `SUM() OVER (ORDER BY ...)` |
| `call_effectiveness.sql` | Do calls actually lift prescriptions? | 14-day lookback join, called vs. uncalled cohorts |

Each file opens with the business question it answers and what a brand manager would do
with the result.

---

## Indexing

`sql/02_indexes.sql` is applied *after* a baseline `EXPLAIN ANALYZE`, so the before/after
is measurable rather than asserted. The main one:

```sql
CREATE INDEX idx_rx_date_product
    ON warehouse.fact_prescriptions (date_key, product_key)
    INCLUDE (trx_count);
```

Column order is deliberate: `date_key` leads because every report filters or groups by
time first. `INCLUDE (trx_count)` makes it covering — the aggregate is answered from the
index without touching the heap.

---

## Status

**Done and verified** — schema and all four queries were applied to a real
`postgres:16-alpine` instance; DDL and indexes apply cleanly and every analytics query
executes against the created schema.

| | File | Lines |
|---|---|---|
| ✅ | `sql/01_schema.sql` — 9 tables, FKs, CHECKs, SCD2 partial unique index | 173 |
| ✅ | `sql/02_indexes.sql` — 4 tuning indexes + `ANALYZE`, each commented | 134 |
| ✅ | `sql/analytics/hcp_decile_ranking.sql` | 48 |
| ✅ | `sql/analytics/brand_share_mom.sql` | 51 |
| ✅ | `sql/analytics/territory_attainment.sql` | 134 |
| ✅ | `sql/analytics/call_effectiveness.sql` | 156 |

**Not yet written** — fully specified in [BUILD_SPEC.md](BUILD_SPEC.md):

| | Component |
|---|---|
| ⬜ | `etl/generate_data.py` — vectorized synthetic generator (~2M prescription rows) |
| ⬜ | `etl/transform.py` — SCD2, surrogate-key resolution, reject routing |
| ⬜ | `etl/load_staging.py` / `etl/pipeline.py` — `COPY` bulk load + orchestration |
| ⬜ | `tests/` — pytest unit tests and data-quality assertions |
| ⬜ | `docker-compose.yml`, `Makefile`, `requirements.txt` |
| ⬜ | Measured before/after `EXPLAIN ANALYZE` numbers |

## Reproducing what exists

```bash
docker run -d --name rxinsight \
  -e POSTGRES_USER=rxinsight -e POSTGRES_PASSWORD=rxinsight -e POSTGRES_DB=rxinsight \
  -p 5544:5432 postgres:16-alpine

psql postgresql://rxinsight:rxinsight@localhost:5544/rxinsight -f sql/01_schema.sql
psql postgresql://rxinsight:rxinsight@localhost:5544/rxinsight -f sql/02_indexes.sql
```

Port 5544 rather than the conventional 5432/5433 to avoid colliding with other local
Postgres instances.

## Next

Build the ETL per `BUILD_SPEC.md`, load ~2M rows, then capture real before/after
`EXPLAIN ANALYZE` timings on `brand_share_mom.sql` and record them here.
