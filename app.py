#!/usr/bin/env python3
"""
Royal Portrush Golf Club - Email Bot with Database-Driven Availability
=======================================================================

CUSTOMER JOURNEY - THREE-STAGE FLOW
===================================

Stage 1: Inquiry
----------------
- Customer sends initial email asking about availability
- Status: 'Inquiry'
- System checks DATABASE for available tee times
- Email shows available times with "Book Now" buttons

Stage 2: Request
----------------
- Customer clicks "Book Now" button
- Status: 'Inquiry' → 'Requested'
- System sends acknowledgment email

Stage 3: Confirmation (Manual by Team)
---------------------------------------
- Booking team reviews and CONFIRMS the booking
- Status: 'Requested' → 'Confirmed'
- Team sends confirmation with payment details

AVAILABILITY MANAGEMENT
=======================
- Tee times are stored in the database
- Staff manage availability via dashboard
- Bot queries database for available slots
- No external API dependency
- No visitor bookings on Wednesdays or weekends
"""

from flask import Flask, request, jsonify
import logging
import json
import os
from datetime import datetime, timedelta
from dateutil import parser as date_parser
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Email, To, Content
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from psycopg2.pool import SimpleConnectionPool
from typing import List, Dict, Optional
import hashlib
import re
from urllib.parse import quote

app = Flask(__name__)

# --- CONFIG ---
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
FROM_EMAIL = os.getenv("FROM_EMAIL", "teetimes@royalportrushgolfclub.com")
FROM_NAME = os.getenv("FROM_NAME", "Royal Portrush Golf Club")
PER_PLAYER_FEE = float(os.getenv("PER_PLAYER_FEE", "325.00"))
CURRENCY_SYMBOL = "£"  # British Pounds

DATABASE_URL = os.getenv("DATABASE_URL")
DEFAULT_COURSE_ID = os.getenv("DEFAULT_COURSE_ID", "royalportrush")
TRACKING_EMAIL_PREFIX = os.getenv("TRACKING_EMAIL_PREFIX", "royalportrush")
CLUB_BOOKING_EMAIL = os.getenv("CLUB_BOOKING_EMAIL", "teetimes@royalportrushgolfclub.com")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

db_pool = None

# Royal Portrush Brand Colors
ROYAL_PORTRUSH_COLORS = {
    'navy_primary': '#081c3c',
    'burgundy': '#6c1535',
    'championship_gold': '#997424',
    'metallic_gold': '#ad7e2d',
    'charcoal': '#221f20',
    'off_white': '#fffefe',
    'white': '#ffffff',
    'light_grey': '#f8f9fa',
    'border_grey': '#e5e7eb',
    'text_dark': '#1f2937',
    'text_medium': '#4b5563',
    'text_light': '#6b7280',
    'success_green': '#3a5a40',
    'success_bg': '#f0f9f4',
    'info_bg': '#e8edf5',
    'warning_bg': '#fef9f0',
}


# ============================================================================
# DATABASE FUNCTIONS
# ============================================================================

def init_db_pool():
    """Initialize database connection pool"""
    global db_pool
    try:
        if not DATABASE_URL:
            logging.error("DATABASE_URL not set!")
            return False
        db_pool = SimpleConnectionPool(minconn=1, maxconn=10, dsn=DATABASE_URL)
        logging.info("Database connection pool created")
        return True
    except Exception as e:
        logging.error(f"Failed to create DB pool: {e}")
        return False


def get_db_connection():
    if db_pool:
        return db_pool.getconn()
    return None


def release_db_connection(conn):
    if db_pool and conn:
        db_pool.putconn(conn)


def generate_booking_id(guest_email: str, timestamp: str = None) -> str:
    if timestamp is None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_str = datetime.now().strftime("%Y%m%d")
    hash_input = f"{guest_email}{timestamp}".encode('utf-8')
    hash_digest = hashlib.md5(hash_input).hexdigest()[:8].upper()
    return f"RP-{date_str}-{hash_digest}"


