-- 0009: open rows of a series carry the series' card (#60). A save that changed a series' amount or schedule
-- together with its card left the rows edited by hand on the old card, so card transfers and the "not scheduled"
-- alert missed them. One-time repair; cleared and removed rows keep the card they were paid with.

UPDATE ledger_rows
SET card = (SELECT s.card FROM series s WHERE s.household_id = ledger_rows.household_id AND s.id = ledger_rows.series_id)
WHERE state = 'open' AND series_id IS NOT NULL
  AND EXISTS (SELECT 1 FROM series s WHERE s.household_id = ledger_rows.household_id AND s.id = ledger_rows.series_id
              AND s.card IS NOT ledger_rows.card);
