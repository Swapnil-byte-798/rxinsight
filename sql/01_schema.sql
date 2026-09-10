-- =============================================================================
-- RxInsight — 01_schema.sql
-- Star-schema warehouse for pharmaceutical commercial analytics.
--
-- Business question this schema exists to answer:
--   "Which HCPs are driving our brand, which territories are behind plan,
--    and are our sales calls actually moving prescriptions?"
--
-- Design:
--   * Four conformed dimensions (date, product, territory, HCP) shared by two
--     fact tables at different grains — prescriptions and sales calls. Conformed
--     dimensions are what let a single WHERE clause slice both facts the same way.
--   * dim_hcp is Slowly Changing Dimension Type 2: an HCP who moves territory or
--     changes decile gets a NEW row with a new surrogate key. History stays true —
--     a script written in January still points at the territory that owned that
--     HCP in January, so the January rep keeps credit for the work they did.
--   * staging.* is deliberately all TEXT. Source extracts lie; a real feed will
--     hand you 'N/A' in a numeric column. Casting in transform means a bad row
--     fails in Python where it can be quarantined, not at the COPY boundary
--     where it would kill the entire load.
--
-- Idempotent: drops and recreates both schemas, so it can be re-run at will.
-- Apply with:
--   psql postgresql://rxinsight:rxinsight@localhost:5544/rxinsight -f sql/01_schema.sql
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Reset — CASCADE clears the FK graph in one shot so re-runs never half-apply.
-- ---------------------------------------------------------------------------
DROP SCHEMA IF EXISTS warehouse CASCADE;
DROP SCHEMA IF EXISTS staging   CASCADE;

CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS warehouse;

COMMENT ON SCHEMA staging   IS 'Raw landing zone. Every column TEXT; no constraints. Cast and validate in transform.';
COMMENT ON SCHEMA warehouse IS 'Conformed star schema: 4 dimensions, 2 fact tables, enforced referential integrity.';

-- ===========================================================================
-- DIMENSIONS
-- ===========================================================================

-- Calendar dimension. Integer YYYYMMDD surrogate key: compact, sorts naturally,
-- and keeps date logic (fiscal periods, month names, MoM grouping) out of the
-- fact tables and out of every analytics query that needs it.
CREATE TABLE warehouse.dim_date (
    date_key      INTEGER PRIMARY KEY,        -- YYYYMMDD
    full_date     DATE    NOT NULL UNIQUE,
    year          SMALLINT NOT NULL,
    quarter       SMALLINT NOT NULL,
    month         SMALLINT NOT NULL,
    month_name    TEXT     NOT NULL,
    year_month    CHAR(7)  NOT NULL           -- 'YYYY-MM', for MoM grouping
);

COMMENT ON TABLE  warehouse.dim_date            IS 'Calendar dimension, one row per day.';
COMMENT ON COLUMN warehouse.dim_date.date_key   IS 'Surrogate key in YYYYMMDD form (e.g. 20240131).';
COMMENT ON COLUMN warehouse.dim_date.year_month IS 'YYYY-MM string; the grouping key for month-over-month brand share.';

-- Product dimension. is_competitor is the flag that makes brand-share analysis
-- possible: our molecules and the market's molecules live in one table so the
-- share denominator is a simple SUM over the whole market.
CREATE TABLE warehouse.dim_product (
    product_key      SERIAL PRIMARY KEY,
    product_code     TEXT NOT NULL UNIQUE,    -- natural key
    brand_name       TEXT NOT NULL,
    molecule         TEXT NOT NULL,
    therapeutic_area TEXT NOT NULL,
    is_competitor    BOOLEAN NOT NULL DEFAULT FALSE
);

COMMENT ON TABLE  warehouse.dim_product               IS 'Product dimension covering both our brands and tracked competitor brands.';
COMMENT ON COLUMN warehouse.dim_product.product_code  IS 'Natural key from the source extract; surrogate key is product_key.';
COMMENT ON COLUMN warehouse.dim_product.is_competitor IS 'TRUE for market/competitor products — the denominator side of brand share.';

-- Sales geography. Territory is the unit a rep is measured on, so it hangs off
-- both fact tables directly (denormalised from dim_hcp on purpose — see the
-- fact table comments).
CREATE TABLE warehouse.dim_territory (
    territory_key  SERIAL PRIMARY KEY,
    territory_code TEXT NOT NULL UNIQUE,
    territory_name TEXT NOT NULL,
    region         TEXT NOT NULL,
    country        TEXT NOT NULL DEFAULT 'India'
);

COMMENT ON TABLE  warehouse.dim_territory                IS 'Sales territory dimension; the grain a field rep is measured at.';
COMMENT ON COLUMN warehouse.dim_territory.territory_code IS 'Natural key from the source extract; surrogate key is territory_key.';

-- SCD Type 2: an HCP moving territory or changing decile creates a NEW row.
-- hcp_id is the natural key and repeats; hcp_key is the surrogate.
CREATE TABLE warehouse.dim_hcp (
    hcp_key       SERIAL PRIMARY KEY,
    hcp_id        TEXT     NOT NULL,
    full_name     TEXT     NOT NULL,
    specialty     TEXT     NOT NULL,
    decile        SMALLINT CHECK (decile BETWEEN 1 AND 10),
    territory_key INTEGER  NOT NULL REFERENCES warehouse.dim_territory(territory_key),
    valid_from    DATE     NOT NULL,
    valid_to      DATE     NOT NULL DEFAULT '9999-12-31',
    is_current    BOOLEAN  NOT NULL DEFAULT TRUE
);

