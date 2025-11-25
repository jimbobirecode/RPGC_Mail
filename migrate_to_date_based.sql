-- ============================================================================
-- MIGRATION: Template-Based (day_of_week) → Date-Based Inventory
-- ============================================================================
--
-- This migration converts from:
--   OLD: Recurring weekly templates (MONDAY, TUESDAY, etc.)
--   NEW: Specific date-based inventory (2026-04-21, 2026-04-22, etc.)
--
-- IMPORTANT: This is a DESTRUCTIVE migration. Backup your data first!
--
-- Usage:
--   psql $DATABASE_URL -f migrate_to_date_based.sql
-- ============================================================================

BEGIN;

-- Step 1: Check if we need to migrate
DO $$
BEGIN
    IF EXISTS (
        SELECT FROM information_schema.columns
        WHERE table_name = 'tee_times'
        AND column_name = 'day_of_week'
    ) THEN
        RAISE NOTICE 'Old schema detected (day_of_week exists). Migration needed.';
    ELSE
        RAISE NOTICE 'New schema detected (day_of_week does not exist). Migration may not be needed.';
    END IF;
END $$;

-- Step 2: Backup old table
DROP TABLE IF EXISTS tee_times_backup;
CREATE TABLE tee_times_backup AS SELECT * FROM tee_times;
RAISE NOTICE 'Created backup: tee_times_backup';

-- Step 3: Drop old table
DROP TABLE IF EXISTS tee_times CASCADE;
RAISE NOTICE 'Dropped old tee_times table';

-- Step 4: Create new table with date-based schema
CREATE TABLE tee_times (
    id SERIAL PRIMARY KEY,
    club VARCHAR(100) NOT NULL,
    date DATE NOT NULL,                    -- Specific date (not day_of_week!)
    time VARCHAR(10) NOT NULL,             -- "10:00", "14:30", etc.
    max_players INTEGER DEFAULT 4,         -- Maximum capacity
    available_slots INTEGER DEFAULT 4,     -- Current available slots
    is_available BOOLEAN DEFAULT TRUE,     -- Whether slot is bookable
    green_fee DECIMAL(10, 2),             -- Price per player
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(club, date, time)
);

RAISE NOTICE 'Created new tee_times table with date-based schema';

-- Step 5: Create indexes
CREATE INDEX idx_tee_times_date ON tee_times(date);
CREATE INDEX idx_tee_times_club_date ON tee_times(club, date);
CREATE INDEX idx_tee_times_available ON tee_times(is_available, available_slots);

RAISE NOTICE 'Created indexes on tee_times';

-- Step 6: Optional - Generate sample inventory from template
-- UNCOMMENT THIS if you want to convert your old templates into actual dates
--
-- This will create tee times for the next 90 days based on your old templates
/*
INSERT INTO tee_times (club, date, time, max_players, available_slots, is_available, green_fee)
SELECT
    'royalportrush' as club,
    date_series.date,
    old.tee_time::TEXT as time,
    old.max_players,
    old.max_players as available_slots,  -- Start with full availability
    old.is_available,
    325.00 as green_fee  -- Default green fee
FROM tee_times_backup old
CROSS JOIN LATERAL (
    SELECT generate_series(
        CURRENT_DATE,
        CURRENT_DATE + INTERVAL '90 days',
        INTERVAL '1 day'
    )::DATE as date
) date_series
WHERE UPPER(TO_CHAR(date_series.date, 'Day')) = TRIM(old.day_of_week)
AND old.is_available = TRUE
ORDER BY date, time;

RAISE NOTICE 'Generated 90 days of tee times from old templates';
*/

COMMIT;

-- ============================================================================
-- POST-MIGRATION VERIFICATION
-- ============================================================================

-- Check new table
SELECT
    COUNT(*) as total_slots,
    COUNT(DISTINCT date) as unique_dates,
    MIN(date) as earliest_date,
    MAX(date) as latest_date,
    SUM(available_slots) as total_available_slots
FROM tee_times;

-- Show sample data
SELECT * FROM tee_times ORDER BY date, time LIMIT 10;
