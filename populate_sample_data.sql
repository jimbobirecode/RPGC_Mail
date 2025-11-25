-- ============================================================================
-- POPULATE SAMPLE TEE TIMES DATA (Date-Based Inventory)
-- ============================================================================
--
-- This script creates sample tee times for Royal Portrush Golf Club
-- for the next 30 days.
--
-- Usage:
--   psql $DATABASE_URL -f populate_sample_data.sql
-- ============================================================================

BEGIN;

-- Clear existing data (optional - comment out if you want to keep existing data)
-- DELETE FROM tee_times WHERE club = 'royalportrush';

-- Generate tee times for next 30 days
-- Monday, Tuesday, Thursday, Friday: 8:00 AM - 5:00 PM (every 8 minutes)
-- Wednesday: No visitor bookings (per business rules)
-- Saturday/Sunday: Limited times (9:00 AM - 2:00 PM)

INSERT INTO tee_times (club, date, time, max_players, available_slots, is_available, green_fee)
SELECT
    'royalportrush' as club,
    date_series.date,
    to_char(time_series.time, 'HH24:MI') as time,
    4 as max_players,
    4 as available_slots,  -- Start with full availability
    TRUE as is_available,
    325.00 as green_fee
FROM (
    -- Generate dates for next 30 days
    SELECT generate_series(
        CURRENT_DATE,
        CURRENT_DATE + INTERVAL '30 days',
        INTERVAL '1 day'
    )::DATE as date
) date_series
CROSS JOIN LATERAL (
    -- Generate times based on day of week
    SELECT generate_series(
        CASE
            -- Monday, Tuesday, Thursday, Friday: 8:00 AM - 5:00 PM
            WHEN EXTRACT(DOW FROM date_series.date) IN (1, 2, 4, 5) THEN '08:00:00'::TIME
            -- Saturday, Sunday: 9:00 AM - 2:00 PM
            WHEN EXTRACT(DOW FROM date_series.date) IN (0, 6) THEN '09:00:00'::TIME
            -- Wednesday: Skip (no visitor bookings)
            ELSE NULL
        END,
        CASE
            WHEN EXTRACT(DOW FROM date_series.date) IN (1, 2, 4, 5) THEN '17:00:00'::TIME
            WHEN EXTRACT(DOW FROM date_series.date) IN (0, 6) THEN '14:00:00'::TIME
            ELSE NULL
        END,
        INTERVAL '8 minutes'  -- 8-minute intervals
    )::TIME as time
) time_series
WHERE time_series.time IS NOT NULL  -- Exclude Wednesdays
ON CONFLICT (club, date, time) DO NOTHING;  -- Skip if already exists

COMMIT;

-- ============================================================================
-- VERIFICATION
-- ============================================================================

-- Show summary
SELECT
    TO_CHAR(date, 'Day') as day_of_week,
    COUNT(*) as num_slots,
    SUM(available_slots) as total_capacity,
    MIN(time) as first_time,
    MAX(time) as last_time
FROM tee_times
WHERE club = 'royalportrush'
AND date >= CURRENT_DATE
AND date < CURRENT_DATE + INTERVAL '7 days'
GROUP BY TO_CHAR(date, 'Day'), EXTRACT(DOW FROM date)
ORDER BY EXTRACT(DOW FROM date);

-- Show sample times for tomorrow
SELECT date, time, available_slots, green_fee
FROM tee_times
WHERE club = 'royalportrush'
AND date = CURRENT_DATE + INTERVAL '1 day'
ORDER BY time
LIMIT 10;
