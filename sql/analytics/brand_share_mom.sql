-- ============================================================================
-- brand_share_mom.sql
--
-- BUSINESS QUESTION
--   What share of total prescriptions in this therapeutic market does each
--   brand hold each month, and is our share rising or falling month over month?
--
-- WHAT A BRAND MANAGER DOES WITH IT
--   Market share is the number the brand team is measured on, because absolute
--   TRx can rise while the brand still loses -- a market growing 10% with a
--   brand growing 4% is a brand in trouble. share_delta_pp is the headline:
--   it says, in percentage points, whether last month's promotional spend
--   bought share or just rode the market. A negative delta for our brand
--   against a positive delta for one competitor is the trigger for a
--   competitive response (message change, sampling push, share-of-voice shift).
--   This is also the query used for the index-tuning benchmark in the README.
--
-- GRAIN      one row per (year_month, brand_name)
-- MEASURES   trx           = total scripts for the brand that month
--            share_pct     = brand TRx / all-brand TRx that month, in percent
--            share_delta_pp = change in share_pct vs the brand's prior month,
--                             in percentage points (a point change, not a
--                             percent change -- 12.0 -> 13.0 is +1.0 pp)
--
-- NOTES
--   * Competitor products are deliberately NOT filtered out: the denominator
--     has to be the whole market or "share" means nothing.
--   * LAG partitions by brand so each brand is compared against its own prior
--     month; the first month of history correctly yields NULL.
--
-- RUN
--   psql postgresql://rxinsight:rxinsight@localhost:5544/rxinsight \
--        -f sql/analytics/brand_share_mom.sql
-- ============================================================================

WITH monthly AS (
    SELECT d.year_month, p.brand_name, p.is_competitor, SUM(f.trx_count) AS trx
    FROM warehouse.fact_prescriptions f
    JOIN warehouse.dim_date d    USING (date_key)
    JOIN warehouse.dim_product p USING (product_key)
    GROUP BY d.year_month, p.brand_name, p.is_competitor
),
shared AS (
    SELECT year_month, brand_name, trx,
           ROUND(100.0 * trx / NULLIF(SUM(trx) OVER (PARTITION BY year_month), 0), 2) AS share_pct
    FROM monthly
)
SELECT year_month, brand_name, trx, share_pct,
       share_pct - LAG(share_pct) OVER (PARTITION BY brand_name ORDER BY year_month) AS share_delta_pp
FROM shared
ORDER BY year_month, share_pct DESC;
