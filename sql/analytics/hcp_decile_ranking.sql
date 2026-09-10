-- ============================================================================
-- hcp_decile_ranking.sql
--
-- BUSINESS QUESTION
--   Inside each territory, which prescribers actually drive our brand volume,
--   and which decile does each of them fall into?
--
-- WHAT A BRAND MANAGER DOES WITH IT
--   This is the call plan. Decile 1 is the top 10% of prescribers in a
--   territory and is where the rep's limited call capacity has to go; deciles
--   8-10 get email/e-detailing instead of a face-to-face visit. The ranking is
--   computed WITHIN territory, not nationally, because a rep can only call on
--   the doctors in their own geography -- a "decile 3" doctor in a dense metro
--   territory may out-write a "decile 1" doctor in a rural one, and the rep in
--   the rural territory still has to call on their best doctors.
--   Comparing calculated_decile against dim_hcp.decile (the decile the CRM
--   thinks the HCP is in) is the quickest way to find stale segmentation.
--
-- GRAIN      one row per HCP surrogate key per territory
-- MEASURE    total_trx = lifetime own-brand TRx (competitor products excluded)
--
-- NOTES
--   * Joins dim_hcp on hcp_key (the surrogate), not hcp_id. Under SCD Type 2 an
--     HCP who changed territory has more than one row, and each fact points at
--     the row that was current when the script was written -- so volume stays
--     credited to the territory that earned it.
--   * NTILE splits each territory into 10 equal-sized buckets; RANK gives the
--     exact within-territory position and ties share a rank.
--
-- RUN
--   psql postgresql://rxinsight:rxinsight@localhost:5544/rxinsight \
--        -f sql/analytics/hcp_decile_ranking.sql
-- ============================================================================

WITH hcp_volume AS (
    SELECT f.hcp_key, f.territory_key, SUM(f.trx_count) AS total_trx
    FROM warehouse.fact_prescriptions f
    JOIN warehouse.dim_product p USING (product_key)
    WHERE p.is_competitor = FALSE
    GROUP BY f.hcp_key, f.territory_key
)
SELECT h.hcp_id, h.full_name, h.specialty, t.territory_name, v.total_trx,
       NTILE(10) OVER (PARTITION BY v.territory_key ORDER BY v.total_trx DESC) AS calculated_decile,
       RANK()    OVER (PARTITION BY v.territory_key ORDER BY v.total_trx DESC) AS territory_rank
FROM hcp_volume v
JOIN warehouse.dim_hcp h ON h.hcp_key = v.hcp_key
JOIN warehouse.dim_territory t ON t.territory_key = v.territory_key
ORDER BY t.territory_name, territory_rank;
