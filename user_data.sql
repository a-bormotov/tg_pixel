-- user_data.sql
-- DB2: выбрать участников эвента (только vip2 и vip3) и их имена

SELECT
  ua."userId" AS "userId",
  ua."vipLevel" AS "vipLevel",
  CASE
    WHEN u.username IS NULL OR u.username = '' OR u.username = 'Secret Dino'
      THEN ua."userId"::text
    ELSE u.username
  END AS "username"
FROM users_additional ua
LEFT JOIN users u
  ON u.id::text = ua."userId"::text
WHERE ua."vipLevel" IN ('vip2', 'vip3')
ORDER BY ua."userId";
