-- data.sql — окно времени подставь при необходимости
WITH
win AS (
  SELECT
    TIMESTAMPTZ '2025-10-21 16:00:00+00' AS win_start,
    TIMESTAMPTZ '2025-10-29 16:00:00+00' AS win_end
),
/* Базовое окно событий */
ewin AS (
  SELECT *
  FROM events e, win
  WHERE e."createdAt" >= win.win_start
    AND e."createdAt" <  win.win_end
    AND lower(left(e."userId", 4)) <> 'line'      -- отсечь line-аккаунты
    -- AND e."userId" = '5282570639'              -- тест по одному игроку
    AND e."name" IN (
      'ClaimChallengesAction',
      'UnlockChallengeAction',
      'SpendGachaAction',
      'WatchAdsPostHookAction'
    )
),
/* 1) Claim/Unlock — зелёные сферы напрямую */
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
/* 2) Показ рекламы (сумма из payload) */
ads_only AS (
  SELECT
    e."userId",
    e."createdAt",
    COALESCE(NULLIF(e.payload::jsonb #>> '{input,rewards,greenStones,amount}','')::bigint, 0) AS amt
  FROM ewin e
  WHERE e."name" = 'WatchAdsPostHookAction'
),
/* 3) Список пользователей для сужения истории VIP */
uids AS ( SELECT DISTINCT "userId" FROM ads_only ),

/* 4) Интервалы подписок из "vipHistory": [from, next_from) */
vip_ranges AS (
  SELECT
    v."userId",
    v."vipLevel",
    v."from" AS from_ts,
    LEAD(v."from") OVER (PARTITION BY v."userId" ORDER BY v."from") AS to_ts
  FROM "vipHistory" v
  JOIN uids u ON u."userId" = v."userId"
),
/* 5) Назначаем каждому показу актуальный vipLevel (или vip0) */
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
/* 6) Порог по уровню */
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
/* 7) Серии подряд одинаковых (amt, threshold) — разность row_number */
ads_series AS (
  SELECT
    a.*,
    (ROW_NUMBER() OVER (PARTITION BY "userId" ORDER BY "createdAt")
     - ROW_NUMBER() OVER (PARTITION BY "userId", amt, threshold ORDER BY "createdAt")) AS grp
  FROM ads_with_threshold a
),
/* 8) Сначала считаем агрегаты ВНУТРИ серии */
ads_credits AS (
  SELECT
    "userId",
    grp,
    MAX(amt)        AS amt,
    MAX(threshold)  AS threshold,
    COUNT(*)        AS cnt
  FROM ads_series
  GROUP BY "userId", grp
),
/* 9) Потом суммируем кредиты по пользователю */
green_watchads AS (
  SELECT
    "userId",
    SUM( (cnt / threshold) * amt )::bigint AS amt
  FROM ads_credits
  GROUP BY "userId"
),
/* 10) Общая сумма green */
green AS (
  SELECT "userId", SUM(amt)::bigint AS green
  FROM (
    SELECT "userId", amt FROM green_claim_unlock
    UNION ALL
    SELECT "userId", amt FROM green_watchads
  ) x
  GROUP BY "userId"
),
/* 11) Гача-герои (+1% за каждый из списка) */
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
