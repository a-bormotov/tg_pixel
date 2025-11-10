-- data.sql — окно времени
WITH
win AS (
  SELECT
    TIMESTAMPTZ '2025-11-10 17:00:00+00' AS win_start,
    TIMESTAMPTZ '2025-11-17 17:00:00+00' AS win_end
),

/* События в окне */
ewin AS (
  SELECT *
  FROM events e, win
  WHERE e."createdAt" >= win.win_start
    AND e."createdAt" <  win.win_end
    AND lower(left(e."userId", 4)) <> 'line'   -- отсечь line*
    -- AND e."userId" = '988810706'           -- для точечного теста
    AND e."name" IN (
      'ClaimChallengesAction',
      'UnlockChallengeAction',
      'SpendGachaAction',
      'WatchAdsPostHookAction'
    )
),

/* 1) Claim/Unlock — прямые gold */
gold_claim_unlock AS (
  SELECT
    e."userId",
    SUM(
      COALESCE(NULLIF(e.payload::jsonb #>> '{output,gold,amount}','')::bigint, 0) +
      COALESCE(NULLIF(e.payload::jsonb #>> '{output,rewards,gold,amount}','')::bigint, 0)
    ) AS amt
  FROM ewin e
  WHERE e."name" IN ('ClaimChallengesAction','UnlockChallengeAction')
  GROUP BY e."userId"
),

/* 2) Реклама (суммы gold из payload) */
ads_only AS (
  SELECT
    e."userId",
    e."createdAt",
    COALESCE(NULLIF(e.payload::jsonb #>> '{input,rewards,gold,amount}','')::bigint, 0) AS amt
  FROM ewin e
  WHERE e."name" = 'WatchAdsPostHookAction'
),

/* 3) Ограничим историю подписок только нужными игроками */
uids AS (
  SELECT DISTINCT "userId"
  FROM ads_only
),

/* 4) Интервалы подписки из "vipHistory": [from, next_from) */
vip_ranges AS (
  SELECT
    v."userId",
    v."vipLevel",
    v."from" AS from_ts,
    LEAD(v."from") OVER (PARTITION BY v."userId" ORDER BY v."from") AS to_ts
  FROM "vipHistory" v
  JOIN uids u ON u."userId" = v."userId"
),

/* 5) Назначим каждому показу актуальный vipLevel (или vip0) */
ads_with_level AS (
  SELECT
    a."userId",
    a."createdAt",
    a.amt,
    COALESCE(r."vipLevel", 'vip0') AS vip_level
  FROM ads_only a
  LEFT JOIN vip_ranges r
    ON  r."userId" = a."userId"
    AND a."createdAt" >= r.from_ts
    AND (r.to_ts IS NULL OR a."createdAt" < r.to_ts)
),

/* 6) Порог на момент показа */
ads_with_threshold AS (
  SELECT
    "userId",
    "createdAt",
    amt,
    CASE vip_level
      WHEN 'vip3' THEN 1
      WHEN 'vip2' THEN 2
      WHEN 'vip1' THEN 3
      ELSE 5
    END AS threshold
  FROM ads_with_level
),

/* 7) Серии подряд одинаковых (amt, threshold) */
ads_series AS (
  SELECT
    a.*,
    (ROW_NUMBER() OVER (PARTITION BY "userId" ORDER BY "createdAt")
     - ROW_NUMBER() OVER (PARTITION BY "userId", amt, threshold ORDER BY "createdAt")) AS grp
  FROM ads_with_threshold a
),

/* 8) Агрегируем серию: считаем её длину и параметры */
ads_credits AS (
  SELECT
    "userId",
    grp,
    MAX(amt)       AS amt,
    MAX(threshold) AS threshold,
    COUNT(*)       AS cnt
  FROM ads_series
  GROUP BY "userId", grp
),

/* 9) Кредиты за рекламу: максимум ОДИН кредит на серию */
gold_watchads AS (
  SELECT
    "userId",
    SUM(CASE WHEN cnt >= threshold THEN amt ELSE 0 END)::bigint AS amt
  FROM ads_credits
  GROUP BY "userId"
),

/* 10) Общая сумма gold */
gold AS (
  SELECT "userId", SUM(amt)::bigint AS gold
  FROM (
    SELECT "userId", amt FROM gold_claim_unlock
    UNION ALL
    SELECT "userId", amt FROM gold_watchads
  ) x
  GROUP BY "userId"
),

/* 11) Дарклинги по гаче */
heroes AS (
  SELECT
    e."userId",
    COUNT(*) FILTER (WHERE (item->>'heroType') = 'darkling')  AS darkling_rare,
    COUNT(*) FILTER (WHERE (item->>'heroType') = 'darkling2') AS darkling_epic,
    COUNT(*) FILTER (WHERE (item->>'heroType') = 'darkling3') AS darkling_legendary
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

/* 12) Финальный скор: gold * (1 + Σ(darkling * %)) */
SELECT
  COALESCE(g."userId", h."userId") AS "userId",
  (COALESCE(g.gold, 0)::numeric) * (
      1
    + COALESCE(h.darkling_rare, 0)        * 0.01
    + COALESCE(h.darkling_epic, 0)        * 0.03
    + COALESCE(h.darkling_legendary, 0)   * 0.10
  ) AS score,
  COALESCE(g.gold, 0)            AS gold,
  COALESCE(h.darkling_rare, 0)   AS "darklingRare",
  COALESCE(h.darkling_epic, 0)   AS "darklingEpic",
  COALESCE(h.darkling_legendary, 0) AS "darklingLegendary"
FROM gold g
FULL OUTER JOIN heroes h
  ON g."userId" = h."userId"
ORDER BY score DESC, gold DESC;
