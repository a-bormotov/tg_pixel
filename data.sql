-- data.sql
-- DB2: events + "vipHistory" (table in same DB as before)
-- Window: 2026-02-05 16:00:00 UTC — 2026-02-12 16:00:00 UTC
-- Resource: points
-- Score: points + (rare*1 + epic*5 + legendary*50)
-- Gacha: SpendGachaAction output-array, rarity field:
--   rarity=0 rare, rarity=1 epic, rarity=2 legendary
-- IMPORTANT (unchanged):
--  - ad series counted inside "packs" after Claim/Unlock with points
--  - subscription valid 31 days from purchase,
--    to_ts = min(from + 31 days, next_from).

WITH
win AS (
  SELECT
    TIMESTAMPTZ '2026-02-05 16:00:00+00' AS win_start,
    TIMESTAMPTZ '2026-02-12 16:00:00+00' AS win_end
),

/* 1) Events in window (exclude userId starting with 'line') */
ewin AS (
  SELECT e.*
  FROM events e
  CROSS JOIN win
  WHERE e."createdAt" >= win.win_start
    AND e."createdAt" <  win.win_end
    AND lower(left(e."userId", 4)) <> 'line'
    AND e."name" IN (
      'ClaimChallengesAction',
      'UnlockChallengeAction',
      'SpendGachaAction',
      'WatchAdsPostHookAction'
    )
),

