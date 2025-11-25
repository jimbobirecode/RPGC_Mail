#!/usr/bin/env python3
"""
Database Schema Checker for RPGC Mail Bot
==========================================

This script checks your current database schema and tells you:
1. What schema you currently have
2. What schema the code expects
3. What migrations (if any) you need to run
"""

import os
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.getenv("DATABASE_URL")

def check_schema():
    """Check current database schema"""
    if not DATABASE_URL:
        print("❌ ERROR: DATABASE_URL environment variable not set!")
        print("   Set it with: export DATABASE_URL='postgresql://user:pass@host:port/dbname'")
        return

    print("="*70)
    print("RPGC MAIL BOT - DATABASE SCHEMA CHECK")
    print("="*70)
    print()

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        # Check if tee_times table exists
        cursor.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_schema = 'public'
                AND table_name = 'tee_times'
            );
        """)
        table_exists = cursor.fetchone()['exists']

        if not table_exists:
            print("❌ TABLE 'tee_times' DOES NOT EXIST")
            print()
            print("You need to create the table. Run the migration script below.")
            cursor.close()
            conn.close()
            return

        print("✅ Table 'tee_times' exists")
        print()

        # Get column information
        cursor.execute("""
            SELECT
                column_name,
                data_type,
                is_nullable,
                column_default
            FROM information_schema.columns
            WHERE table_schema = 'public'
            AND table_name = 'tee_times'
            ORDER BY ordinal_position;
        """)

        columns = cursor.fetchall()

        print("📊 CURRENT SCHEMA:")
        print("-" * 70)
        print(f"{'Column Name':<20} {'Data Type':<20} {'Nullable':<10} {'Default':<15}")
        print("-" * 70)
        for col in columns:
            print(f"{col['column_name']:<20} {col['data_type']:<20} {col['is_nullable']:<10} {str(col['column_default'] or ''):<15}")
        print()

        # Check for required columns
        column_names = [col['column_name'] for col in columns]

        required_columns = {
            'id': 'Primary key',
            'club': 'Club identifier',
            'date': 'Specific date (not day_of_week!)',
            'time': 'Tee time (not tee_time!)',
            'max_players': 'Maximum players per slot',
            'available_slots': 'Current available slots',
            'is_available': 'Whether slot is bookable',
            'green_fee': 'Price per player',
        }

        old_columns = ['day_of_week', 'tee_time', 'period']

        print("✅ REQUIRED COLUMNS:")
        print("-" * 70)
        all_good = True
        for col, description in required_columns.items():
            if col in column_names:
                print(f"  ✅ {col:<20} - {description}")
            else:
                print(f"  ❌ {col:<20} - {description} (MISSING!)")
                all_good = False

        print()
        print("🚫 OLD COLUMNS (should NOT exist):")
        print("-" * 70)
        has_old_columns = False
        for col in old_columns:
            if col in column_names:
                print(f"  ⚠️  {col:<20} - This is from the OLD template-based schema!")
                has_old_columns = True
                all_good = False
            else:
                print(f"  ✅ {col:<20} - Correctly not present")

        print()
        print("="*70)

        if all_good:
            print("✅ SCHEMA IS CORRECT!")
            print("   Your database schema matches the code.")
            print("   No migration needed - you're good to go!")
        elif has_old_columns:
            print("⚠️  MIGRATION NEEDED: Template-based → Date-based")
            print("   You have OLD schema columns (day_of_week, tee_time, period)")
            print("   See migration instructions below.")
        else:
            print("⚠️  SCHEMA INCOMPLETE")
            print("   You're missing required columns.")
            print("   See migration instructions below.")

        print("="*70)
        print()

        # Check for sample data
        cursor.execute("SELECT COUNT(*) as count FROM tee_times;")
        row_count = cursor.fetchone()['count']

        print(f"📈 DATA: {row_count} rows in tee_times table")

        if row_count > 0:
            cursor.execute("SELECT * FROM tee_times LIMIT 3;")
            samples = cursor.fetchall()
            print()
            print("📋 SAMPLE DATA (first 3 rows):")
            print("-" * 70)
            for i, row in enumerate(samples, 1):
                print(f"Row {i}:")
                for key, value in row.items():
                    print(f"  {key}: {value}")
                print()

        cursor.close()
        conn.close()

    except Exception as e:
        print(f"❌ ERROR: {e}")
        print()
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    check_schema()
