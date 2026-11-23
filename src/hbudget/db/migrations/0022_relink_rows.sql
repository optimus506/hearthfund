-- 0022: rows an item left behind when it was deleted (kept because they were edited by hand or cleared) are linked
-- again to an item that came back with the same id, as a bill-file merge can bring it (core.ledger.adopt_rows does
-- the same from now on). Unlinked, payee rules naming the item never matched them. Amounts and dates are kept.
UPDATE ledger_rows
SET series_id = substr(id, 1, instr(id, ':') - 1)
WHERE series_id IS NULL
  AND nominal_date IS NOT NULL
  AND instr(id, ':') > 1
  AND substr(id, instr(id, ':') + 1) = nominal_date
  AND EXISTS (SELECT 1 FROM series s
              WHERE s.household_id = ledger_rows.household_id
                AND s.id = substr(ledger_rows.id, 1, instr(ledger_rows.id, ':') - 1)
                AND s.kind = ledger_rows.kind);
