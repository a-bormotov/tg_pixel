WITH ids(id, ord) AS (
  VALUES %s
)
SELECT
  ids.id AS "userId",
  CASE
    WHEN u.username IS NULL OR u.username = '' OR u.username = 'Secret Dino'
      THEN ids.id
    ELSE u.username
  END AS "username",
  ids.ord
FROM ids
JOIN users u
  ON u.id::text = ids.id
WHERE
  u."createdAt" < TIMESTAMP '2025-10-11 00:00:00'
  AND lower(left(ids.id, 4)) <> 'line'
ORDER BY ids.ord;
