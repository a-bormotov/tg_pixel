insert into "vipHistory" ("userId", "vipLevel", "from")
select 
	e."userId", (e.payload::jsonb)->'input'->'invoicePayload'->>'slotType' as "vipLevel",  e."createdAt" as "from"
from events e
where
    e."name" = 'ProcessShopPurchaseAction'
    and e.payload::jsonb->'input'->'invoicePayload'->>'slotType' IN ('vip1','vip2','vip3')
	and e."createdAt" >= (now() - interval '3 days')
