-- 10 последних событий — быстрый и безопасный тест-запрос
SELECT e."id", e."userId", e."name", e."createdAt"
FROM events e
ORDER BY e."createdAt" DESC
LIMIT 10;
