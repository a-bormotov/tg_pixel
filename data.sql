-- data.sql — окно времени подставь при необходимости
WITH ewin AS (
  SELECT *
  FROM events
  WHERE "createdAt" >= TIMESTAMPTZ '2025-10-21 16:00:00+00'
    AND "createdAt" <  TIMESTAMPTZ '2025-10-29 16:00:00+00'
	AND lower(left("userId", 4)) <> 'line'
    AND "name" IN (
      'ClaimChallengesAction',
      'UnlockChallengeAction',
      'SpendGachaAction',
      'WatchAdsPostHookAction'
    )
),
/* 1) Claim/Unlock — green */
green_claim_unlock AS (
  SELECT
    e."userId",
    SUM(
      COALESCE(NULLIF(e.payload::jsonb #>> '{output,greenStones,amount}','')::bigint, 0) +
      COALESCE(NULLIF(e.payload::jsonb #>> '{output,rewards,greenStones,amount}','')::bigint, 0)
    ) AS amt
  FROM ewin e
  WHERE e."name" IN ('ClaimChallengesAction','UnlockChallengeAction')
  GROUP BY e."userId"
),
/* 2) WatchAdsPostHookAction — не более 5 подряд одинаковых amt */
wa_raw AS (
  SELECT
    e."userId",
    e."createdAt",
    COALESCE(NULLIF(e.payload::jsonb #>> '{input,rewards,greenStones,amount}','')::bigint, 0) AS amt
  FROM ewin e
  WHERE e."name" = 'WatchAdsPostHookAction'
),
wa_grouped AS (
  SELECT
    w.*,
    CASE
      WHEN w.amt = LAG(w.amt) OVER (PARTITION BY w."userId" ORDER BY w."createdAt")
        THEN 0 ELSE 1
    END AS is_new_group
  FROM wa_raw w
),
wa_with_grp AS (
  SELECT
    g.*,
    SUM(is_new_group) OVER (PARTITION BY g."userId" ORDER BY g."createdAt") AS grp
  FROM wa_grouped g
),
wa_capped AS (
  SELECT *
  FROM (
    SELECT
      w.*,
      ROW_NUMBER() OVER (PARTITION BY w."userId", w.grp ORDER BY w."createdAt") AS rn
    FROM wa_with_grp w
  ) z
  WHERE z.rn <= 5
),
green_watchads AS (
  SELECT "userId", SUM(amt) AS amt
  FROM wa_capped
  GROUP BY "userId"
),
/* 3) Общая сумма green */
green AS (
  SELECT
    COALESCE(c."userId", a."userId") AS "userId",
    COALESCE(c.amt, 0) + COALESCE(a.amt, 0) AS green
  FROM green_claim_unlock c
  FULL JOIN green_watchads a
    ON c."userId" = a."userId"
),
/* 4) Гача-герои (+1% за каждый из списка) */
heroes AS (
  SELECT
    e."userId",
    COUNT(*) FILTER (
      WHERE (item->>'heroType') IN (
        'dinoRare','dinoEpic','dinoLegendary',
        'mazhikRare','mazhikEpic','mazhikLegendary',
        'reinaRare','reinaEpic','reinaLegendary',
        'zillaRare','zillaEpic','zillaLegendary'
      )
    ) AS heroes_matched
  FROM ewin e
  CROSS JOIN LATERAL jsonb_array_elements(
    CASE
      WHEN jsonb_typeof(e.payload::jsonb->'output') = 'array'
        THEN e.payload::jsonb->'output'
      ELSE '[]'::jsonb
    END
  ) AS item
  WHERE e."name" = 'SpendGachaAction'
  GROUP BY e."userId"
)
SELECT
  COALESCE(g."userId", h."userId")                                              AS "userId",
  (COALESCE(g.green, 0)::numeric) * (1 + COALESCE(h.heroes_matched, 0) * 0.01)  AS score,
  COALESCE(g.green, 0)                                                          AS green,
  COALESCE(h.heroes_matched, 0)                                                 AS "heroesMatched"
FROM green g
FULL OUTER JOIN heroes h
  ON g."userId" = h."userId"
ORDER BY score DESC, green DESC, "heroesMatched" DESC;
