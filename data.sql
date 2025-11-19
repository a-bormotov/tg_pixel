-- data.sql
-- DB1: шаблон. Питон подставляет {IDS} списком userId из DB2 (user_data.sql).
-- Для ручного теста можно временно вписать:
--   {IDS} → ('123456789'),('987654321')

WITH
win AS (
  SELECT
    TIMESTAMPTZ '2025-11-10 16:00:00+00' AS win_start,
    TIMESTAMPTZ '2025-11-26 16:00:00+00' AS win_end
),

-- Участники эвента: список userId, приходит из питона
participants AS (
  SELECT "userId"
  FROM (VALUES {IDS}) AS v("userId")
),

/* 1) События в окне по этим userId */
ewin AS (
  SELECT e.*
  FROM events e, win, participants p
  WHERE e."userId" = p."userId"
    AND e."createdAt" >= win.win_start
    AND e."createdAt" <  win.win_end
    AND lower(left(e."userId", 4)) <> 'line'
    AND e."name" IN (
      'ClaimChallengesAction',
      'UnlockChallengeAction',
      'SpendGachaAction',
      'WatchAdsPostHookAction'
    )
),

/* 2) Claim/Unlock — прямые purpleStones */
purple_claim_unlock AS (
  SELECT
    e."userId",
    SUM(
      COALESCE(NULLIF(e.payload::jsonb #>> '{output,purpleStones,amount}','')::bigint, 0) +
      COALESCE(NULLIF(e.payload::jsonb #>> '{output,rewards,purpleStones,amount}','')::bigint, 0)
    ) AS amt
  FROM ewin e
  WHERE e."name" IN ('ClaimChallengesAction','UnlockChallengeAction')
  GROUP BY e."userId"
),

/* 3) Реклама (суммы purpleStones из payload) */
ads_only AS (
  SELECT
    e."userId",
    e."createdAt",
    COALESCE(NULLIF(e.payload::jsonb #>> '{input,rewards,purpleStones,amount}','')::bigint, 0) AS amt
  FROM ewin e
  WHERE e."name" = 'WatchAdsPostHookAction'
),

/* 4) Ограничим историю подписок только нужными игроками */
uids AS (
  SELECT DISTINCT "userId"
  FROM ads_only
),

/* 5) Интервалы подписки из "vipHistory": [from, next_from) */
vip_ranges AS (
  SELECT
    v."userId",
    v."vipLevel",
    v."from" AS from_ts,
    LEAD(v."from") OVER (PARTITION BY v."userId" ORDER BY v."from") AS to_ts
  FROM "vipHistory" v
  JOIN uids u ON u."userId" = v."userId"
),

/* 6) Назначим каждому показу актуальный vipLevel (или vip0) */
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

/* 7) Порог на момент показа (логика та же, что в прошлый раз) */
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

/* 8) Серии подряд одинаковых (amt, threshold) */
ads_series AS (
  SELECT
    a.*,
    (ROW_NUMBER() OVER (PARTITION BY "userId" ORDER BY "createdAt")
     - ROW_NUMBER() OVER (PARTITION BY "userId", amt, threshold ORDER BY "createdAt")) AS grp
  FROM ads_with_threshold a
),

/* 9) Агрегируем серию */
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

/* 10) Кредиты за рекламу: максимум ОДИН кредит на серию */
purple_watchads AS (
  SELECT
    "userId",
    SUM(CASE WHEN cnt >= threshold THEN amt ELSE 0 END)::bigint AS amt
  FROM ads_credits
  GROUP BY "userId"
),

/* 11) Общая сумма purpleStones */
purple AS (
  SELECT "userId", SUM(amt)::bigint AS "purpleStones"
  FROM (
    SELECT "userId", amt FROM purple_claim_unlock
    UNION ALL
    SELECT "userId", amt FROM purple_watchads
  ) x
  GROUP BY "userId"
),

/* 12) Карты из SpendGachaAction — любые, по редкости.
   ПРЕДПОЛОЖЕНИЕ: редкость лежит в item->>'rarity'
   и значения: 'rare' / 'epic' / 'legendary'.
   Если у тебя другое поле или значения — поправь WHERE и FILTER'ы. */
cards AS (
  SELECT
    e."userId",
    COUNT(*) FILTER (WHERE (item->>'rarity') = 'rare')       AS "cardsRare",
    COUNT(*) FILTER (WHERE (item->>'rarity') = 'epic')       AS "cardsEpic",
    COUNT(*) FILTER (WHERE (item->>'rarity') = 'legendary')  AS "cardsLegendary"
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

/* 13) Финальный скор: purpleStones * (1 + Σ(cards * %)) */
SELECT
  p."userId",
  COALESCE(pr."purpleStones", 0)::bigint     AS "purpleStones",
  COALESCE(c."cardsRare", 0)                 AS "cardsRare",
  COALESCE(c."cardsEpic", 0)                 AS "cardsEpic",
  COALESCE(c."cardsLegendary", 0)            AS "cardsLegendary",
  ( COALESCE(c."cardsRare",0)
  + COALESCE(c."cardsEpic",0)
  + COALESCE(c."cardsLegendary",0) )         AS "heroesMatched",
  (COALESCE(pr."purpleStones", 0)::numeric) * (
      1
    + COALESCE(c."cardsRare", 0)      * 0.01
    + COALESCE(c."cardsEpic", 0)      * 0.03
    + COALESCE(c."cardsLegendary", 0) * 0.10
  ) AS score
FROM participants p
LEFT JOIN purple pr ON pr."userId" = p."userId"
LEFT JOIN cards  c  ON c."userId"  = p."userId"
ORDER BY score DESC, "purpleStones" DESC;
