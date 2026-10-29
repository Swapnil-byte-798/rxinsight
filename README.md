# RxInsight

[![tests](https://github.com/Swapnil-byte-798/rxinsight/actions/workflows/ci.yml/badge.svg)](https://github.com/Swapnil-byte-798/rxinsight/actions/workflows/ci.yml)

A pharmaceutical commercial analytics warehouse built with Python and PostgreSQL 16. It validates generated extracts, preserves prescriber history with SCD Type 2, loads conformed dimensions and two fact tables, and answers four analytical questions.

All source data is **synthetic**, generated with a fixed seed. The project is a full-refresh software demonstration, not a production feed or clinical system.

## Verified behavior

| Check | Result from the 29 October 2026 audit |
|---|---:|
| Prescription source rows | 2,230,247 |
| Loaded prescriptions / verified rejects | 2,229,747 / 500 |
| Loaded sales calls | 68,619 |
| HCPs / dimension versions | 2,000 / 2,038 |
| Complete suite per dependency configuration | 67 passed; zero skips |
| Independent report checks | 12 passed |
| Source-to-warehouse aggregate mismatches | Zero |

The repaired full-size ETL took 81.0 seconds in the audit environment. Both dependency configurations also passed [implementation CI](https://github.com/Swapnil-byte-798/rxinsight/actions/runs/40846649738). See the [verification record](docs/verification.md) for methodology and limits. Timings are measurements, not portable guarantees.

## Run it

Use Python 3.12 and Docker Compose. The database exposes host port 5544. Use the project's isolated database because normal refreshes reset its staging and warehouse schemas.

```bash
make venv
make up
make generate
make load
make test
make report
make tune
```

`make test` requires a populated warehouse; unavailable or empty DB checks fail. Pure units can run separately:

```bash
.venv/bin/python -m pytest tests/test_transform.py tests/test_regressions.py -q
```

Default requirements pin pandas 3.1.0, NumPy 2.5.4, SQLAlchemy 2.1.6, psycopg2-binary 2.9.13 and pytest 9.1.1. `requirements-compat.txt` exercises pandas 2.2.3 and SQLAlchemy 2.0.55. The SQLAlchemy engine explicitly uses the installed psycopg2 driver.

## Data model and loading

Four dimensions describe date, product, territory and HCP versions. Prescription facts are at HCP-version/product/day grain; call facts record individual calls. The shared dimensions make this one conformed star schema.

The loader lands TEXT staging, validates values, resolves natural keys against historical intervals and accounts for every input fact as loaded or rejected. Missing, fractional, non-finite or out-of-range counts and unresolved temporal keys are not silently accepted or dropped. Reject identities are checked in addition to counts.

HCP territory and decile changes create SCD Type 2 history. Conflicting same-day or backdated tracked changes are rejected rather than rewriting established intervals. Python date comparisons preserve the year-9999 sentinel across both tested pandas versions.

Fact loading uses batches, server-side cursor reads and COPY writes. Eight fact foreign keys are restored and validated inside the fact transaction, with reconciliation before commit. Earlier stages have separate commits; the complete refresh is not one atomic publication.

## Reports

| SQL file | Meaning |
|---|---|
| `hcp_decile_ranking.sql` | Own-brand volume ranking by historical HCP version and territory |
| `brand_share_mom.sql` | Share within therapeutic area/month and preceding reported-month change |
| `territory_attainment.sql` | Derived baseline-plus-8% quota, attainment and YTD reset |
| `call_effectiveness.sql` | Prior-days-1-through-14 exposure using stable HCP identity and one person-month |

Call analysis counts 47,184 person-months, not 47,216 SCD version-months. Called months average 39.32 own-brand TRx versus 23.70 for uncalled months. A separate raw-CSV as-of calculation reproduced both cohorts. This is an observational association on generated data, not causal promotional evidence.

## Indexing evidence

The benchmark discards a warm-up and uses the median of three measured runs per state.

| Workload | Without secondary indexes | Indexed | Plan when indexed |
|---|---:|---:|---|
| Selective prescriber lookup | 131.640 ms | 2.250 ms | Bitmap Heap Scan |
| Full aggregate | 1,165.269 ms | 1,172.546 ms | Parallel Seq Scan |

The selective lookup improved about 58.5x in that run. The unfiltered aggregate did not materially improve. Do not claim that the index eliminated sequential scans from the full report.

## Study documents

The [study-material index](docs/README.md) links matching Markdown and PDF editions of the architecture guide, interview questions and tool primer. All were updated to describe the repaired behavior, current measurements and known limits.

## Boundaries

Incremental watermarks, restartable publication, explicit history backfills, real source/target feeds, scheduling/lineage, sharding and production deployment are not implemented. Partitioning is a future workload-driven option, not a completed optimization. Local Docker Compose could not launch in the audit workspace; PostgreSQL 16 execution and GitHub service-container CI were separately verified.

## Licence

MIT. No real patient, prescriber or commercial records are included.
