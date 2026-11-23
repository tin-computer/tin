-- ICP fit moves from a score between 0 and 1 to a rating from 1 to 5 (people_icp): 1 no fit,
-- 2 low fit, 3 worth contacting, 4 contact this week, 5 contact now. Existing values convert by
-- the old score's bands; none becomes 5, which the old score never judged. Tin's own values keep
-- their old definition, so they read as stale and the next run rates them again; a member's
-- value, and the run value it replaced, convert the same way and stay.
UPDATE record_values
SET value_number = CASE
        WHEN value_number < 0.3 THEN 1
        WHEN value_number < 0.6 THEN 2
        WHEN value_number < 0.8 THEN 3
        ELSE 4
    END
WHERE field = 'icp_fit' AND value_number IS NOT NULL;

UPDATE record_values
SET previous = jsonb_set(
    previous,
    '{number}',
    to_jsonb(CASE
        WHEN (previous->>'number')::double precision < 0.3 THEN 1
        WHEN (previous->>'number')::double precision < 0.6 THEN 2
        WHEN (previous->>'number')::double precision < 0.8 THEN 3
        ELSE 4
    END)
)
WHERE field = 'icp_fit' AND previous IS NOT NULL AND jsonb_typeof(previous->'number') = 'number';
