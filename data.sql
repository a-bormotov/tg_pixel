-- data.sql
-- DB1: events + "vipHistory"
-- Шаблон: питон подставляет {IDS} списком userId из DB1 (user_data.sql).
-- Окно эвента: 2025-11-20 16:00:00 UTC — 2025-11-26 16:00:00 UTC
-- Ресурс: purpleStones
-- Скор: purpleStones * (1 + 1% * rare + 3% * epic + 10% * legendary)
-- Карты: любые из output (SpendGachaAction), редкость по полю rarity (0/1/2+).
-- ВАЖНО: серии рекламы считаются внутри "пакетов" после Claim/Unlock с фиолетом.

WITH
win AS (
  SELECT
    TIMESTAMPTZ '2025-11-20 16:00:00+00' AS win_start,
    TIMESTAMPTZ '2025-11-26 16:00:00+00' AS win_end
),

-- Участники эвента: список userId приходит из питона
participants AS (
  SELECT "userId"
  FROM (VALUES {IDS}) AS v("userId")
),

/* 1) События в окне по этим userId */
ewin AS (
  SELECT e.*
  FROM events e
  JOIN participants p ON p."userId" = e."userId"
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

/* 1.1) Пакеты: счётчик Claim/Unlock, которые реально дают purpleStones */
ewin_with_pack AS (
  SELECT
    e.*,
    SUM(
      CASE
        WHEN e."name" IN ('ClaimChallengesAction','UnlockChallengeAction')
         AND (
           NULLIF(e.payload::jsonb #>> '{output,purpleStones,amount}','') IS NOT NULL
           OR NULLIF(e.payload::jsonb #>> '{output,rewards,purpleStones,amount}','') IS NOT NULL
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

/* 2) Claim/Unlock — прямые purpleStones */
purple_claim_unlock AS (
  SELECT
    e."userId",
    SUM(
      COALESCE(NULLIF(e.payload::jsonb #>> '{output,purpleStones,amount}','')::bigint, 0) +
      COALESCE(NULLIF(e.payload::jsonb #>> '{output,rewards,purpleStones,amount}','')::bigint, 0)
    ) AS amt
  FROM ewin_with_pack e
  WHERE e."name" IN ('ClaimChallengesAction','UnlockChallengeAction')
  GROUP BY e."userId"
),

/* 3) Реклама (суммы purpleStones из payload) */
ads_only AS (
  SELECT
    e."userId",
    e."createdAt",
    e.pack_id,
    COALESCE(
      NULLIF(e.payload::jsonb #>> '{input,rewards,purpleStones,amount}','')::bigint,
      0
    ) AS amt
  FROM ewin_with_pack e
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
    a.pack_id,
    a.amt,
    COALESCE(vr."vipLevel", 'vip0') AS vip_level
  FROM ads_only a
  LEFT JOIN vip_ranges vr
    ON  vr."userId" = a."userId"
    AND a."createdAt" >= vr.from_ts
    AND (vr.to_ts IS NULL OR a."createdAt" < vr.to_ts)
),

/* 7) Порог на момент показа */
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

/* 8) Серии подряд одинаковых (amt, threshold) ВНУТРИ ОДНОГО pack_id */
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

/* 9) Агрегируем серию */
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

/* 10) Кредиты за рекламу: максимум ОДИН кредит на серию внутри пакета */
purple_watchads AS (
  SELECT
    "userId",
    SUM(
      CASE WHEN cnt >= threshold THEN amt ELSE 0 END
    )::bigint AS amt
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

/* 12) Карты из SpendGachaAction — любые, по числовому rarity:
      0 = rare, 1 = epic, 2+ = legendary */
cards AS (
  SELECT
    e."userId",
    COUNT(*) FILTER (
      WHERE (item->>'rarity')::int = 0
    ) AS "cardsRare",
    COUNT(*) FILTER (
      WHERE (item->>'rarity')::int = 1
    ) AS "cardsEpic",
    COUNT(*) FILTER (
      WHERE (item->>'rarity')::int >= 2
    ) AS "cardsLegendary"
  FROM ewin_with_pack e
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
