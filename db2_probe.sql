-- Быстрый тест: 20 последних событий
SELECT
  e."id",
  e."userId",
  e."name",
  e."createdAt"
FROM events e
ORDER BY e."createdAt" DESC
LIMIT 20;
