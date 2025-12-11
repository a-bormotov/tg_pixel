-- data.sql
-- DB1: events + "vipHistory"
-- Окно эвента: 2025-12-08 16:00:00 UTC — 2025-12-15 16:00:00 UTC
-- Ресурс: gold
-- Скор: gold * (1 + 10% * cardsLegendary)
-- Карты: любые из output (SpendGachaAction), редкость по полю rarity:
--   rarity = 0 -> обычные/rare
--   rarity = 1 -> epic
--   rarity >= 2 -> legendary
-- ВАЖНО:
--  - серии рекламы считаются внутри "пакетов" после Claim/Unlock с gold,
--  - подписка действует 31 день с момента покупки,
--    to_ts = min(from + 31 days, next_from).

WITH
win AS (
  SELECT
    TIMESTAMPTZ '2025-12-08 16:00:00+00' AS win_start,
    TIMESTAMPTZ '2025-12-15 16:00:00+00' AS win_end
),

/* 1) События в окне (для всех, кроме userId, начинающихся с 'line') */
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

/* 1.1) Пакеты: Claim/Unlock, которые реально дают gold */
ewin_with_pack AS (
  SELECT
    e.*,
    SUM(
      CASE
        WHEN e."name" IN ('ClaimChallengesAction','UnlockChallengeAction')
         AND (
           NULLIF(e.payload::jsonb #>> '{output,gold,amount}','') IS NOT NULL
           OR NULLIF(e.payload::jsonb #>> '{output,rewards,gold,amount}','') IS NOT NULL
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

/* 2) Claim/Unlock — прямые gold */
gold_claim_unlock AS (
  SELECT
    e."userId",
    SUM(
      COALESCE(NULLIF(e.payload::jsonb #>> '{output,gold,amount}','')::bigint, 0) +
      COALESCE(NULLIF(e.payload::jsonb #>> '{output,rewards,gold,amount}','')::bigint, 0)
    ) AS amt
  FROM ewin_with_pack e
  WHERE e."name" IN ('ClaimChallengesAction','UnlockChallengeAction')
  GROUP BY e."userId"
),

/* 3) Реклама (суммы gold из payload) */
ads_only AS (
  SELECT
    e."userId",
    e."createdAt",
    e.pack_id,
    COALESCE(
      NULLIF(e.payload::jsonb #>> '{input,rewards,gold,amount}','')::bigint,
      0
    ) AS amt
  FROM ewin_with_pack e
  WHERE e."name" = 'WatchAdsPostHookAction'
),

/* 4) Ограничим историю подписок только нужными игроками (те, у кого есть реклама) */
uids AS (
  SELECT DISTINCT "userId"
  FROM ads_only
),

/* 5) Интервалы подписки из "vipHistory":
      from_ts ... to_ts, где
      expires_ts = from + 31 days,
      next_from = время следующей покупки,
      to_ts = min(expires_ts, next_from) или expires_ts, если next_from нет. */
vip_ranges AS (
  WITH base AS (
    SELECT
      v."userId",
      v."vipLevel",
      v."from" AS from_ts,
      v."from" + INTERVAL '31 days' AS expires_ts,
      LEAD(v."from") OVER (
        PARTITION BY v."userId"
        ORDER BY v."from"
      ) AS next_from
    FROM "vipHistory" v
    JOIN uids u ON u."userId" = v."userId"
  )
  SELECT
    userId,
    vipLevel,
    from_ts,
    CASE
      WHEN next_from IS NULL
        THEN expires_ts
      ELSE LEAST(expires_ts, next_from)
    END AS to_ts
  FROM base
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
    AND a."createdAt" <  vr.to_ts
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
gold_watchads AS (
  SELECT
    "userId",
    SUM(
      CASE WHEN cnt >= threshold THEN amt ELSE 0 END
    )::bigint AS amt
  FROM ads_credits
  GROUP BY "userId"
),

/* 11) Общая сумма gold */
gold AS (
  SELECT "userId", SUM(amt)::bigint AS gold
  FROM (
    SELECT "userId", amt FROM gold_claim_unlock
    UNION ALL
    SELECT "userId", amt FROM gold_watchads
  ) x
  GROUP BY "userId"
),

/* 12) Карты из SpendGachaAction — по rarity:
      0 = обычные/rare, 1 = epic, 2+ = legendary */
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

/* 13) Финальный скор: gold * (1 + 10% * cardsLegendary)
   (редкие/эпические ни на что не влияют, но остаются в сырой статистике) */
SELECT
  COALESCE(g."userId", c."userId")        AS "userId",
  COALESCE(g.gold, 0)::bigint            AS gold,
  COALESCE(c."cardsRare", 0)             AS "cardsRare",
  COALESCE(c."cardsEpic", 0)             AS "cardsEpic",
  COALESCE(c."cardsLegendary", 0)        AS "cardsLegendary",
  (
    COALESCE(c."cardsRare", 0)
  + COALESCE(c."cardsEpic", 0)
  + COALESCE(c."cardsLegendary", 0)
  )                                      AS "heroesMatched",
  (COALESCE(g.gold, 0)::numeric) * (
      1
    + COALESCE(c."cardsLegendary", 0) * 0.10
  )                                      AS score
FROM gold g
FULL OUTER JOIN cards c
  ON g."userId" = c."userId"
ORDER BY score DESC, gold DESC;
