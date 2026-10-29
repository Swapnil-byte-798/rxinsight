# Verification record — 29 October 2026

The execution audit of revision `c644fb6` found installation, temporal-key,
validation, SCD, and report-definition defects. Those defects are fixed in the
source containing this record. The original Python/PostgreSQL architecture is
preserved; the default dependency file is now pinned.

## Executed checks

| Check | Result |
|---|---|
| Clean requirements install | Passed; no extra PostgreSQL driver installation needed |
| Full suite: pandas 3.1.0 / SQLAlchemy 2.1.6 | **67 passed, zero skips** |
| Full suite: pandas 2.2.3 / SQLAlchemy 2.0.55 | **67 passed, zero skips** |
| Full default regenerated source | 2,230,247 prescriptions; 68,619 calls; 2,000 distinct HCPs |
| Clean-environment full ETL | **81.0 seconds** on the audit environment |
| Fact load and FK revalidation | 58.7 seconds |
| Warehouse prescriptions | 2,229,747 loaded; 500 rejected |
| Warehouse sales calls | 68,619 loaded; none rejected |
| Reject identity | 250 actual non-numeric TRx rows; 250 actual unknown product codes |
| HCP history | 2,038 versions: 2,000 current and 38 closed |
| Source versus warehouse HCP/product/day aggregates | **Zero mismatches** for TRx, NRx, units, and gross sales |
| Duplicate prescription fact grain | Zero |
| Warehouse foreign keys | All nine validated, including all eight fact FKs |
| Independent report arithmetic | **12 checks passed**, including market share, ranks, quota, YTD reset, and person-month counts |
| Independent raw-CSV exposure algorithm | Matched both SQL cohorts using a separate as-of join algorithm |

Tests run against real PostgreSQL 16.15. Local Docker Compose was unavailable;
the database transport used a secure WebSocket relay, without changing the
project's PostgreSQL driver, ETL, SQL, or tests. GitHub Actions runs PostgreSQL 16
containers and the entire suite for both requirement files. `--require-db` makes
unavailable PostgreSQL and empty warehouse checks fail rather than skip.

## Corrections

- Explicitly select the installed psycopg2 driver with SQLAlchemy 2.0 and 2.1.
- Compare SCD validity as Python dates so the year-9999 sentinel works on pandas 2.2 and 3.1.
- Preserve every input position through temporal resolution. No matching version
  and ambiguous overlapping versions are quarantined instead of silently lost or picked arbitrarily.
- Quarantine missing, fractional, non-finite, and out-of-range integer values.
  Calendar-invalid dates are validated before PostgreSQL casting and then rejected by the transform.
- Reject conflicting same-day/backdated tracked SCD updates and enforce valid
  database intervals. These require an explicit backfill; historical facts are not rewritten.
- Calculate brand share within `(year_month, therapeutic_area)`.
- Join call exposure through stable `hcp_id`, and count one person-month across
  SCD transfers. Label its segment on its last prescribing day.
- Use the same prior-days-1-through-14 window in the generator and SQL, excluding the same day.
- Multiply before dividing when deriving territory quotas. An additional regression
  catches the exact half-unit case: `8675 * 1.08 / 6 = 1561.50`, rounded to **1562**.
- Reconcile fact counts before committing them; make report commands stop on SQL errors.

The original audit's failing cases now pass. The old positive control that summed
share across unrelated markets was replaced with a control that sums to 100%
inside each therapeutic market. This follows the corrected business definition.

## Current report results

All four SQL files execute successfully: 192 brand-share rows, 22 call-effectiveness
rows, 2,038 HCP-version ranking rows, and 1,200 territory-month rows.

| Exposure cohort | People | Person-months | Total own-brand TRx | Mean TRx |
|---|---:|---:|---:|---:|
| Called | 2,000 | 39,225 | 1,542,150 | 39.32 |
| Not called | 1,947 | 7,959 | 188,653 | 23.70 |

There are **47,184 distinct prescribing person-months**, not 47,216 SCD
version-months. A person may appear in different exposure cohorts in different months.
These are results on seeded **synthetic data**, not causal or clinical findings.

## Indexing measurement

Executed `etl.measure`: discard a warm-up, then take the median of three runs.

| Query | Without secondary indexes | Indexed | Indexed plan |
|---|---:|---:|---|
| Selective prescriber lookup | 131.640 ms | 2.250 ms | Bitmap Heap Scan on fact_prescriptions |
| Full aggregate | 1165.269 ms | 1172.546 ms | Parallel Seq Scan on fact_prescriptions |

The selective lookup improved **58.5x in this run**. The full aggregate
still uses a parallel sequential scan and shows no material improvement. Do not
claim that indexing eliminated scans from the full brand report. Timings are
machine-dependent and are not portable performance guarantees.

## Reproduce

```bash
make venv
make up
make generate
make load
make test
make report
make tune
```

For the older dependency combination, install `requirements-compat.txt` in a
separate virtual environment and rerun `pytest tests --require-db`.
The full refresh resets the staging and warehouse schemas, so use the project's
isolated database. This is not an incremental loader or a production clinical
system. The Markdown study guides and PDF editions were rebuilt together on 29 October
2026. The documentation index links both maintained formats.
