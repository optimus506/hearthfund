-- 0015: "Every N" now also applies to monthly items (every N months, D-099). Until now it was stored but ignored for
-- every cadence except weekly, so an item that carries a leftover value keeps its present dates.
UPDATE series
   SET schedule_json = json_set(schedule_json, '$.interval', 1)
 WHERE json_extract(schedule_json, '$.cadence') <> 'weekly'
   AND COALESCE(json_extract(schedule_json, '$.interval'), 1) <> 1;
