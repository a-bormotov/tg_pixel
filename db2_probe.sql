-- Берём 20 последних событий по createdAt.
-- Это быстрый запрос: использует индекс по "createdAt" (если он есть) и не делает COUNT по всей таблице.
SELECT
  e."id",
  e."userId",
  e."name",
  e."createdAt"
FROM events e
ORDER BY e."createdAt" DESC
LIMIT 20;
