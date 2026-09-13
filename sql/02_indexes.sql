-- =============================================================================
-- RxInsight — 02_indexes.sql  (the tuning story)
--
-- APPLY ORDER MATTERS. This file is deliberately NOT part of 01_schema.sql:
--
--   1. Load the warehouse (~2M prescription rows) with only the primary-key and
--      SCD2 indexes 01_schema.sql creates. Bulk loads are faster without extra
--      indexes to maintain, and the un-indexed state is the honest baseline.
--   2. Capture the BEFORE plan:
--        EXPLAIN (ANALYZE, BUFFERS) <sql/analytics/brand_share_mom.sql>
--      Expect a parallel Seq Scan over the whole fact table.
--   3. Apply THIS file.
--   4. Capture the AFTER plan with the identical query and paste both into the
--      README. Report measured numbers only — a fabricated latency figure is the
--      one claim an interviewer can disprove by asking "how did you measure it?"
--
-- To re-run the baseline from a tuned database, drop these first:
--   DROP INDEX IF EXISTS warehouse.idx_rx_date_product;
--   DROP INDEX IF EXISTS warehouse.idx_rx_hcp;
--   DROP INDEX IF EXISTS warehouse.idx_rx_territory;
--   DROP INDEX IF EXISTS warehouse.idx_calls_hcp_date;
--
-- Apply with:
--   psql postgresql://rxinsight:rxinsight@localhost:5544/rxinsight -f sql/02_indexes.sql
--
-- Note on composite B-tree column order — the rule behind every choice below:
-- Postgres can only range-scan a composite index on a prefix of its columns, so
-- the leading column must be the one the query pins down most selectively
-- (equality predicate or the primary grouping key). Columns used with a range
-- predicate go last, because a range on column N makes columns N+1.. useless
-- for narrowing the scan.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. The flagship index — target of the before/after EXPLAIN ANALYZE.
--
-- Serves brand_share_mom.sql, which scans fact_prescriptions, joins dim_date and
-- dim_product, and aggregates SUM(trx_count) by month and brand.
--
-- Why (date_key, product_key) and not the reverse:
--   * date_key leads because every commercial report is period-bounded ("last 12
--     months", "Q3 YTD"). Over 24 months of daily data date_key is the high-
--     cardinality, most selective predicate — a one-month filter cuts ~2M rows to
--     ~85K. product_key has only 8 distinct values; leading with it would cut the
--     scan to 1/8th at best and then still force a full range walk per product.
--   * product_key second refines within the date range and lets the planner feed
--     an already-sorted stream into the group-by, avoiding a sort.
--
-- Why INCLUDE (trx_count):
--   trx_count is the measure being summed, not a filter. Carrying it as an
--   INCLUDE payload rather than a fourth key column keeps the B-tree narrow (no
--   extra sort level, smaller internal pages) while still allowing an INDEX ONLY
--   SCAN — the aggregate reads the index and never touches the heap. That is
--   where the bulk of the measured speedup comes from: heap fetches disappear
--   from the BUFFERS line entirely.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_rx_date_product
    ON warehouse.fact_prescriptions (date_key, product_key)
    INCLUDE (trx_count);

COMMENT ON INDEX warehouse.idx_rx_date_product IS
    'Covering index for month/brand aggregation (brand_share_mom). Leading date_key = most selective filter; trx_count included for index-only scans.';

-- ---------------------------------------------------------------------------
-- 2. Prescriber access path.
--
-- Serves hcp_decile_ranking.sql (GROUP BY hcp_key over the fact table) and the
-- fact-to-fact join in call_effectiveness.sql, where prescriptions are matched to
-- calls on hcp_key.
--
-- Single-column, hcp_key leading, because that is the whole predicate: with 2,000
-- HCPs a single-prescriber lookup touches ~0.05% of the table. Adding date_key
-- here would duplicate what idx_rx_date_product already covers for period filters
-- while making this index bigger for the lookups it actually serves.
--
-- Second job, stated precisely: this index also backs the foreign key to dim_hcp.
-- Postgres indexes the referenced side automatically but never the referencing
-- side, so DELETEing a dim_hcp row — or changing an hcp_key — has to find the
-- children, which is a full scan of the fact table without this index. Note what
-- that does NOT cover: this pipeline is full-refresh and rebuilds dim_hcp by COPY,
-- so it issues no such DELETE, and SCD2 closes rows inside a pandas frame rather
-- than with SQL. Even an in-place close would not fire the check, because a
-- referential-integrity check only runs when the referenced key columns change and
-- closing a row touches valid_to and is_current, not hcp_key. The drill-down
-- workload above is what justifies this index today; the FK support is insurance
-- for a future incremental loader.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_rx_hcp
    ON warehouse.fact_prescriptions (hcp_key);

COMMENT ON INDEX warehouse.idx_rx_hcp IS
    'Per-prescriber lookups/aggregation (hcp_decile_ranking); also backs the FK to dim_hcp for dimension deletes/key changes.';

-- ---------------------------------------------------------------------------
-- 3. Territory access path.
--
-- Serves territory_attainment.sql, which aggregates actuals per territory and
-- runs a SUM() OVER running total inside each one.
--
-- territory_key leads (it is the only column in the index) because territory is
-- the equality/grouping predicate: 50 territories over 2M rows means one
-- territory's drill-down reads ~2% of the table — well inside the range where an
-- index scan beats a sequential scan. It likewise backs the FK to dim_territory,
-- with the same caveat as above: nothing in this pipeline deletes a territory or
-- rewrites a territory_key, so that is latent value, not a measured saving.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_rx_territory
    ON warehouse.fact_prescriptions (territory_key);

COMMENT ON INDEX warehouse.idx_rx_territory IS
    'Per-territory rollups (territory_attainment); also backs the FK to dim_territory.';

-- ---------------------------------------------------------------------------
-- 4. Call-to-prescription matching.
--
-- Serves call_effectiveness.sql: "did this HCP receive a call in the 14 days
-- before the script was written?" — an equality on hcp_key plus a BETWEEN on
-- date_key, evaluated once per prescriber.
--
-- Why (hcp_key, date_key) and not (date_key, hcp_key):
--   The equality column goes first. Positioned that way the planner descends
--   straight to one HCP's slice and then range-scans date_key within it, reading
--   only the matching window. Reversed, the date range would be the leading
--   bound and hcp_key would be a filter applied to every row in the window —
--   the index would degrade into a wide scan of the whole period.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_calls_hcp_date
    ON warehouse.fact_sales_calls (hcp_key, date_key);

COMMENT ON INDEX warehouse.idx_calls_hcp_date IS
    'Call-effectiveness lag join: equality on hcp_key first, date_key range second.';

-- ---------------------------------------------------------------------------
-- Refresh planner statistics.
--
-- An index the planner does not know about is an index it will not use. After a
-- bulk COPY load the tables carry stale (or zero) statistics, so the planner's
-- row estimates are guesses and it can still pick a sequential scan over a
-- perfectly good index. Autovacuum gets here eventually; an explicit ANALYZE
-- makes the AFTER plan reproducible instead of a matter of timing.
-- ---------------------------------------------------------------------------
ANALYZE warehouse.fact_prescriptions;
ANALYZE warehouse.fact_sales_calls;