/* 1.1) Packs: Claim/Unlock that actually give points */
ewin_with_pack AS (
  SELECT
    e.*,
    SUM(
      CASE
        WHEN e."name" IN ('ClaimChallengesAction','UnlockChallengeAction')
         AND (
           NULLIF(e.payload::jsonb #>> '{output,points,amount}','') IS NOT NULL
           OR NULLIF(e.payload::jsonb #>> '{output,rewards,points,amount}','') IS NOT NULL
         )
        THEN 1
        ELSE 0
      END
    ) OVER (
      PARTITION BY e."userId"
      ORDER BY e."createdAt"
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS pack_id
  FROM ewin e
),

/* 2) Claim/Unlock — direct points */
points_claim_unlock AS (
  SELECT
    e."userId",
    SUM(
      COALESCE(NULLIF(e.payload::jsonb #>> '{output,points,amount}','')::bigint, 0) +
      COALESCE(NULLIF(e.payload::jsonb #>> '{output,rewards,points,amount}','')::bigint, 0)
    ) AS amt
  FROM ewin_with_pack e
  WHERE e."name" IN ('ClaimChallengesAction','UnlockChallengeAction')
  GROUP BY e."userId"
),

/* 3) Ads (points amount from payload) */
ads_only AS (
  SELECT
    e."userId",
    e."createdAt",
    e.pack_id,
    COALESCE(
      NULLIF(e.payload::jsonb #>> '{input,rewards,points,amount}','')::bigint,
      0
    ) AS amt
  FROM ewin_with_pack e
  WHERE e."name" = 'WatchAdsPostHookAction'
),

/* 4) Limit vip history only to users who have ads */
uids AS (
  SELECT DISTINCT "userId"
  FROM ads_only
),

/* 5) Subscription ranges from "vipHistory" (unchanged) */
vip_ranges AS (
  WITH base AS (
    SELECT
      v."userId"   AS "userId",
      v."vipLevel" AS "vipLevel",
      v."from"     AS from_ts,
      v."from" + INTERVAL '31 days' AS expires_ts,
      LEAD(v."from") OVER (
        PARTITION BY v."userId"
        ORDER BY v."from"
      ) AS next_from
    FROM "vipHistory" v
    JOIN uids u ON u."userId" = v."userId"
  )
  SELECT
    "userId",
    "vipLevel",
    from_ts,
    CASE
      WHEN next_from IS NULL
        THEN expires_ts
      ELSE LEAST(expires_ts, next_from)
    END AS to_ts
  FROM base
),

/* 6) Assign current vipLevel per ad (or vip0) */
ads_with_level AS (
  SELECT
    a."userId",
    a."createdAt",
    a.pack_id,
    a.amt,
    COALESCE(vr."vipLevel", 'vip0') AS vip_level
  FROM ads_only a
  LEFT JOIN vip_ranges vr
    ON  vr."userId" = a."userId"
    AND a."createdAt" >= vr.from_ts
    AND a."createdAt" <  vr.to_ts
),

/* 7) Threshold per ad view */
ads_with_threshold AS (
  SELECT
    "userId",
    "createdAt",
    pack_id,
    amt,
    CASE vip_level
      WHEN 'vip3' THEN 1
      WHEN 'vip2' THEN 2
      WHEN 'vip1' THEN 3
      ELSE 5
    END AS threshold
  FROM ads_with_level
),

/* 8) Runs of same (amt, threshold) inside same pack_id */
ads_series AS (
  SELECT
    a.*,
    (
      ROW_NUMBER() OVER (
        PARTITION BY "userId", pack_id
        ORDER BY "createdAt"
      )
      -
      ROW_NUMBER() OVER (
        PARTITION BY "userId", pack_id, amt, threshold
        ORDER BY "createdAt"
      )
    ) AS grp
  FROM ads_with_threshold a
),

/* 9) Aggregate each run */
ads_credits AS (
  SELECT
    "userId",
    pack_id,
    grp,
    MAX(amt)       AS amt,
    MAX(threshold) AS threshold,
    COUNT(*)       AS cnt
  FROM ads_series
  GROUP BY "userId", pack_id, grp
),

/* 10) Credits from ads: max ONE credit per run inside pack */
points_watchads AS (
  SELECT
    "userId",
    SUM(
      CASE WHEN cnt >= threshold THEN amt ELSE 0 END
    )::bigint AS amt
  FROM ads_credits
  GROUP BY "userId"
),

/* 11) Total points (claim/unlock + ads credits) */
points AS (
  SELECT "userId", SUM(amt)::bigint AS "points"
  FROM (
    SELECT "userId", amt FROM points_claim_unlock
    UNION ALL
    SELECT "userId", amt FROM points_watchads
  ) x
  GROUP BY "userId"
),

/* 12) Gacha: rarity counts + points from cards */
gacha AS (
  SELECT
    e."userId",
    COUNT(*) FILTER (WHERE rarity_num = 0) AS rare,
    COUNT(*) FILTER (WHERE rarity_num = 1) AS epic,
    COUNT(*) FILTER (WHERE rarity_num = 2) AS legendary,
    SUM(
      CASE rarity_num
        WHEN 0 THEN 5
        WHEN 1 THEN 20
        WHEN 2 THEN 80
        ELSE 0
      END
    )::bigint AS gacha_points
  FROM ewin e
  CROSS JOIN LATERAL jsonb_array_elements(
    CASE
      WHEN jsonb_typeof(e.payload::jsonb->'output') = 'array'
        THEN e.payload::jsonb->'output'
      ELSE '[]'::jsonb
    END
  ) AS item
  CROSS JOIN LATERAL (
    SELECT
      CASE
        WHEN (item ? 'rarity')
         AND (item->>'rarity') ~ '^\d+$'
          THEN (item->>'rarity')::int
        ELSE NULL
      END AS rarity_num
  ) r
  WHERE e."name" = 'SpendGachaAction'
  GROUP BY e."userId"
)

/* 13) Final score */
SELECT
  COALESCE(p."userId", g."userId") AS "userId",
  (COALESCE(p."points", 0) + COALESCE(g.gacha_points, 0))::numeric AS score,
  COALESCE(p."points", 0) AS "points",
  COALESCE(g.rare, 0) AS "rare",
  COALESCE(g.epic, 0) AS "epic",
  COALESCE(g.legendary, 0) AS "legendary"
FROM points p
FULL OUTER JOIN gacha g
  ON p."userId" = g."userId"
ORDER BY score DESC, "points" DESC;
