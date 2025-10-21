-- data.sql — окно времени подставь при необходимости
WITH ewin AS (
  SELECT *
  FROM events
  WHERE "createdAt" >= TIMESTAMPTZ '2025-10-21 16:00:00+00'
    AND "createdAt" <  TIMESTAMPTZ '2025-10-29 16:00:00+00'
    AND "name" IN ('ClaimChallengesAction','UnlockChallengeAction','SpendGachaAction')
),
-- Сумма greenStones из двух путей в payload:
-- ClaimChallengesAction:   payload.output.greenStones.amount
-- UnlockChallengeAction:   payload.output.rewards.greenStones.amount
green AS (
  SELECT
    e."userId",
    SUM(
      COALESCE(NULLIF(e.payload::jsonb #>> '{output,greenStones,amount}','')::bigint, 0) +
      COALESCE(NULLIF(e.payload::jsonb #>> '{output,rewards,greenStones,amount}','')::bigint, 0)
    ) AS green
  FROM ewin e
  WHERE e."name" IN ('ClaimChallengesAction','UnlockChallengeAction')
  GROUP BY e."userId"
),
-- Карточки из SpendGachaAction: +1% за каждую карточку,
-- если её heroType входит в список ниже.
-- Твой пример payload — это массив в output, поэтому распаковываем его.
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