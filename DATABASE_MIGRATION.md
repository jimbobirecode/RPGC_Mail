# Database Schema Migration Guide

## Quick Start: Check Your Database

**First, check what schema you currently have:**

```bash
# Set your database connection
export DATABASE_URL="postgresql://username:password@host:port/database"

# Run the diagnostic script
python3 check_db_schema.py
```

This will tell you:
- ✅ If your schema is correct (no changes needed)
- ⚠️ If you need to migrate (old template-based schema)
- ❌ If your database is missing required tables

---

## Understanding the Two Schemas

### OLD Schema (Template-Based - DEPRECATED ❌)

```sql
CREATE TABLE tee_times (
    id SERIAL PRIMARY KEY,
    day_of_week VARCHAR,      -- 'MONDAY', 'TUESDAY', etc.
    tee_time TIME,            -- Time field
    period VARCHAR,           -- 'AM' or 'PM'
    max_players INTEGER,
    is_available BOOLEAN
);
```

**How it worked:**
- Store recurring weekly templates ("Every Tuesday at 10:00")
- Query: `WHERE day_of_week = 'TUESDAY'`
- **Problem:** Doesn't track actual date-specific availability

### NEW Schema (Date-Based Inventory - CURRENT ✅)

```sql
CREATE TABLE tee_times (
    id SERIAL PRIMARY KEY,
    club VARCHAR(100) NOT NULL,
    date DATE NOT NULL,           -- Specific date: 2026-04-21
    time VARCHAR(10) NOT NULL,    -- "10:00", "14:30"
    max_players INTEGER DEFAULT 4,
    available_slots INTEGER DEFAULT 4,  -- Decrements on booking
    is_available BOOLEAN DEFAULT TRUE,
    green_fee DECIMAL(10,2),
    notes TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    UNIQUE(club, date, time)
);
```

**How it works:**
- Store specific date/time inventory ("April 21, 2026 at 10:00")
- Query: `WHERE date = '2026-04-21' AND available_slots > 0`
- **Benefit:** Tracks actual availability per date

---

## Do You Need to Migrate?

### Scenario 1: ✅ You Already Have the Correct Schema

**Check shows:**
```
✅ SCHEMA IS CORRECT!
   Your database schema matches the code.
   No migration needed - you're good to go!
```

**Action:** Nothing! You're done. The code I just pushed matches your database.

### Scenario 2: ⚠️ You Have the OLD Template-Based Schema

**Check shows:**
```
⚠️ MIGRATION NEEDED: Template-based → Date-based
   You have OLD schema columns (day_of_week, tee_time, period)
```

**Action:** Run the migration (see below)

### Scenario 3: ❌ Table Doesn't Exist

**Check shows:**
```
❌ TABLE 'tee_times' DOES NOT EXIST
```

**Action:** Create fresh database with new schema (see below)

---

## Migration Instructions

### Option A: You Have OLD Schema → Migrate

**⚠️ IMPORTANT: This will backup your old data, but test in non-production first!**

1. **Backup your database:**
   ```bash
   pg_dump $DATABASE_URL > backup_$(date +%Y%m%d).sql
   ```

2. **Review the migration script:**
   ```bash
   cat migrate_to_date_based.sql
   ```

3. **Run the migration:**
   ```bash
   psql $DATABASE_URL -f migrate_to_date_based.sql
   ```

4. **What it does:**
   - Creates `tee_times_backup` with your old data
   - Drops old `tee_times` table
   - Creates new `tee_times` with date-based schema
   - Optionally converts templates → actual dates (uncomment in script)

5. **Populate with actual dates:**

   If you want to convert your old templates into actual dates for the next 90 days, edit `migrate_to_date_based.sql` and **UNCOMMENT** this section:

   ```sql
   -- Line 54-75: Uncomment the INSERT statement
   INSERT INTO tee_times (club, date, time, ...)
   SELECT ...
   FROM tee_times_backup ...
   ```

   This will create actual tee times for the next 90 days based on your old weekly templates.

### Option B: Fresh Database Setup

**If you don't have any data or want to start fresh:**

1. **Create the tables:**
   ```bash
   python3 app.py  # This will run init_database() on startup
   ```

   Or manually:
   ```bash
   psql $DATABASE_URL
   ```
   ```sql
   -- Copy the CREATE TABLE statements from app.py init_database() function
   -- (lines 130-205 in app.py)
   ```

2. **Populate with sample data:**
   ```bash
   psql $DATABASE_URL -f populate_sample_data.sql
   ```

   This creates sample tee times for the next 30 days:
   - Mon/Tue/Thu/Fri: 8:00 AM - 5:00 PM (every 8 minutes)
   - Sat/Sun: 9:00 AM - 2:00 PM
   - Wed: No visitor bookings (per business rules)

---

## Post-Migration: Test Your Setup

### 1. Verify Schema
```bash
python3 check_db_schema.py
```

Should show: ✅ SCHEMA IS CORRECT!

### 2. Check Sample Data
```bash
psql $DATABASE_URL
```
```sql
-- See available times for tomorrow
SELECT date, time, available_slots, green_fee
FROM tee_times
WHERE club = 'royalportrush'
AND date = CURRENT_DATE + 1
AND is_available = TRUE
AND available_slots > 0
ORDER BY time
LIMIT 10;
```

### 3. Test the Email Bot

Send a test email to your inbound webhook:

```bash
curl -X POST http://your-server/webhook/inbound \
  -d "from=test@example.com" \
  -d "subject=Tee time inquiry" \
  -d "text=Hi, I need 4 players for tomorrow" \
  -d "headers=Message-ID: <test123@example.com>"
```

Check logs for:
```
🔎 QUERYING - tee_times for date = '2026-04-22'
📊 QUERY RESULT - Found 8 available tee time(s) for 2026-04-22
   • 10:04 - 4/4 slots available - £325.00 per player
```

---

## Troubleshooting

### Error: "column day_of_week does not exist"

**Problem:** You have the NEW code but OLD database schema

**Solution:** Run the migration (Option A above)

### Error: "relation tee_times does not exist"

**Problem:** Database table not created

**Solution:** Run fresh setup (Option B above)

### No availability showing up

**Problem:** No data in tee_times table

**Solution:**
```bash
psql $DATABASE_URL -f populate_sample_data.sql
```

### Bookings have NULL tee_time

**Problem:** Old bookings created before fix

**Solution:** The new code now ALWAYS populates tee_time. Old bookings can be updated:
```sql
UPDATE bookings
SET tee_time = '10:00'  -- Set a default
WHERE tee_time IS NULL
AND status = 'Inquiry';
```

---

## Summary

| Your Situation | Action Required |
|----------------|----------------|
| ✅ Schema already correct (has `date`, `time` columns) | **Nothing!** Just pull latest code |
| ⚠️ Old schema (has `day_of_week`, `tee_time` columns) | **Run migration** → `migrate_to_date_based.sql` |
| ❌ No tee_times table | **Fresh setup** → `populate_sample_data.sql` |

**Questions?** Check with:
```bash
python3 check_db_schema.py
```