def init_database():
    """Create all required tables"""
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return False
        cursor = conn.cursor()

        # Bookings table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id SERIAL PRIMARY KEY,
                booking_id VARCHAR(255) UNIQUE NOT NULL,
                message_id VARCHAR(500),
                confirmation_message_id VARCHAR(500),
                timestamp TIMESTAMP NOT NULL,
                guest_email VARCHAR(255) NOT NULL,
                guest_name VARCHAR(255),
                dates JSONB,
                date DATE,
                tee_time VARCHAR(10),
                players INTEGER NOT NULL,
                total DECIMAL(10, 2) NOT NULL,
                status VARCHAR(50) NOT NULL DEFAULT 'Inquiry',
                note TEXT,
                club VARCHAR(100),
                club_name VARCHAR(255),
                customer_confirmed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Tee times table - THIS IS THE KEY CHANGE
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tee_times (
                id SERIAL PRIMARY KEY,
                club VARCHAR(100) NOT NULL,
                date DATE NOT NULL,
                time VARCHAR(10) NOT NULL,
                max_players INTEGER DEFAULT 4,
                available_slots INTEGER DEFAULT 4,
                is_available BOOLEAN DEFAULT TRUE,
                green_fee DECIMAL(10, 2),
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(club, date, time)
            );
        """)

        # Blocked dates table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS blocked_dates (
                id SERIAL PRIMARY KEY,
                club VARCHAR(100) NOT NULL,
                date DATE NOT NULL,
                reason VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(club, date)
            );
        """)

        # Create indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bookings_email ON bookings(guest_email);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tee_times_date ON tee_times(date);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tee_times_club_date ON tee_times(club, date);")

        conn.commit()
        cursor.close()
        logging.info("Database schema ready")
        return True
    except Exception as e:
        logging.error(f"Database initialization error: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            release_db_connection(conn)


def find_grouped_tee_times(all_slots: List[Dict], players: int, max_gap_minutes: int = 20) -> List[Dict]:
    """
    Group consecutive tee times together for larger parties.

    Args:
        all_slots: List of individual tee time slots
        players: Total number of players needed
        max_gap_minutes: Maximum gap between consecutive times (default 20 minutes)

    Returns:
        List of grouped tee time options that can accommodate the full group
    """
    if players <= 4:
        return all_slots  # No grouping needed for 4 or fewer players

    logging.info(f"👥 GROUPING TEE TIMES - Need {players} players, searching for combinations")

    grouped_results = []

    # Group slots by date
    dates = sorted(list(set([slot['date'] for slot in all_slots])))

    for date in dates:
        date_slots = [s for s in all_slots if s['date'] == date]
        date_slots.sort(key=lambda x: x['time'])

        # Try to find combinations of consecutive times
        i = 0
        while i < len(date_slots):
            combination = []
            total_capacity = 0
            current_slot = date_slots[i]

            # Start building a combination
            combination.append(current_slot)
            total_capacity += current_slot['available_slots']

            # Look ahead for consecutive times
            j = i + 1
            while j < len(date_slots) and total_capacity < players:
                next_slot = date_slots[j]

                # Calculate time gap
                current_time = datetime.strptime(current_slot['time'], '%H:%M')
                next_time = datetime.strptime(next_slot['time'], '%H:%M')
                gap_minutes = (next_time - current_time).total_seconds() / 60

                if gap_minutes <= max_gap_minutes:
                    combination.append(next_slot)
                    total_capacity += next_slot['available_slots']
                    current_slot = next_slot
                    j += 1
                else:
                    break

            # If this combination can fit the group, add it
            if total_capacity >= players:
                times = [s['time'] for s in combination]
                num_groups = len(combination)
                # Use green_fee from first slot (or default if not available)
                green_fee = combination[0].get('green_fee', PER_PLAYER_FEE)

                logging.info(f"   ✓ Found combination: {' & '.join(times)} = {total_capacity} slots ({num_groups} groups)")

                grouped_results.append({
                    'date': date,
                    'time': times[0],  # Primary time
                    'grouped_times': times,  # All times in the group
                    'available_slots': total_capacity,
                    'green_fee': green_fee,
                    'num_groups': num_groups,
                    'is_grouped': True
                })

            i += 1

    logging.info(f"📊 GROUPING RESULT - Found {len(grouped_results)} grouped tee time option(s)")
    return grouped_results


def check_availability_db(dates: List[str], players: int, club: str = None) -> List[Dict]:
    """
    Check tee time availability from the database (date-based inventory)
    Returns list of available slots for specific dates
    For groups > 4, automatically finds grouped consecutive tee times
    """
    if club is None:
        club = DEFAULT_COURSE_ID

    logging.info(f"🔍 DATABASE QUERY - Club: {club}, Dates: {dates}, Players: {players}")

    conn = None
    all_slots = []

    try:
        conn = get_db_connection()
        if not conn:
            logging.error("❌ DATABASE - No connection available")
            return []

        cursor = conn.cursor(cursor_factory=RealDictCursor)

        for date_str in dates:
            # Parse and validate date
            try:
                date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                day_name = date_obj.strftime('%A')  # e.g., 'Monday'

                logging.info(f"📅 CHECKING - {date_str} ({day_name})")

            except ValueError:
                logging.warning(f"⚠️  INVALID DATE FORMAT - {date_str}")
                continue

            # Check if date is blocked
            cursor.execute("""
                SELECT reason FROM blocked_dates
                WHERE club = %s AND date = %s
            """, (club, date_str))

            blocked = cursor.fetchone()
            if blocked:
                logging.info(f"🚫 DATE BLOCKED - {date_str}: {blocked.get('reason', 'No reason given')}")
                continue

            # Check day of week restrictions
            if date_obj.weekday() == 2:  # Wednesday
                logging.info(f"🚫 DATE EXCLUDED - {date_str} ({day_name}): No visitor bookings on Wednesdays")
                continue

            # Query available tee times for specific date (date-based inventory)
            logging.info(f"🔎 QUERYING - tee_times for date = '{date_str}'")

            cursor.execute("""
                SELECT
                    id,
                    date,
                    tee_time,
                    max_players,
                    available_slots,
                    is_available,
                    green_fee,
                    notes
                FROM tee_times
                WHERE club = %s
                AND date = %s
                AND is_available = TRUE
                AND available_slots > 0
                ORDER BY tee_time ASC
            """, (club, date_str))

            date_results = cursor.fetchall()
            slots_found = len(date_results)

            logging.info(f"📊 QUERY RESULT - Found {slots_found} available tee time(s) for {date_str}")

            if slots_found > 0:
                for slot in date_results:
                    # Convert TIME type to string (HH:MM format)
                    tee_time = slot['tee_time']
                    if hasattr(tee_time, 'strftime'):
                        # It's a datetime.time object
                        time_str = tee_time.strftime('%H:%M')
                    else:
                        # It's already a string
                        time_str = str(tee_time)
                        if len(time_str) == 8:  # HH:MM:SS format
                            time_str = time_str[:5]  # Convert to HH:MM

                    # Use actual available_slots from inventory
                    available_slots = slot['available_slots']
                    green_fee = float(slot['green_fee']) if slot['green_fee'] else PER_PLAYER_FEE

                    logging.info(f"   • {time_str} - {available_slots}/{slot['max_players']} slots available - £{green_fee:.2f} per player")

                    all_slots.append({
                        'date': date_str,
                        'time': time_str,
                        'available_slots': available_slots,
                        'max_players': slot['max_players'],
                        'green_fee': green_fee,
                        'is_grouped': False
                    })

        cursor.close()

        # If group size > 4, find grouped tee times
        if players > 4:
            results = find_grouped_tee_times(all_slots, players, max_gap_minutes=20)
        else:
            # For 4 or fewer, just filter slots that can accommodate
            results = [s for s in all_slots if s['available_slots'] >= players]

            if results:
                logging.info(f"✅ AVAILABILITY - {len(results)} tee time(s) found for {players} player(s)")

        logging.info(f"📋 TOTAL RESULTS - {len(results)} available tee time(s) across all dates")
        return results

    except Exception as e:
        logging.error(f"❌ DATABASE ERROR - {e}")
        logging.exception("Full traceback:")
        return []
    finally:
        if conn:
            release_db_connection(conn)


def find_alternative_dates(requested_dates: List[str], players: int, club: str = None, days_range: int = 2) -> List[Dict]:
    """
    Find alternative dates within ±days_range when requested dates aren't available
    Returns list of available slots for nearby dates
    """
    if club is None:
        club = DEFAULT_COURSE_ID

    logging.info(f"🔄 SEARCHING ALTERNATIVES - ±{days_range} days from requested dates")

    alternative_dates = []

    for date_str in requested_dates:
        try:
            requested_date = datetime.strptime(date_str, '%Y-%m-%d')

            # Check 2 days before and 2 days after
            for offset in range(-days_range, days_range + 1):
                if offset == 0:  # Skip the originally requested date
                    continue

                alt_date = requested_date + timedelta(days=offset)
                alt_date_str = alt_date.strftime('%Y-%m-%d')

                # Don't suggest past dates
                if alt_date.date() < datetime.now().date():
                    continue

                if alt_date_str not in alternative_dates:
                    alternative_dates.append(alt_date_str)

        except ValueError:
            continue

    # Remove duplicates and sort
    alternative_dates = sorted(list(set(alternative_dates)))

    if alternative_dates:
        logging.info(f"📅 ALTERNATIVE DATES - Checking: {', '.join(alternative_dates)}")
        results = check_availability_db(alternative_dates, players, club)

        if results:
            logging.info(f"✅ ALTERNATIVES FOUND - {len(results)} tee time(s) on nearby dates")
        else:
            logging.info(f"❌ NO ALTERNATIVES - No availability found on nearby dates")

        return results

    return []


def save_booking_to_db(booking_data: dict):
    """Save booking to PostgreSQL"""
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            logging.error("💥 SAVE FAILED - No database connection")
            return False

        cursor = conn.cursor()

        if 'booking_id' not in booking_data or not booking_data['booking_id']:
            booking_id = generate_booking_id(booking_data['guest_email'], booking_data['timestamp'])
            booking_data['booking_id'] = booking_id
        else:
            booking_id = booking_data['booking_id']

        logging.info(f"💾 ATTEMPTING DB INSERT - Booking ID: {booking_id}")
        logging.info(f"   Table: bookings")
        logging.info(f"   Guest: {booking_data['guest_email']}")
        logging.info(f"   Date: {booking_data.get('date')}, Time: {booking_data.get('tee_time')}")
        logging.info(f"   Players: {booking_data['players']}, Status: {booking_data['status']}")

        cursor.execute("""
            INSERT INTO bookings (
                booking_id, message_id, timestamp, guest_email, dates, date, tee_time,
                players, total, status, note, club, club_name
            ) VALUES (
                %(booking_id)s, %(message_id)s, %(timestamp)s, %(guest_email)s, %(dates)s,
                %(date)s, %(tee_time)s, %(players)s, %(total)s, %(status)s, %(note)s,
                %(club)s, %(club_name)s
            )
            ON CONFLICT (booking_id) DO UPDATE SET
                status = EXCLUDED.status,
                note = EXCLUDED.note,
                updated_at = CURRENT_TIMESTAMP
        """, {
            'booking_id': booking_id,
            'message_id': booking_data.get('message_id'),
            'timestamp': booking_data['timestamp'],
            'guest_email': booking_data['guest_email'],
            'dates': Json(booking_data.get('dates', [])),
            'date': booking_data.get('date'),
            'tee_time': booking_data.get('tee_time'),
            'players': booking_data['players'],
            'total': booking_data['total'],
            'status': booking_data['status'],
            'note': booking_data.get('note'),
            'club': booking_data.get('club'),
            'club_name': booking_data.get('club_name')
        })

        conn.commit()
        cursor.close()

        logging.info(f"✅ DB INSERT SUCCESSFUL - Booking {booking_id} saved to bookings table")
        return booking_id

    except Exception as e:
        logging.error(f"💥 DB INSERT FAILED - {e}")
        logging.exception("Full traceback:")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            release_db_connection(conn)


def get_booking_by_id(booking_id: str):
    """Get a specific booking by booking_id"""
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return None

        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT booking_id as id, timestamp, guest_email, dates, date, tee_time,
                   players, total, status, note, club, club_name
            FROM bookings WHERE booking_id = %s
        """, (booking_id,))

        booking = cursor.fetchone()
        cursor.close()

        if booking:
            booking_dict = dict(booking)
            for field in ['timestamp']:
                if booking_dict.get(field) and hasattr(booking_dict[field], 'strftime'):
                    booking_dict[field] = booking_dict[field].strftime('%Y-%m-%d %H:%M:%S')
            if booking_dict.get('date') and hasattr(booking_dict['date'], 'strftime'):
                booking_dict['date'] = booking_dict['date'].strftime('%Y-%m-%d')
            return booking_dict
        return None

    except Exception as e:
        logging.error(f"Failed to fetch booking: {e}")
        return None
    finally:
        if conn:
            release_db_connection(conn)


def update_booking_in_db(booking_id: str, updates: dict):
    """Update booking in PostgreSQL"""
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            logging.error("💥 UPDATE FAILED - No database connection")
            return False

        cursor = conn.cursor()
        set_clauses = []
        params = {'booking_id': booking_id}

        for key, value in updates.items():
            if key in ['status', 'note', 'players', 'total', 'date', 'tee_time']:
                set_clauses.append(f"{key} = %({key})s")
                params[key] = value

        if not set_clauses:
            logging.warning(f"⚠️  UPDATE SKIPPED - No valid fields to update for {booking_id}")
            return False

        set_clauses.append("updated_at = CURRENT_TIMESTAMP")

        logging.info(f"🔄 ATTEMPTING DB UPDATE - Booking ID: {booking_id}")
        logging.info(f"   Table: bookings")
        logging.info(f"   Updates: {updates}")

        query = f"""
            UPDATE bookings SET {', '.join(set_clauses)}
            WHERE booking_id = %(booking_id)s
        """
        cursor.execute(query, params)

        rows_updated = cursor.rowcount
        conn.commit()
        cursor.close()

        if rows_updated > 0:
            logging.info(f"✅ DB UPDATE SUCCESSFUL - {rows_updated} row(s) updated in bookings table")
            return True
        else:
            logging.warning(f"⚠️  DB UPDATE - No rows matched booking_id {booking_id}")
            return False

    except Exception as e:
        logging.error(f"💥 DB UPDATE FAILED - {e}")
        logging.exception("Full traceback:")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            release_db_connection(conn)


def extract_booking_id(text: str) -> Optional[str]:
    pattern = r'RP-\d{8}-[A-F0-9]{8}'
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(0).upper() if match else None


def strip_html_tags(html_content: str) -> str:
    """Remove HTML tags and decode entities from HTML content"""
    if not html_content:
        return ""

    # Remove HTML tags
    text = re.sub(r'<[^>]+>', ' ', html_content)

    # Common HTML entities
    text = text.replace('&nbsp;', ' ')
    text = text.replace('&amp;', '&')
    text = text.replace('&lt;', '<')
    text = text.replace('&gt;', '>')
    text = text.replace('&quot;', '"')
    text = text.replace('&#39;', "'")
    text = text.replace('&apos;', "'")

    # Clean up excessive whitespace
    text = re.sub(r'\s+', ' ', text)
    text = text.strip()

    return text


def extract_message_id(headers: str) -> Optional[str]:
    if not headers:
        return None
    pattern = r'Message-I[Dd]:\s*<?([^>\s]+)>?'
    match = re.search(pattern, headers, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


# ============================================================================
# HTML EMAIL TEMPLATES - ROYAL PORTRUSH BRANDING
# ============================================================================

def get_email_header():
    """Royal Portrush Golf Club branded email header - Outlook compatible with gradients"""
    return f"""
    <!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
    <html xmlns="http://www.w3.org/1999/xhtml" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
    <head>
        <meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
        <meta name="x-apple-disable-message-reformatting" />
        <title>Royal Portrush Golf Club - Booking</title>
        <!--[if gte mso 9]>
        <xml>
            <o:OfficeDocumentSettings>
                <o:AllowPNG/>
                <o:PixelsPerInch>96</o:PixelsPerInch>
            </o:OfficeDocumentSettings>
        </xml>
        <![endif]-->
        <style type="text/css">
            body {{
                margin: 0; padding: 0; width: 100%;
                font-family: Georgia, 'Times New Roman', serif;
                -webkit-text-size-adjust: 100%;
                -ms-text-size-adjust: 100%;
            }}
        </style>
    </head>
    <body style="margin: 0; padding: 0; width: 100%; font-family: Georgia, 'Times New Roman', serif; background-color: {ROYAL_PORTRUSH_COLORS['light_grey']};">
        <!-- Outer table for background color -->
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin: 0; padding: 0; background-color: {ROYAL_PORTRUSH_COLORS['light_grey']};">
            <tr>
                <td align="center" style="padding: 20px 0;">
                    <!-- Main email container -->
                    <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="background-color: #ffffff; margin: 0 auto;">

                        <!-- Header with gradient background (VML for Outlook, CSS for others) -->
                        <tr>
                            <td align="center" style="padding: 0;">
                                <!--[if gte mso 9]>
                                <v:rect xmlns:v="urn:schemas-microsoft-com:vml" fill="true" stroke="false" style="width:600px;height:200px;">
                                <v:fill type="gradient" color="{ROYAL_PORTRUSH_COLORS['navy_primary']}" color2="{ROYAL_PORTRUSH_COLORS['burgundy']}" angle="135" />
                                <v:textbox inset="0,0,0,0">
                                <![endif]-->
                                <div style="background: linear-gradient(135deg, {ROYAL_PORTRUSH_COLORS['navy_primary']} 0%, {ROYAL_PORTRUSH_COLORS['burgundy']} 100%); padding: 40px 30px; text-align: center;">
                                    <img src="https://raw.githubusercontent.com/jimbobirecode/TeeMail-Assests/main/LOGO.png" alt="Royal Portrush Golf Club" width="120" style="display: block; margin: 0 auto 20px auto; max-width: 120px; height: auto;" />
                                    <h1 style="color: #ffffff; font-size: 28px; margin: 0 0 10px 0; font-family: Georgia, 'Times New Roman', serif; font-weight: normal;">Royal Portrush Golf Club</h1>
                                    <p style="color: {ROYAL_PORTRUSH_COLORS['off_white']}; font-size: 16px; margin: 0; font-family: Georgia, 'Times New Roman', serif;">Available Tee Times for Your Round</p>
                                </div>
                                <!--[if gte mso 9]>
                                </v:textbox>
                                </v:rect>
                                <![endif]-->
                            </td>
                        </tr>

                        <!-- Content area -->
                        <tr>
                            <td style="padding: 40px 30px;">
    """


def get_email_footer():
    """Royal Portrush Golf Club branded email footer - Outlook compatible with gradients"""
    return f"""
                            </td>
                        </tr>

                        <!-- Footer with gradient background (VML for Outlook, CSS for others) -->
                        <tr>
                            <td align="center" style="padding: 0;">
                                <!--[if gte mso 9]>
                                <v:rect xmlns:v="urn:schemas-microsoft-com:vml" fill="true" stroke="false" style="width:600px;height:150px;">
                                <v:fill type="gradient" color="{ROYAL_PORTRUSH_COLORS['navy_primary']}" color2="{ROYAL_PORTRUSH_COLORS['burgundy']}" angle="135" />
                                <v:textbox inset="0,0,0,0">
                                <![endif]-->
                                <div style="background: linear-gradient(135deg, {ROYAL_PORTRUSH_COLORS['navy_primary']} 0%, {ROYAL_PORTRUSH_COLORS['burgundy']} 100%); padding: 30px; text-align: center; color: #ffffff;">
                                    <p style="margin: 0 0 10px 0; font-family: Georgia, 'Times New Roman', serif; font-size: 14px; color: #ffffff;">We look forward to welcoming you to Royal Portrush Golf Club!</p>
                                    <p style="margin: 0 0 15px 0; font-family: Georgia, 'Times New Roman', serif; font-size: 14px;">
                                        <strong style="color: {ROYAL_PORTRUSH_COLORS['championship_gold']};">Royal Portrush Golf Club</strong>
                                    </p>
                                    <p style="margin: 0; font-size: 13px; font-family: Georgia, 'Times New Roman', serif; color: #ffffff;">
                                        Questions? Email us at
                                        <a href="mailto:{CLUB_BOOKING_EMAIL}" style="color: {ROYAL_PORTRUSH_COLORS['championship_gold']}; text-decoration: underline;">{CLUB_BOOKING_EMAIL}</a>
                                    </p>
                                    <p style="margin: 15px 0 0 0; color: {ROYAL_PORTRUSH_COLORS['off_white']}; font-size: 11px; font-family: Georgia, 'Times New Roman', serif;">
                                        Powered by TeeMail &middot; Automated Visitor Booking
                                    </p>
                                </div>
                                <!--[if gte mso 9]>
                                </v:textbox>
                                </v:rect>
                                <![endif]-->
                            </td>
                        </tr>

                    </table>
                    <!-- End main email container -->
                </td>
            </tr>
        </table>
        <!-- End outer table -->
    </body>
    </html>
    """


def build_booking_link(date: str, time: str, players: int, guest_email: str, booking_id: str = None, grouped_times: List[str] = None, num_groups: int = None) -> str:
    """Generate mailto link for Book Now button"""
    tracking_email = f"{TRACKING_EMAIL_PREFIX}@bookings.teemail.io"

    # Handle grouped times
    if grouped_times and len(grouped_times) > 1:
        time_display = " & ".join(grouped_times)
        subject = quote(f"GROUP BOOKING REQUEST - {date} at {time_display}")
        tee_times_text = f"I would like to book the following tee times as a group:"
        time_detail = f"- Tee Times: {time_display} ({num_groups} groups)"
    else:
        subject = quote(f"BOOKING REQUEST - {date} at {time}")
        tee_times_text = f"I would like to book the following tee time:"
        time_detail = f"- Time: {time}"

    body_lines = [
        tee_times_text,
        f"",
        f"Booking Details:",
        f"- Date: {date}",
        time_detail,
        f"- Players: {players}",
        f"- Green Fee: {CURRENCY_SYMBOL}{PER_PLAYER_FEE:.0f} per player",
        f"- Total: {CURRENCY_SYMBOL}{players * PER_PLAYER_FEE:.0f}",
        f"",
        f"Guest Email: {guest_email}",
    ]

    if booking_id:
        body_lines.insert(3, f"- Booking ID: {booking_id}")

    body = quote("\n".join(body_lines))
    return f"mailto:{tracking_email}?subject={subject}&body={body}"


def format_date_display(date_str: str) -> str:
    """Format date for display"""
    try:
        date_obj = datetime.strptime(date_str, '%Y-%m-%d')
        return date_obj.strftime('%A, %B %d, %Y')
    except ValueError:
        return date_str


def outlook_button(text: str, link: str, bg_color: str = None) -> str:
    """Generate Outlook-compatible button using tables"""
    if bg_color is None:
        bg_color = ROYAL_PORTRUSH_COLORS['burgundy']

    return f"""
    <!--[if mso]>
    <v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="{link}" style="height:40px;v-text-anchor:middle;width:150px;" arcsize="10%" stroke="f" fillcolor="{bg_color}">
    <w:anchorlock/>
    <center style="color:#ffffff;font-family:Arial,sans-serif;font-size:13px;font-weight:bold;">{text}</center>
    </v:roundrect>
    <![endif]-->
    <![if !mso]>
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="display:inline-block;">
        <tr>
            <td style="background-color: {bg_color}; padding: 12px 24px; text-align: center;">
                <a href="{link}" style="color: #ffffff; text-decoration: none; font-weight: 600; font-size: 13px; font-family: Arial, sans-serif; display: inline-block;">{text}</a>
            </td>
        </tr>
    </table>
    <![endif]>
    """


def outlook_info_box(content: str, border_color: str = None, bg_color: str = None) -> str:
    """Generate Outlook-compatible info box using tables"""
    if border_color is None:
        border_color = ROYAL_PORTRUSH_COLORS['navy_primary']
    if bg_color is None:
        bg_color = ROYAL_PORTRUSH_COLORS['info_bg']

    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin: 20px 0;">
        <tr>
            <td style="background-color: {bg_color}; border-left: 4px solid {border_color}; padding: 20px;">
                {content}
            </td>
        </tr>
    </table>
    """


def format_inquiry_email(results: list, player_count: int, guest_email: str, booking_id: str = None) -> str:
    """Generate inquiry email with available tee times"""
    html = get_email_header()
    
    # Get date range
    dates_list = sorted(list(set([r["date"] for r in results])))
    
    html += f"""
        <p style="color: {ROYAL_PORTRUSH_COLORS['text_dark']}; font-size: 16px; line-height: 1.8;">
            Thank you for your enquiry! We're delighted to share available tee times for your round at Royal Portrush.
        </p>
        
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin: 20px 0;"><tr><td style="background-color: #e8edf5; border-left: 4px solid #081c3c; padding: 20px;">
            <h3 style="color: {ROYAL_PORTRUSH_COLORS['navy_primary']}; margin: 0 0 15px 0;">ℹ️ Booking Information</h3>
            <p style="margin: 5px 0;"><strong>Party Size:</strong> {player_count} player(s)</p>
            <p style="margin: 5px 0;"><strong>Status:</strong> <span style="color: {ROYAL_PORTRUSH_COLORS['success_green']};">✓ Tee Times Available</span></p>
        </div>
    """
    
    for date in dates_list:
        date_results = [r for r in results if r["date"] == date]
        if not date_results:
            continue
            
        formatted_date = format_date_display(date)
        
        html += f"""
        <div style="margin: 30px 0;">
            <h2 style="color: {ROYAL_PORTRUSH_COLORS['burgundy']}; font-size: 18px; margin: 0 0 15px 0;">
                📅 {formatted_date}
            </h2>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="1" style="border-collapse: collapse; margin: 25px 0; border-color: #e5e7eb;">
                <thead>
                    <tr>
                        <th>Tee Time</th>
                        <th>Players</th>
                        <th>Green Fee</th>
                        <th style="text-align: center;">Action</th>
                    </tr>
                </thead>
                <tbody>
        """
        
        for result in date_results:
            time = result["time"]
            green_fee = result.get("green_fee", PER_PLAYER_FEE)

            # Handle grouped tee times for larger parties
            is_grouped = result.get("is_grouped", False)
            grouped_times = result.get("grouped_times", [time])
            num_groups = result.get("num_groups", 1)

            if is_grouped and len(grouped_times) > 1:
                # Display grouped times
                time_display = " & ".join(grouped_times)
                players_display = f"{player_count} players ({num_groups} groups)"
                total_fee = player_count * PER_PLAYER_FEE
                booking_link = build_booking_link(date, time, player_count, guest_email, booking_id,
                                                  grouped_times=grouped_times, num_groups=num_groups)
            else:
                # Display single time
                time_display = time
                players_display = f"{player_count} players"
                total_fee = player_count * PER_PLAYER_FEE
                booking_link = build_booking_link(date, time, player_count, guest_email, booking_id)

            html += f"""
                <tr>
                    <td><strong style="color: {ROYAL_PORTRUSH_COLORS['navy_primary']};">{time_display}</strong></td>
                    <td>{players_display}</td>
                    <td style="color: {ROYAL_PORTRUSH_COLORS['burgundy']}; font-weight: 700;">{CURRENCY_SYMBOL}{total_fee:.2f}</td>
                    <td style="text-align: center;">
                        <a href="{booking_link}" style="background: linear-gradient(135deg, {ROYAL_PORTRUSH_COLORS['burgundy']} 0%, {ROYAL_PORTRUSH_COLORS['navy_primary']} 100%); color: #ffffff; padding: 10px 20px; text-decoration: none; border-radius: 6px; font-weight: 600; font-size: 13px; display: inline-block;">Book Now</a>
                    </td>
                </tr>
            """
        
        html += "</tbody></table></div>"
    
    html += get_email_footer()
    return html


def format_acknowledgment_email(booking_data: Dict) -> str:
    """Generate acknowledgment email when customer clicks Book Now"""
    booking_id = booking_data.get('id') or booking_data.get('booking_id', 'N/A')
    date = booking_data.get('date', 'TBD')
    time = booking_data.get('tee_time', 'TBD')
    players = booking_data.get('players', 4)
    total_fee = players * PER_PLAYER_FEE
    
    formatted_date = format_date_display(date) if date and date != 'TBD' else date
    
    html = get_email_header()
    
    html += f"""
        <div style="background: {ROYAL_PORTRUSH_COLORS['info_bg']}; padding: 20px; border-radius: 8px; text-align: center; margin-bottom: 30px;">
            <h2 style="margin: 0; font-size: 24px; color: {ROYAL_PORTRUSH_COLORS['navy_primary']};">📬 Booking Request Received</h2>
        </div>
        
        <p style="color: {ROYAL_PORTRUSH_COLORS['text_dark']}; font-size: 16px; line-height: 1.8;">
            Thank you for your booking request at <strong>Royal Portrush Golf Club</strong>. We have received your request and will review it shortly.
        </p>
        
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin: 20px 0;"><tr><td style="background-color: #e8edf5; border-left: 4px solid #081c3c; padding: 20px;">
            <h3 style="color: {ROYAL_PORTRUSH_COLORS['navy_primary']}; margin: 0 0 20px 0;">📋 Your Booking Request</h3>
            <table width="100%" style="border-collapse: collapse;">
                <tr style="background: {ROYAL_PORTRUSH_COLORS['light_grey']};">
                    <td style="padding: 12px; border-bottom: 1px solid {ROYAL_PORTRUSH_COLORS['border_grey']};"><strong>Booking ID</strong></td>
                    <td style="padding: 12px; text-align: right; border-bottom: 1px solid {ROYAL_PORTRUSH_COLORS['border_grey']};">{booking_id}</td>
                </tr>
                <tr>
                    <td style="padding: 12px; border-bottom: 1px solid {ROYAL_PORTRUSH_COLORS['border_grey']};"><strong>📅 Date</strong></td>
                    <td style="padding: 12px; text-align: right; border-bottom: 1px solid {ROYAL_PORTRUSH_COLORS['border_grey']};">{formatted_date}</td>
                </tr>
                <tr style="background: {ROYAL_PORTRUSH_COLORS['light_grey']};">
                    <td style="padding: 12px; border-bottom: 1px solid {ROYAL_PORTRUSH_COLORS['border_grey']};"><strong>🕐 Time</strong></td>
                    <td style="padding: 12px; text-align: right; border-bottom: 1px solid {ROYAL_PORTRUSH_COLORS['border_grey']};">{time}</td>
                </tr>
                <tr>
                    <td style="padding: 12px; border-bottom: 1px solid {ROYAL_PORTRUSH_COLORS['border_grey']};"><strong>👥 Players</strong></td>
                    <td style="padding: 12px; text-align: right; border-bottom: 1px solid {ROYAL_PORTRUSH_COLORS['border_grey']};">{players}</td>
                </tr>
                <tr style="background: {ROYAL_PORTRUSH_COLORS['warning_bg']};">
                    <td style="padding: 15px;"><strong>💷 Total Fee</strong></td>
                    <td style="padding: 15px; text-align: right; font-size: 20px; font-weight: 700; color: {ROYAL_PORTRUSH_COLORS['success_green']};">{CURRENCY_SYMBOL}{total_fee:.2f}</td>
                </tr>
            </table>
        </div>
        
        <div style="background: {ROYAL_PORTRUSH_COLORS['success_bg']}; border-left: 4px solid {ROYAL_PORTRUSH_COLORS['success_green']}; padding: 20px; border-radius: 8px; margin: 30px 0;">
            <p style="margin: 0;"><strong style="color: {ROYAL_PORTRUSH_COLORS['success_green']};">✅ What Happens Next</strong></p>
            <p style="margin: 10px 0 0 0; font-size: 14px;">Our team will review your booking request and contact you within 24 hours to confirm your tee time and provide payment details.</p>
        </div>
    """
    
    html += get_email_footer()
    return html


def format_confirmation_email(booking_data: Dict) -> str:
    """Generate confirmation email when booking team confirms"""
    booking_id = booking_data.get('id') or booking_data.get('booking_id', 'N/A')
    date = booking_data.get('date', 'TBD')
    time = booking_data.get('tee_time', 'TBD')
    players = booking_data.get('players', 4)
    total_fee = players * PER_PLAYER_FEE
    
    formatted_date = format_date_display(date) if date and date != 'TBD' else date
    
    html = get_email_header()
    
    html += f"""
        <div style="background: linear-gradient(135deg, {ROYAL_PORTRUSH_COLORS['success_green']} 0%, #1f4d31 100%); color: #ffffff; padding: 25px; border-radius: 8px; text-align: center; margin-bottom: 30px;">
            <h2 style="margin: 0; font-size: 28px;">✅ Booking Confirmed</h2>
        </div>
        
        <p style="color: {ROYAL_PORTRUSH_COLORS['text_dark']}; font-size: 16px; line-height: 1.8;">
            Congratulations! Your booking at <strong>Royal Portrush Golf Club</strong> has been confirmed.
        </p>
        
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin: 20px 0;"><tr><td style="background-color: #e8edf5; border-left: 4px solid #081c3c; padding: 20px;">
            <h3 style="color: {ROYAL_PORTRUSH_COLORS['navy_primary']}; margin: 0 0 20px 0;">📋 Confirmed Booking Details</h3>
            <table width="100%" style="border-collapse: collapse;">
                <tr style="background: {ROYAL_PORTRUSH_COLORS['light_grey']};">
                    <td style="padding: 12px; border-bottom: 1px solid {ROYAL_PORTRUSH_COLORS['border_grey']};"><strong>Booking ID</strong></td>
                    <td style="padding: 12px; text-align: right; border-bottom: 1px solid {ROYAL_PORTRUSH_COLORS['border_grey']};">{booking_id}</td>
                </tr>
                <tr>
                    <td style="padding: 12px; border-bottom: 1px solid {ROYAL_PORTRUSH_COLORS['border_grey']};"><strong>📅 Date</strong></td>
                    <td style="padding: 12px; text-align: right; font-weight: 700; color: {ROYAL_PORTRUSH_COLORS['navy_primary']}; border-bottom: 1px solid {ROYAL_PORTRUSH_COLORS['border_grey']};">{formatted_date}</td>
                </tr>
                <tr style="background: {ROYAL_PORTRUSH_COLORS['light_grey']};">
                    <td style="padding: 12px; border-bottom: 1px solid {ROYAL_PORTRUSH_COLORS['border_grey']};"><strong>🕐 Tee Time</strong></td>
                    <td style="padding: 12px; text-align: right; font-weight: 700; color: {ROYAL_PORTRUSH_COLORS['navy_primary']}; border-bottom: 1px solid {ROYAL_PORTRUSH_COLORS['border_grey']};">{time}</td>
                </tr>
                <tr>
                    <td style="padding: 12px; border-bottom: 1px solid {ROYAL_PORTRUSH_COLORS['border_grey']};"><strong>👥 Players</strong></td>
                    <td style="padding: 12px; text-align: right; border-bottom: 1px solid {ROYAL_PORTRUSH_COLORS['border_grey']};">{players}</td>
                </tr>
                <tr style="background: {ROYAL_PORTRUSH_COLORS['warning_bg']}; border: 2px solid {ROYAL_PORTRUSH_COLORS['championship_gold']};">
                    <td style="padding: 15px;"><strong>💷 Total Amount Due</strong></td>
                    <td style="padding: 15px; text-align: right; font-size: 24px; font-weight: 700; color: {ROYAL_PORTRUSH_COLORS['success_green']};">{CURRENCY_SYMBOL}{total_fee:.2f}</td>
                </tr>
            </table>
        </div>
        
        <div style="background: {ROYAL_PORTRUSH_COLORS['warning_bg']}; border-left: 4px solid {ROYAL_PORTRUSH_COLORS['championship_gold']}; padding: 20px; border-radius: 8px; margin: 30px 0;">
            <h3 style="margin: 0 0 15px 0; color: {ROYAL_PORTRUSH_COLORS['navy_primary']};"><strong>💳 Payment Details</strong></h3>
            <p style="margin: 0 0 10px 0;"><strong>Payment Method:</strong> Bank Transfer or Card Payment</p>
            <p style="margin: 10px 0 0 0; font-size: 14px; font-style: italic;">💡 For payment details, please reply to this email or call us at <strong>+44 28 7082 2311</strong></p>
        </div>
        
        <div style="background: {ROYAL_PORTRUSH_COLORS['info_bg']}; border-left: 4px solid {ROYAL_PORTRUSH_COLORS['navy_primary']}; padding: 20px; border-radius: 8px; margin: 30px 0;">
            <h3 style="margin: 0 0 10px 0; color: {ROYAL_PORTRUSH_COLORS['navy_primary']};">📍 Important Information</h3>
            <ul style="margin: 10px 0; padding-left: 20px; font-size: 14px; line-height: 1.8;">
                <li>Please arrive <strong>30 minutes before</strong> your tee time</li>
                <li>Maximum handicap: 18 for men, 24 for ladies</li>
                <li>Cancellations must be made at least 48 hours in advance</li>
            </ul>
        </div>
    """
    
    html += get_email_footer()
    return html


def format_no_availability_email(player_count: int, dates: list = None, alternative_results: list = None, guest_email: str = None, booking_id: str = None) -> str:
    """Generate email when no availability found, with alternative dates if available"""
    html = get_email_header()

    html += f"""
        <p style="color: {ROYAL_PORTRUSH_COLORS['text_dark']}; font-size: 16px; line-height: 1.8;">
            Thank you for your enquiry at <strong>Royal Portrush Golf Club</strong>.
        </p>

        <div style="background: #fef2f2; border-left: 4px solid #dc2626; border-radius: 8px; padding: 20px; margin: 25px 0;">
            <h3 style="color: #dc2626; margin: 0 0 12px 0;">⚠️ No Availability Found</h3>
            <p style="margin: 0;">Unfortunately, we do not have availability for <strong>{player_count} player(s)</strong> on your requested dates.</p>
        </div>
    """

    # If alternative dates found, show them
    if alternative_results and len(alternative_results) > 0:
        html += f"""
        <div style="background: {ROYAL_PORTRUSH_COLORS['success_bg']}; border-left: 4px solid {ROYAL_PORTRUSH_COLORS['success_green']}; border-radius: 8px; padding: 20px; margin: 25px 0;">
            <h3 style="color: {ROYAL_PORTRUSH_COLORS['success_green']}; margin: 0 0 15px 0;">✨ Alternative Dates Available</h3>
            <p style="margin: 0 0 15px 0;">We found availability on nearby dates (within 2 days):</p>
        </div>
        """

        # Group alternatives by date
        alt_dates_list = sorted(list(set([r["date"] for r in alternative_results])))

        for date in alt_dates_list:
            date_results = [r for r in alternative_results if r["date"] == date]
            if not date_results:
                continue

            formatted_date = format_date_display(date)

            html += f"""
            <div style="margin: 30px 0;">
                <h2 style="color: {ROYAL_PORTRUSH_COLORS['burgundy']}; font-size: 18px; margin: 0 0 15px 0;">
                    📅 {formatted_date}
                </h2>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="1" style="border-collapse: collapse; margin: 25px 0; border-color: #e5e7eb;">
                    <thead>
                        <tr>
                            <th>Tee Time</th>
                            <th>Players</th>
                            <th>Green Fee</th>
                            <th style="text-align: center;">Action</th>
                        </tr>
                    </thead>
                    <tbody>
            """

            for result in date_results[:8]:  # Limit to 8 times per date
                time = result["time"]
                green_fee = result.get("green_fee", PER_PLAYER_FEE)
                booking_link = build_booking_link(date, time, player_count, guest_email, booking_id) if guest_email else "#"

                html += f"""
                    <tr>
                        <td><strong style="color: {ROYAL_PORTRUSH_COLORS['navy_primary']};">{time}</strong></td>
                        <td>{player_count} players</td>
                        <td style="color: {ROYAL_PORTRUSH_COLORS['burgundy']}; font-weight: 700;">{CURRENCY_SYMBOL}{green_fee:.2f}</td>
                        <td style="text-align: center;">
                            <a href="{booking_link}" style="background: linear-gradient(135deg, {ROYAL_PORTRUSH_COLORS['burgundy']} 0%, {ROYAL_PORTRUSH_COLORS['navy_primary']} 100%); color: #ffffff; padding: 10px 20px; text-decoration: none; border-radius: 6px; font-weight: 600; font-size: 13px; display: inline-block;">Book Now</a>
                        </td>
                    </tr>
                """

            html += "</tbody></table></div>"

    # Always show suggestions and contact info
    html += f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin: 20px 0;"><tr><td style="background-color: #e8edf5; border-left: 4px solid #081c3c; padding: 20px;">
            <h3 style="color: {ROYAL_PORTRUSH_COLORS['navy_primary']}; margin: 0 0 12px 0;">💡 Suggestions</h3>
            <ul style="margin: 10px 0; padding-left: 20px; font-size: 14px; line-height: 1.8;">
                <li>Try different dates (note: no visitor bookings on Wednesdays)</li>
                <li>Consider a smaller group size</li>
                <li>Contact us directly for more options</li>
            </ul>
        </div>

        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin: 20px 0;"><tr><td style="background-color: #e8edf5; border-left: 4px solid #081c3c; padding: 20px;">
            <h3 style="color: {ROYAL_PORTRUSH_COLORS['navy_primary']}; margin: 0 0 12px 0;">📞 Contact Us</h3>
            <p style="margin: 5px 0;"><strong>Email:</strong> <a href="mailto:{CLUB_BOOKING_EMAIL}" style="color: {ROYAL_PORTRUSH_COLORS['burgundy']};">{CLUB_BOOKING_EMAIL}</a></p>
            <p style="margin: 5px 0;"><strong>Phone:</strong> +44 28 7082 2311</p>
        </div>
    """

    html += get_email_footer()
    return html


# ============================================================================
# EMAIL SENDING
# ============================================================================

def send_email_sendgrid(to_email: str, subject: str, html_body: str) -> bool:
    """Send email via SendGrid"""
    try:
        logging.info(f"Sending email to: {to_email}")
        message = Mail(
            from_email=Email(FROM_EMAIL, FROM_NAME),
            to_emails=To(to_email),
            subject=subject,
            html_content=Content("text/html", html_body)
        )
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)
        logging.info(f"Email sent - Status: {response.status_code}")
        return True
    except Exception as e:
        logging.error(f"Failed to send email: {e}")
        return False


# ============================================================================
# EMAIL DETECTION
# ============================================================================

def is_booking_request(subject: str, body: str) -> bool:
    subject_lower = subject.lower() if subject else ""
    body_lower = body.lower() if body else ""
    
    if "booking request" in subject_lower:
        return True
    
    has_booking_ref = extract_booking_id(body) or extract_booking_id(subject)
    booking_keywords = ['booking request', 'book now', 'reserve']
    has_keyword = any(k in body_lower or k in subject_lower for k in booking_keywords)
    
    return has_booking_ref and has_keyword


def is_staff_confirmation(subject: str, body: str, from_email: str) -> bool:
    subject_lower = subject.lower() if subject else ""
    body_lower = body.lower() if body else ""
    
    confirm_keywords = ['confirm booking', 'confirmed', 'approve booking']
    has_confirm = any(k in subject_lower or k in body_lower for k in confirm_keywords)
    has_booking_ref = extract_booking_id(body) or extract_booking_id(subject)
    
    return has_confirm and has_booking_ref


def parse_email_simple(subject: str, body: str) -> Dict:
    """Parse email to extract dates and player count - Enhanced version"""
    full_text = f"{subject}\n{body}"
    full_text_lower = full_text.lower()
    result = {'players': 4, 'dates': []}

    logging.info(f"🔍 PARSING EMAIL - Length: {len(full_text)} chars")
    logging.info(f"   Subject: {subject[:100]}")
    logging.info(f"   Body preview: {body[:200]}")

    # ========================================================================
    # EXTRACT PLAYER COUNT - Multiple patterns
    # ========================================================================
    player_patterns = [
        r'(\d+)\s*(?:players?|people|persons?|golfers?|guests?)',  # "4 players", "2 people"
        r'(?:party|group)\s+of\s+(\d+)',                            # "party of 4", "group of 6"
        r'(\d+)[-\s]ball',                                          # "4-ball", "2 ball"
        r'(?:foursome|four\s*ball)',                                # "foursome" = 4
        r'(?:twosome|two\s*ball)',                                  # "twosome" = 2
        r'for\s+(\d+)',                                             # "booking for 4"
        r'we\s+(?:are|have)\s+(\d+)',                               # "we are 6", "we have 4"
    ]

    player_found = False
    for pattern in player_patterns:
        match = re.search(pattern, full_text_lower)
        if match:
            if pattern in [r'(?:foursome|four\s*ball)', r'(?:twosome|two\s*ball)']:
                # Fixed patterns without capture groups
                num = 4 if 'four' in pattern else 2
            else:
                num = int(match.group(1))

            if 1 <= num <= 20:
                result['players'] = num
                player_found = True
                logging.info(f"📊 PARSED - Players: {num} (pattern: {pattern[:30]}...)")
                break
            else:
                logging.warning(f"⚠️  PARSED - Players: {num} (out of range 1-20, using default 4)")

    if not player_found:
        logging.info(f"📊 PARSED - Players: 4 (default, no match found)")

    # ========================================================================
    # EXTRACT DATES - Multiple formats
    # ========================================================================
    date_patterns = [
        # ISO format: 2025-12-25
        (r'(\d{4}-\d{2}-\d{2})', 'iso'),

        # DD/MM/YYYY variants: 25/12/2025, 25-12-2025, 25.12.2025
        (r'(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4})', 'dmy_full'),

        # DD/MM/YY variants: 25/12/25
        (r'(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2})(?!\d)', 'dmy_short'),

        # Month name formats: December 25 2025, Dec 25, 25 December 2025, 25th Dec 2025
        (r'(\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{4})', 'dmy_named_year'),
        (r'((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?\s*,?\s+\d{4})', 'mdy_named_year'),
        (r'(\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*)', 'dmy_named'),
        (r'((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?)', 'mdy_named'),
    ]

    dates_found = []
    for pattern, pattern_name in date_patterns:
        for match in re.finditer(pattern, full_text_lower, re.IGNORECASE):
            date_str = match.group(1).strip()

            try:
                # Parse based on pattern type
                if pattern_name == 'iso':
                    parsed_date = datetime.strptime(date_str, '%Y-%m-%d')
                    logging.debug(f"   Parsed ISO date: {date_str} -> {parsed_date.date()}")

                elif pattern_name.startswith('dmy'):
                    # UK format - day first
                    parsed_date = date_parser.parse(date_str, fuzzy=True, dayfirst=True, default=datetime.now().replace(day=1))
                    logging.debug(f"   Parsed DMY date: {date_str} -> {parsed_date.date()}")

                elif pattern_name.startswith('mdy'):
                    # US format - month first
                    parsed_date = date_parser.parse(date_str, fuzzy=True, dayfirst=False, default=datetime.now().replace(day=1))
                    logging.debug(f"   Parsed MDY date: {date_str} -> {parsed_date.date()}")

                else:
                    # Generic parsing
                    parsed_date = date_parser.parse(date_str, fuzzy=True, dayfirst=True, default=datetime.now().replace(day=1))
                    logging.debug(f"   Parsed generic date: {date_str} -> {parsed_date.date()}")

                # Validation: Only future dates within next 2 years
                today = datetime.now().date()
                two_years_ahead = today.replace(year=today.year + 2)

                if parsed_date.date() >= today and parsed_date.date() <= two_years_ahead:
                    formatted = parsed_date.strftime('%Y-%m-%d')
                    if formatted not in dates_found:
                        dates_found.append(formatted)
                        logging.debug(f"   ✓ Valid date: {formatted}")
                    else:
                        logging.debug(f"   - Duplicate date: {formatted}")
                else:
                    logging.debug(f"   ✗ Date out of range: {parsed_date.date()} (must be {today} to {two_years_ahead})")

            except Exception as e:
                logging.debug(f"   ✗ Failed to parse '{date_str}' with pattern {pattern_name}: {e}")
                continue

    result['dates'] = sorted(dates_found)

    if dates_found:
        logging.info(f"📅 PARSED - Dates found: {', '.join(dates_found)}")
    else:
        logging.info(f"📅 PARSED - No valid dates found in email")

    logging.info(f"✅ PARSE COMPLETE - Players: {result['players']}, Dates: {len(result['dates'])}")
    return result


# ============================================================================
# WEBHOOK ENDPOINTS
# ============================================================================

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'healthy',
        'service': 'Royal Portrush Email Bot - Database-Driven Availability',
        'database': 'connected' if db_pool else 'disconnected'
    })


@app.route('/webhook/inbound', methods=['POST'])
def handle_inbound_email():
    """Handle incoming emails"""
    try:
        from_email = request.form.get('from', '')
        subject = request.form.get('subject', '')
        text_body = request.form.get('text', '')
        html_body = request.form.get('html', '')
        headers = request.form.get('headers', '')

        # Properly handle text vs HTML body - prefer text, strip HTML if using HTML
        if text_body and text_body.strip():
            body = text_body.strip()
            body_type = "text"
        elif html_body and html_body.strip():
            body = strip_html_tags(html_body)
            body_type = "html (stripped)"
        else:
            body = ""
            body_type = "empty"

        message_id = extract_message_id(headers)

        logging.info("="*60)
        logging.info(f"INBOUND EMAIL - From: {from_email}")
        logging.info(f"Subject: {subject}")
        logging.info(f"Body type: {body_type}")
        logging.info(f"Body preview: {body[:200]}..." if len(body) > 200 else f"Body: {body}")
        logging.info("="*60)
        
        # Extract sender email
        if '<' in from_email:
            sender_email = from_email.split('<')[1].strip('>')
        else:
            sender_email = from_email
        
        if not sender_email or '@' not in sender_email:
            return jsonify({'status': 'invalid_email'}), 400
        
        parsed = parse_email_simple(subject, body)
        
        # CASE 1: Staff Confirmation
        if is_staff_confirmation(subject, body, sender_email):
            logging.info("DETECTED: Staff Confirmation")
            booking_id = extract_booking_id(subject) or extract_booking_id(body)

            if booking_id:
                booking = get_booking_by_id(booking_id)
                if booking and booking.get('status') == 'Requested':
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    logging.info(f"✅ CONFIRMING BOOKING - {booking_id}")
                    logging.info(f"   Customer: {booking.get('guest_email')}")
                    logging.info(f"   Date: {booking.get('date', 'TBD')}")
                    logging.info(f"   Time: {booking.get('tee_time', 'TBD')}")
                    logging.info(f"   Players: {booking.get('players', 'N/A')}")

                    update_booking_in_db(booking_id, {
                        'status': 'Confirmed',
                        'note': f"Booking confirmed by team on {timestamp}"
                    })

                    customer_email = booking.get('guest_email')
                    if customer_email:
                        logging.info(f"📧 SENDING CONFIRMATION EMAIL - to {customer_email}")
                        html_email = format_confirmation_email(booking)
                        send_email_sendgrid(customer_email, "Booking Confirmed - Royal Portrush Golf Club", html_email)

                    return jsonify({'status': 'confirmed', 'booking_id': booking_id}), 200

            return jsonify({'status': 'no_booking_id'}), 200
        
        # CASE 2: Booking Request
        elif is_booking_request(subject, body):
            logging.info("DETECTED: Booking Request")
            booking_id = extract_booking_id(subject) or extract_booking_id(body)

            if booking_id:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                updates = {
                    'status': 'Requested',
                    'note': f"Customer sent booking request on {timestamp}"
                }

                # Extract date and time
                date_match = re.search(r'(\d{4}-\d{2}-\d{2})', subject + body)
                time_match = re.search(r'(\d{1,2}:\d{2})', subject + body)

                if date_match:
                    updates['date'] = date_match.group(1)
                    logging.info(f"📅 EXTRACTED DATE - {date_match.group(1)}")
                if time_match:
                    updates['tee_time'] = time_match.group(1)
                    logging.info(f"🕐 EXTRACTED TIME - {time_match.group(1)}")

                logging.info(f"🔄 UPDATING BOOKING - {booking_id} to 'Requested' status")
                update_booking_in_db(booking_id, updates)

                booking_data = get_booking_by_id(booking_id)
                if booking_data:
                    logging.info(f"📧 SENDING ACKNOWLEDGMENT - Booking {booking_id} to {sender_email}")
                    logging.info(f"   Date: {booking_data.get('date', 'TBD')}")
                    logging.info(f"   Time: {booking_data.get('tee_time', 'TBD')}")
                    logging.info(f"   Players: {booking_data.get('players', 'N/A')}")
                    logging.info(f"   Total: £{booking_data.get('total', 0):.2f}")

                    html_email = format_acknowledgment_email(booking_data)
                    send_email_sendgrid(sender_email, "Your Booking Request - Royal Portrush Golf Club", html_email)

                return jsonify({'status': 'requested', 'booking_id': booking_id}), 200

            return jsonify({'status': 'no_booking_id'}), 200
        
        # CASE 3: New Inquiry
        else:
            logging.info("DETECTED: New Inquiry")
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            booking_id = generate_booking_id(sender_email, timestamp)

            new_entry = {
                "booking_id": booking_id,
                "timestamp": timestamp,
                "guest_email": sender_email,
                "message_id": message_id,
                "dates": parsed['dates'],
                "date": parsed['dates'][0] if parsed['dates'] else None,
                "tee_time": None,
                "players": parsed['players'],
                "total": PER_PLAYER_FEE * parsed['players'],
                "status": "Inquiry",
                "note": "Initial inquiry received",
                "club": DEFAULT_COURSE_ID,
                "club_name": FROM_NAME
            }

            logging.info(f"💾 SAVING BOOKING - ID: {booking_id}, Guest: {sender_email}, Players: {parsed['players']}, Total: £{new_entry['total']:.2f}")

            save_booking_to_db(new_entry)

            # Check database for availability
            if parsed['dates']:
                results = check_availability_db(parsed['dates'], parsed['players'], DEFAULT_COURSE_ID)

                if results:
                    # Update booking with first available time
                    first_result = results[0]
                    update_booking_in_db(booking_id, {
                        'date': first_result['date'],
                        'tee_time': first_result['time'],
                        'note': f"Initial inquiry - {len(results)} tee time(s) available"
                    })

                    logging.info(f"📧 SENDING EMAIL - Available Tee Times ({len(results)} options) to {sender_email}")
                    logging.info(f"   Booking ID: {booking_id}")
                    logging.info(f"   Players: {parsed['players']}")
                    logging.info(f"   Suggested Time: {first_result['date']} at {first_result['time']}")
                    logging.info(f"   Green Fee: £{first_result.get('green_fee', PER_PLAYER_FEE):.2f} per player")
                    logging.info(f"   Total: £{parsed['players'] * first_result.get('green_fee', PER_PLAYER_FEE):.2f}")

                    html_email = format_inquiry_email(results, parsed['players'], sender_email, booking_id)
                    subject_line = "Available Tee Times at Royal Portrush Golf Club"
                else:
                    # No availability on requested dates - search for alternatives
                    alternative_results = find_alternative_dates(parsed['dates'], parsed['players'], DEFAULT_COURSE_ID, days_range=2)

                    if alternative_results:
                        # Update booking with first alternative time
                        first_alt = alternative_results[0]
                        update_booking_in_db(booking_id, {
                            'date': first_alt['date'],
                            'tee_time': first_alt['time'],
                            'note': f"Initial inquiry - no availability on requested dates, {len(alternative_results)} alternative(s) suggested"
                        })

                        logging.info(f"📧 SENDING EMAIL - No Availability on Requested Dates, but {len(alternative_results)} Alternative(s) Found")
                        logging.info(f"   Requested: {', '.join(parsed['dates'])}")
                        logging.info(f"   Players: {parsed['players']}")
                        logging.info(f"   Alternatives: {len(set([r['date'] for r in alternative_results]))} nearby date(s)")
                        logging.info(f"   Suggested Time: {first_alt['date']} at {first_alt['time']}")

                        html_email = format_no_availability_email(parsed['players'], parsed['dates'], alternative_results, sender_email, booking_id)
                        subject_line = "Alternative Tee Times Available - Royal Portrush Golf Club"
                    else:
                        logging.info(f"📧 SENDING EMAIL - No Availability (including alternatives) to {sender_email}")
                        logging.info(f"   Requested: {', '.join(parsed['dates'])}")
                        logging.info(f"   Players: {parsed['players']}")

                        html_email = format_no_availability_email(parsed['players'], parsed['dates'])
                        subject_line = "Tee Time Availability - Royal Portrush Golf Club"
            else:
                logging.info(f"📧 SENDING EMAIL - No Dates Provided to {sender_email}")
                html_email = format_no_availability_email(parsed['players'])
                subject_line = "Tee Time Inquiry - Royal Portrush Golf Club"

            send_email_sendgrid(sender_email, subject_line, html_email)

            return jsonify({'status': 'inquiry_created', 'booking_id': booking_id}), 200
    
    except Exception as e:
        logging.exception(f"Error processing email: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ============================================================================
# API ENDPOINTS FOR DASHBOARD
# ============================================================================

@app.route('/api/bookings', methods=['GET'])
def api_get_bookings():
    """Get all bookings"""
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'No database connection'}), 500
        
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM bookings WHERE club = %s ORDER BY timestamp DESC", (DEFAULT_COURSE_ID,))
        bookings = cursor.fetchall()
        cursor.close()
        release_db_connection(conn)
        
        # Convert to serializable format
        booking_list = []
        for b in bookings:
            bd = dict(b)
            for f in ['timestamp', 'created_at', 'updated_at']:
                if bd.get(f) and hasattr(bd[f], 'strftime'):
                    bd[f] = bd[f].strftime('%Y-%m-%d %H:%M:%S')
            if bd.get('date') and hasattr(bd['date'], 'strftime'):
                bd['date'] = bd['date'].strftime('%Y-%m-%d')
            if bd.get('total'):
                bd['total'] = float(bd['total'])
            booking_list.append(bd)
        
        return jsonify({'success': True, 'bookings': booking_list, 'count': len(booking_list)})
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bookings/<booking_id>', methods=['PUT'])
def api_update_booking(booking_id):
    """Update a booking"""
    try:
        data = request.json
        if update_booking_in_db(booking_id, data):
            return jsonify({'success': True})
        return jsonify({'success': False}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/tee-times', methods=['GET'])
def api_get_tee_times():
    """Get all tee times"""
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'No database connection'}), 500
        
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        date_from = request.args.get('from')
        date_to = request.args.get('to')
        
        query = "SELECT * FROM tee_times WHERE club = %s"
        params = [DEFAULT_COURSE_ID]
        
        if date_from:
            query += " AND date >= %s"
            params.append(date_from)
        if date_to:
            query += " AND date <= %s"
            params.append(date_to)
        
        query += " ORDER BY date ASC, time ASC"
        
        cursor.execute(query, params)
        tee_times = cursor.fetchall()
        cursor.close()
        release_db_connection(conn)
        
        tt_list = []
        for t in tee_times:
            td = dict(t)
            if td.get('date') and hasattr(td['date'], 'strftime'):
                td['date'] = td['date'].strftime('%Y-%m-%d')
            if td.get('green_fee'):
                td['green_fee'] = float(td['green_fee'])
            tt_list.append(td)
        
        return jsonify({'success': True, 'tee_times': tt_list, 'count': len(tt_list)})
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/tee-times', methods=['POST'])
def api_add_tee_time():
    """Add a new tee time slot"""
    try:
        data = request.json
        date = data.get('date')
        time = data.get('time')
        max_players = data.get('max_players', 4)
        green_fee = data.get('green_fee', PER_PLAYER_FEE)
        
        if not date or not time:
            return jsonify({'success': False, 'error': 'Date and time required'}), 400
        
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'No database connection'}), 500
        
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO tee_times (club, date, time, max_players, available_slots, green_fee, is_available)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (club, date, time) DO UPDATE SET
                max_players = EXCLUDED.max_players,
                available_slots = EXCLUDED.available_slots,
                green_fee = EXCLUDED.green_fee,
                is_available = TRUE,
                updated_at = CURRENT_TIMESTAMP
        """, (DEFAULT_COURSE_ID, date, time, max_players, max_players, green_fee))
        
        conn.commit()
        cursor.close()
        release_db_connection(conn)
        
        return jsonify({'success': True, 'message': f'Tee time added: {date} at {time}'})
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/tee-times/<int:tee_time_id>', methods=['DELETE'])
def api_delete_tee_time(tee_time_id):
    """Delete a tee time slot"""
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'No database connection'}), 500
        
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tee_times WHERE id = %s", (tee_time_id,))
        conn.commit()
        cursor.close()
        release_db_connection(conn)
        
        return jsonify({'success': True})
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/tee-times/bulk', methods=['POST'])
def api_bulk_add_tee_times():
    """Bulk add tee times for a date range"""
    try:
        data = request.json
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        times = data.get('times', [])
        max_players = data.get('max_players', 4)
        green_fee = data.get('green_fee', PER_PLAYER_FEE)
        
        if not start_date or not end_date or not times:
            return jsonify({'success': False, 'error': 'start_date, end_date, and times required'}), 400
        
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'No database connection'}), 500
        
        cursor = conn.cursor()
        
        start = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')
        
        added_count = 0
        current = start
        
        while current <= end:
            # Skip Wed, Sat, Sun
            if current.weekday() not in [2, 5, 6]:
                date_str = current.strftime('%Y-%m-%d')
                for time in times:
                    cursor.execute("""
                        INSERT INTO tee_times (club, date, time, max_players, available_slots, green_fee, is_available)
                        VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                        ON CONFLICT (club, date, time) DO NOTHING
                    """, (DEFAULT_COURSE_ID, date_str, time, max_players, max_players, green_fee))
                    if cursor.rowcount > 0:
                        added_count += 1
            current += timedelta(days=1)
        
        conn.commit()
        cursor.close()
        release_db_connection(conn)
        
        return jsonify({'success': True, 'added': added_count})
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/availability/check', methods=['POST'])
def api_check_availability():
    """Check availability for given dates and players"""
    try:
        data = request.json
        dates = data.get('dates', [])
        players = data.get('players', 4)
        
        if not dates:
            return jsonify({'success': False, 'error': 'Dates required'}), 400
        
        results = check_availability_db(dates, players, DEFAULT_COURSE_ID)
        
        return jsonify({'success': True, 'results': results, 'count': len(results)})
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# INITIALIZE
# ============================================================================

logging.info("="*60)
logging.info("Royal Portrush Golf Club - Email Bot")
logging.info("="*60)
logging.info("Availability Source: LOCAL DATABASE")
logging.info("Email Flow: Inquiry -> Requested -> Confirmed")
logging.info("="*60)

if init_db_pool():
    init_database()
    logging.info("Database ready")

logging.info(f"SendGrid: {FROM_EMAIL}")
logging.info(f"Club Email: {CLUB_BOOKING_EMAIL}")
logging.info(f"Green Fee: {CURRENCY_SYMBOL}{PER_PLAYER_FEE}")
logging.info("="*60)

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
