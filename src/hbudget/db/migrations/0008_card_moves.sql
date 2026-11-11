-- 0008: card purchases can be moved to another category by hand (D-083).
-- A category keeps the stores moved to it; a purchase moved on its own is marked so re-sorting leaves it alone.
-- Adds two columns with empty defaults; changes nothing else.

ALTER TABLE card_categories ADD COLUMN merchants_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE card_txns ADD COLUMN by_hand INTEGER NOT NULL DEFAULT 0;
