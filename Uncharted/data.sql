WITH stars_spend AS (
    SELECT
        st."userId",
        SUM(st.amount) AS star_spend
    FROM stars_transactions st
    GROUP BY st."userId"
),
resources AS (
    SELECT
        urt."userId",
        COALESCE(SUM(CASE WHEN urt."resourceType" = 'gold'
            THEN urt.amount END), 0) AS gold,
        COALESCE(SUM(CASE WHEN urt."resourceType" = 'greenStones'
            THEN urt.amount END), 0) AS green_stones,
        COALESCE(SUM(CASE WHEN urt."resourceType" = 'purpleStones'
            THEN urt.amount END), 0) AS purple_stones
    FROM users_resources_total urt
    GROUP BY urt."userId"
),
hero_stars AS (
    SELECT
        uh."userId",
        SUM(uh.stars) AS hero_stars
    FROM users_heroes uh
    GROUP BY uh."userId"
),
challenge_progress AS (
    SELECT
        uc."userId",
        MAX(CASE WHEN uc."isRaid" = false THEN uc."constellationType" END) AS max_normal_constellation,
        MAX(CASE WHEN uc."isRaid" = true  THEN uc."constellationType" END) AS max_raid_constellation
    FROM users_challenges uc
    GROUP BY uc."userId"
)

SELECT
    u.id AS "User ID",

    COALESCE(ss.star_spend, 0)  AS "Star Spend",          -- потраченные звезды
    COALESCE(hs.hero_stars, 0)  AS "Collection Power",    -- звезды по героям

    COALESCE(r.gold, 0)         AS "Gold",
    COALESCE(r.green_stones, 0) AS "Green Stones",
    COALESCE(r.purple_stones, 0)AS "Purple Stones",

    -- Прогресс по испытаниям
    cp.max_normal_constellation AS "Max Normal Constellation",
    cp.max_raid_constellation   AS "Max Raid Constellation"
    -- u."createdAt" as "Registration Date"

FROM users u
LEFT JOIN stars_spend        ss ON ss."userId" = u.id
LEFT JOIN resources          r  ON r."userId"  = u.id
LEFT JOIN hero_stars         hs ON hs."userId" = u.id
LEFT JOIN challenge_progress cp ON cp."userId" = u.id
WHERE
    u."createdAt" >= DATE '2025-12-11'
;
