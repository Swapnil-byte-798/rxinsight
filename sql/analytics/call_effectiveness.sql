-- ============================================================================
-- call_effectiveness.sql
--
-- BUSINESS QUESTION
--   Do HCPs who were called on by a rep in the previous 14 days write more of
--   our brand than HCPs who were not?
--
-- WHAT A BRAND MANAGER DOES WITH IT
--   This is the return-on-promotion question that decides next year's field
--   force budget. If called HCP-months out-prescribe uncalled HCP-months by a
--   wide margin, call frequency is the lever to pull and the ask is more reps
--   or more calls per rep. If the lift is thin -- especially inside the top
--   deciles, where reps already call on everyone -- the money is better spent
--   on message quality, sampling, or non-personal promotion. The per-decile
--   rows matter more than the headline row: they tell you WHICH segment
--   responds, which is how a call plan gets rewritten.
--
-- GRAIN      one row per (decile stratum, exposure cohort); the 'ALL DECILES'
--            rows are the national headline, produced by the same GROUPING SETS
--            pass so the totals can never drift from the detail
-- UNIT OF
-- ANALYSIS   the HCP-month. An HCP-month is CALLED if, on at least one day that
--            HCP wrote a script that month, a sales call had landed in the
--            preceding 14 days.
--
-- HOW EXPOSURE IS DEFINED
--   Prescriptions are rolled up to HCP-day, sales calls to HCP-day, and the two
--   are joined through dim_date on a 14-day lookback:
--       call_date BETWEEN rx_date - 14 AND rx_date - 1
--   The window is exclusive of the script day itself -- a call and a script on
--   the same day is not evidence the call moved the script -- and 14 days is
--   used because that is the response lag the commercial team assumes for a
--   detail (and the lag the synthetic generator encodes, so this query is also
--   a check that the ETL preserved the signal).
--
-- READ THIS BEFORE QUOTING THE LIFT
--   The comparison is observational, not a randomised experiment, and it is
--   confounded in the obvious direction: reps deliberately call on high-decile
--   HCPs, so high prescribers are over-represented in the CALLED cohort. The
--   national lift is therefore an upper bound. Stratifying by decile is the
--   cheap control -- within a single decile the targeting bias is much smaller,
--   so the per-decile lift is the number to defend in a meeting. A clean causal
--   read needs a matched-control or pre/post design; that is the next iteration,
--   not something this query claims.
--   Second caveat: an HCP-month only exists here if the HCP wrote at least one
--   script that month, because months with no prescribing leave no fact row.
--
-- PERFORMANCE
--   Both facts are aggregated to HCP-day BEFORE the range join. Joining ~2M raw
--   prescription rows directly against the call fact is what makes the naive
--   version of this report unusable.
--
-- RUN
--   psql postgresql://rxinsight:rxinsight@localhost:5544/rxinsight \
--        -f sql/analytics/call_effectiveness.sql
-- ============================================================================

WITH rx_days AS (
    -- Own-brand scripts per HCP per day. Competitor volume is not something a
    -- call from our rep is supposed to move.
    SELECT f.hcp_key,
           d.full_date  AS rx_date,
           d.year_month,
           SUM(f.trx_count) AS trx
    FROM warehouse.fact_prescriptions f
    JOIN warehouse.dim_date    d USING (date_key)
    JOIN warehouse.dim_product p USING (product_key)
    WHERE p.is_competitor = FALSE
    GROUP BY f.hcp_key, d.full_date, d.year_month
),
call_days AS (
    -- One row per HCP per day on which that HCP was called on at all; call type
    -- and duration do not change whether the HCP was touched.
    SELECT DISTINCT
           c.hcp_key,
           d.full_date AS call_date
    FROM warehouse.fact_sales_calls c
    JOIN warehouse.dim_date    d USING (date_key)
    JOIN warehouse.dim_product p USING (product_key)
    WHERE p.is_competitor = FALSE
),
rx_day_exposure AS (
    -- The 14-day lookback. LEFT JOIN, so prescribing days with no preceding
    -- call survive as the control group instead of disappearing.
    SELECT r.hcp_key,
           r.year_month,
           r.rx_date,
           r.trx,
           BOOL_OR(c.call_date IS NOT NULL) AS called_in_prior_14d
    FROM rx_days r
    LEFT JOIN call_days c
           ON c.hcp_key = r.hcp_key
          AND c.call_date BETWEEN r.rx_date - 14 AND r.rx_date - 1
    GROUP BY r.hcp_key, r.year_month, r.rx_date, r.trx
),
hcp_months AS (
    SELECT e.hcp_key,
           e.year_month,
           SUM(e.trx)                                        AS trx_in_month,
           COUNT(*)                                          AS rx_days_in_month,
           COUNT(*) FILTER (WHERE e.called_in_prior_14d)     AS exposed_rx_days,
           BOOL_OR(e.called_in_prior_14d)                    AS was_called
    FROM rx_day_exposure e
    GROUP BY e.hcp_key, e.year_month
),
labelled AS (
    -- dim_hcp is joined on the surrogate key, so the decile attached is the one
    -- that was current when the scripts were written, not today's decile.
    SELECT m.hcp_key,
           m.year_month,
           m.trx_in_month,
           m.rx_days_in_month,
           m.exposed_rx_days,
           h.decile,
           CASE WHEN m.was_called THEN 'CALLED' ELSE 'NOT CALLED' END AS cohort
    FROM hcp_months m
    JOIN warehouse.dim_hcp h ON h.hcp_key = m.hcp_key
),
cohort_stats AS (
    SELECT CASE
               WHEN GROUPING(decile) = 1 THEN 'ALL DECILES'
               WHEN decile IS NULL       THEN 'DECILE UNKNOWN'
               ELSE 'DECILE ' || LPAD(decile::text, 2, '0')
           END                                            AS stratum,
           GROUPING(decile)                               AS is_national_row,
           cohort,
           COUNT(DISTINCT hcp_key)                        AS hcps,
           COUNT(*)                                       AS hcp_months,
           SUM(trx_in_month)                              AS total_trx,
           ROUND(AVG(trx_in_month), 2)                    AS avg_trx_per_hcp_month,
           ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY trx_in_month))::numeric, 2)
                                                          AS median_trx_per_hcp_month,
           ROUND(AVG(100.0 * exposed_rx_days / NULLIF(rx_days_in_month, 0)), 1)
                                                          AS pct_of_rx_days_exposed
    FROM labelled
    GROUP BY GROUPING SETS ((cohort), (decile, cohort))
)
SELECT stratum,
       cohort,
       hcps,
       hcp_months,
       total_trx,
       avg_trx_per_hcp_month,
       median_trx_per_hcp_month,
       pct_of_rx_days_exposed,
       -- The uncalled cohort inside the same stratum is the baseline; the window
       -- carries it onto the called row so the lift reads off a single line.
       ROUND(avg_trx_per_hcp_month
             - MAX(avg_trx_per_hcp_month) FILTER (WHERE cohort = 'NOT CALLED')
                   OVER (PARTITION BY stratum), 2)        AS trx_lift_vs_uncalled,
       ROUND(100.0 * avg_trx_per_hcp_month
             / NULLIF(MAX(avg_trx_per_hcp_month) FILTER (WHERE cohort = 'NOT CALLED')
                          OVER (PARTITION BY stratum), 0) - 100, 1)
                                                          AS lift_pct
FROM cohort_stats
ORDER BY is_national_row DESC, stratum, cohort;
