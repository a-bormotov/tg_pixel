-- data.sql
-- DB1: events + "vipHistory"
-- Окно эвента: 2026-01-13 16:00:00 UTC — 2026-01-19 16:00:00 UTC
-- Ресурс: greenStones
-- Скор: greenStones * (1 + 1%*dinoRare + 3%*dinoEpic + 10%*dinoLegendary)
-- Карты: SpendGachaAction (output-array), персонаж по item.heroType:
--   dinoRare / dinoEpic / dinoLegendary
-- ВАЖНО:
--  - серии рекламы считаются внутри "пакетов" после Claim/Unlock с greenStones,
--  - подписка действует 31 день с момента покупки,
--    to_ts = min(from + 31 days, next_from).

WITH
win AS (
  SELECT
    TIMESTAMPTZ '2026-01-13 16:00:00+00' AS win_start,
    TIMESTAMPTZ '2026-01-20 16:00:00+00' AS win_end
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

/* 1.1) Пакеты: Claim/Unlock, которые реально дают greenStones */
ewin_with_pack AS (
  SELECT
    e.*,
    SUM(
      CASE
        WHEN e."name" IN ('ClaimChallengesAction','UnlockChallengeAction')
         AND (
           NULLIF(e.payload::jsonb #>> '{output,greenStones,amount}','') IS NOT NULL
           OR NULLIF(e.payload::jsonb #>> '{output,rewards,greenStones,amount}','') IS NOT NULL
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

/* 2) Claim/Unlock — прямые greenStones */
green_claim_unlock AS (
  SELECT
    e."userId",
    SUM(
      COALESCE(NULLIF(e.payload::jsonb #>> '{output,greenStones,amount}','')::bigint, 0) +
      COALESCE(NULLIF(e.payload::jsonb #>> '{output,rewards,greenStones,amount}','')::bigint, 0)
    ) AS amt
  FROM ewin_with_pack e
  WHERE e."name" IN ('ClaimChallengesAction','UnlockChallengeAction')
  GROUP BY e."userId"
),

/* 3) Реклама (суммы greenStones из payload) */
ads_only AS (
  SELECT
    e."userId",
    e."createdAt",
    e.pack_id,
    COALESCE(
      NULLIF(e.payload::jsonb #>> '{input,rewards,greenStones,amount}','')::bigint,
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
green_watchads AS (
  SELECT
    "userId",
    SUM(
      CASE WHEN cnt >= threshold THEN amt ELSE 0 END
    )::bigint AS amt
  FROM ads_credits
  GROUP BY "userId"
),

/* 11) Общая сумма greenStones */
green AS (
  SELECT "userId", SUM(amt)::bigint AS "greenStones"
  FROM (
    SELECT "userId", amt FROM green_claim_unlock
    UNION ALL
    SELECT "userId", amt FROM green_watchads
  ) x
  GROUP BY "userId"
),

/* 12) Dino по гаче */
heroes AS (
  SELECT
    e."userId",
    COUNT(*) FILTER (WHERE (item->>'heroType') = 'dinoRare')       AS dino_rare,
    COUNT(*) FILTER (WHERE (item->>'heroType') = 'dinoEpic')       AS dino_epic,
    COUNT(*) FILTER (WHERE (item->>'heroType') = 'dinoLegendary')  AS dino_legendary
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

/* 13) Финальный скор: greenStones * (1 + Σ(dino * %)) */
SELECT
  COALESCE(gs."userId", h."userId") AS "userId",
  (COALESCE(gs."greenStones", 0)::numeric) * (
      1
    + COALESCE(h.dino_rare, 0)      * 0.01
    + COALESCE(h.dino_epic, 0)      * 0.03
    + COALESCE(h.dino_legendary, 0) * 0.10
  ) AS score,
  COALESCE(gs."greenStones", 0)    AS "greenStones",
  COALESCE(h.dino_rare, 0)         AS "dinoRare",
  COALESCE(h.dino_epic, 0)         AS "dinoEpic",
  COALESCE(h.dino_legendary, 0)    AS "dinoLegendary"
FROM green gs
FULL OUTER JOIN heroes h
  ON gs."userId" = h."userId"
ORDER BY score DESC, "greenStones" DESC;