-- ============================================================================
-- territory_attainment.sql
--
-- BUSINESS QUESTION
--   Which territories are hitting their monthly own-brand TRx plan, and how
--   deep is the year-to-date hole for the ones that are not?
--
-- WHAT A BRAND MANAGER DOES WITH IT
--   This is the field-force scorecard that runs the monthly business review.
--   Monthly attainment says who missed last month; ytd_attainment_pct says who
--   is structurally behind, which is the number that actually drives action --
--   a rep at 96% for one month is noise, a rep at 88% YTD in month nine needs
--   a coaching plan, a territory realignment, or a sampling budget. The ranking
--   column turns it into a league table for the national sales meeting, and
--   plan_status is the traffic light a regional manager scans first.
--
-- GRAIN      one row per (territory, year_month)
-- MEASURES   actual_trx / target_trx      monthly own-brand scripts vs plan
--            ytd_*                        running totals, reset each calendar year
--            attainment_pct               actual / target for the month
--            ytd_attainment_pct           YTD actual / YTD target
--
-- HOW THE TARGET IS DERIVED (and why)
--   There is no target table in this warehouse -- in a real commercial stack
--   the quota comes from the annual brand plan or the incentive-compensation
--   system, and lands as its own fact table. Rather than invent a fake feed,
--   the target here is a deterministic function of each territory's own
--   baseline volume:
--
--       baseline_monthly_trx = AVG(own-brand TRx) over the territory's FIRST
--                              SIX months on file
--       target_trx           = ROUND(baseline_monthly_trx * 1.08)
--
--   That mirrors how quotas are genuinely set -- last period's run rate plus a
--   growth ask -- and it is territory-relative, so a small rural territory is
--   not measured against a metro territory's absolute volume. The 8% uplift is
--   the growth ask. Two consequences worth stating out loud rather than hiding:
--   the first six months are the plan-setting window, so they will sit near
--   ~93% attainment by construction and should be read as baseline rather than
--   performance; and because the target is flat across months, a seasonal
--   market will show seasonal attainment swings. A real plan seasonalises the
--   quota; swapping this CTE for a join to a target fact table is the only
--   change needed if one ever exists.
--
-- NOTES
--   * territory_key is taken from the fact row, which is the territory that
--     owned the script at the time it was written -- SCD Type 2 on dim_hcp
--     means a mid-year territory move does not retroactively move history.
--   * The YTD window partitions by (territory, calendar year) so the running
--     total resets in January, which is what "year to date" means to a sales
--     ops team. An explicit ROWS frame is used instead of the default RANGE
--     frame so that the total is truly cumulative row-by-row.
--
-- RUN
--   psql postgresql://rxinsight:rxinsight@localhost:5544/rxinsight \
--        -f sql/analytics/territory_attainment.sql
-- ============================================================================

WITH monthly_actuals AS (
    -- Actuals: own-brand scripts only. Competitor volume is market context,
    -- not something a rep is paid on.
    SELECT t.territory_key,
           t.territory_code,
           t.territory_name,
           t.region,
           d.year,
           d.year_month,
           SUM(f.trx_count)   AS actual_trx,
           SUM(f.gross_sales) AS actual_gross_sales
    FROM warehouse.fact_prescriptions f
    JOIN warehouse.dim_date      d USING (date_key)
    JOIN warehouse.dim_product   p USING (product_key)
    JOIN warehouse.dim_territory t USING (territory_key)
    WHERE p.is_competitor = FALSE
    GROUP BY t.territory_key, t.territory_code, t.territory_name, t.region,
             d.year, d.year_month
),
sequenced AS (
    -- Number each territory's months so the baseline window is the first six
    -- months that territory actually has data for, not a hard-coded date.
    SELECT m.*,
           ROW_NUMBER() OVER (PARTITION BY m.territory_key ORDER BY m.year_month) AS month_seq
    FROM monthly_actuals m
),
baseline AS (
    SELECT territory_key,
           AVG(actual_trx) AS baseline_monthly_trx
    FROM sequenced
    WHERE month_seq <= 6
    GROUP BY territory_key
),
planned AS (
    -- Baseline run rate + 8% growth ask = the monthly quota.
    SELECT s.territory_key,
           s.territory_code,
           s.territory_name,
           s.region,
           s.year,
           s.year_month,
           s.month_seq,
           s.actual_trx,
           s.actual_gross_sales,
           ROUND(b.baseline_monthly_trx)          AS baseline_monthly_trx,
           ROUND(b.baseline_monthly_trx * 1.08)   AS target_trx
    FROM sequenced s
    JOIN baseline  b USING (territory_key)
)
SELECT territory_code,
       territory_name,
       region,
       year_month,
       actual_trx,
       target_trx,
       actual_trx - target_trx AS gap_to_target_trx,
       ROUND(100.0 * actual_trx / NULLIF(target_trx, 0), 1) AS attainment_pct,
       SUM(actual_trx) OVER ytd AS ytd_actual_trx,
       SUM(target_trx) OVER ytd AS ytd_target_trx,
       ROUND(100.0 * SUM(actual_trx) OVER ytd
                   / NULLIF(SUM(target_trx) OVER ytd, 0), 1) AS ytd_attainment_pct,
       RANK() OVER (PARTITION BY year_month
                    ORDER BY actual_trx / NULLIF(target_trx, 0) DESC) AS attainment_rank_in_month,
       CASE
           WHEN month_seq <= 6                 THEN 'BASELINE PERIOD'
           WHEN actual_trx >= target_trx       THEN 'AT/ABOVE PLAN'
           WHEN actual_trx >= 0.90 * target_trx THEN 'WATCH'
           ELSE 'BEHIND PLAN'
       END AS plan_status
FROM planned
WINDOW ytd AS (
    PARTITION BY territory_key, year
    ORDER BY year_month
    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
)
ORDER BY year_month, attainment_pct;