-- Exactly one current row per HCP — enforced, not assumed.
-- A partial unique index is the right tool here: the uniqueness only holds over
-- the current slice, while the closed historical rows for the same hcp_id are
-- free to repeat. Postgres also keeps the index small (one entry per HCP, not
-- one per version), so the current-row lookup the ETL does on every load stays cheap.
CREATE UNIQUE INDEX uq_hcp_current ON warehouse.dim_hcp (hcp_id) WHERE is_current;

COMMENT ON TABLE  warehouse.dim_hcp            IS 'Health care professional dimension, SCD Type 2 on decile and territory_key.';
COMMENT ON COLUMN warehouse.dim_hcp.hcp_key    IS 'Surrogate key. One per version of an HCP — facts point here, never at hcp_id.';
COMMENT ON COLUMN warehouse.dim_hcp.hcp_id     IS 'Natural key from the source extract; repeats across versions.';
COMMENT ON COLUMN warehouse.dim_hcp.decile     IS 'Prescriber value segment 1-10 (10 = highest volume). Tracked attribute.';
COMMENT ON COLUMN warehouse.dim_hcp.valid_from IS 'Inclusive start of this version''s validity window.';
COMMENT ON COLUMN warehouse.dim_hcp.valid_to   IS 'Inclusive end of validity; 9999-12-31 marks the open, current row.';
COMMENT ON COLUMN warehouse.dim_hcp.is_current IS 'TRUE for the single live version of an hcp_id; enforced by uq_hcp_current.';

-- ===========================================================================
-- FACTS
-- ===========================================================================

-- Grain: one row per HCP per product per day.
-- territory_key is carried on the fact rather than joined through dim_hcp so
-- that territory rollups stay correct after an SCD2 move and don't require
-- walking the HCP version history at query time.
CREATE TABLE warehouse.fact_prescriptions (
    rx_key        BIGSERIAL PRIMARY KEY,
    date_key      INTEGER NOT NULL REFERENCES warehouse.dim_date(date_key),
    hcp_key       INTEGER NOT NULL REFERENCES warehouse.dim_hcp(hcp_key),
    product_key   INTEGER NOT NULL REFERENCES warehouse.dim_product(product_key),
    territory_key INTEGER NOT NULL REFERENCES warehouse.dim_territory(territory_key),
    trx_count     INTEGER NOT NULL CHECK (trx_count >= 0),   -- total scripts
    nrx_count     INTEGER NOT NULL CHECK (nrx_count >= 0),   -- new scripts
    units         NUMERIC(12,2) NOT NULL,
    gross_sales   NUMERIC(14,2) NOT NULL
);

COMMENT ON TABLE  warehouse.fact_prescriptions             IS 'Prescription fact. Grain: one row per HCP per product per day.';
COMMENT ON COLUMN warehouse.fact_prescriptions.trx_count   IS 'Total prescriptions (TRx) — new plus refill.';
COMMENT ON COLUMN warehouse.fact_prescriptions.nrx_count   IS 'New prescriptions (NRx) — the subset of TRx that are new starts.';
COMMENT ON COLUMN warehouse.fact_prescriptions.gross_sales IS 'Gross sales value of the scripts on this row, in local currency.';

-- Grain: one row per sales call (a rep visit, detailing one product to one HCP).
CREATE TABLE warehouse.fact_sales_calls (
    call_key         BIGSERIAL PRIMARY KEY,
    date_key         INTEGER NOT NULL REFERENCES warehouse.dim_date(date_key),
    hcp_key          INTEGER NOT NULL REFERENCES warehouse.dim_hcp(hcp_key),
    product_key      INTEGER NOT NULL REFERENCES warehouse.dim_product(product_key),
    territory_key    INTEGER NOT NULL REFERENCES warehouse.dim_territory(territory_key),
    call_type        TEXT NOT NULL CHECK (call_type IN ('DETAIL','SAMPLE','FOLLOW_UP')),
    duration_minutes SMALLINT NOT NULL,
    samples_dropped  SMALLINT NOT NULL DEFAULT 0
);

COMMENT ON TABLE  warehouse.fact_sales_calls                 IS 'Sales-call fact. Grain: one row per rep call on one HCP for one product.';
COMMENT ON COLUMN warehouse.fact_sales_calls.call_type       IS 'DETAIL, SAMPLE or FOLLOW_UP — constrained so a bad feed cannot invent a type.';
COMMENT ON COLUMN warehouse.fact_sales_calls.samples_dropped IS 'Sample units left with the HCP during the call.';

-- ===========================================================================
-- STAGING (raw, all TEXT — cast happens in transform)
-- ===========================================================================

CREATE TABLE staging.stg_hcp           (hcp_id TEXT, full_name TEXT, specialty TEXT,
                                        decile TEXT, territory_code TEXT, effective_date TEXT);
CREATE TABLE staging.stg_prescriptions (rx_date TEXT, hcp_id TEXT, product_code TEXT,
                                        trx_count TEXT, nrx_count TEXT, units TEXT, gross_sales TEXT);
CREATE TABLE staging.stg_sales_calls   (call_date TEXT, hcp_id TEXT, product_code TEXT,
                                        call_type TEXT, duration_minutes TEXT, samples_dropped TEXT);

COMMENT ON TABLE staging.stg_hcp           IS 'Raw HCP master extract. All TEXT; no constraints — bad rows are quarantined in transform.';
COMMENT ON TABLE staging.stg_prescriptions IS 'Raw prescription extract. All TEXT; typed and key-resolved in transform.';
COMMENT ON TABLE staging.stg_sales_calls   IS 'Raw sales-call extract. All TEXT; typed and key-resolved in transform.';
