WITH ids(id, ord) AS (
  VALUES %s   -- сюда скрипт подставит ('id1',1),('id2',2),...
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
  u."createdAt" < TIMESTAMP '2025-10-11 00:00:00'   -- фильтр по времени создания
  AND lower(left(ids.id, 4)) <> 'line'              -- исключаем id с префиксом "line"
ORDER BY ids.ord;
