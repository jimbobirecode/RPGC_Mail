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


def check_availability_db(dates: List[str], players: int, club: str = None) -> List[Dict]:
    """
    Check tee time availability from the database
    Returns list of available slots based on recurring weekly template
    """
    if club is None:
        club = DEFAULT_COURSE_ID

    logging.info(f"🔍 DATABASE QUERY - Club: {club}, Dates: {dates}, Players: {players}")

    conn = None
    results = []

    try:
        conn = get_db_connection()
        if not conn:
            logging.error("❌ DATABASE - No connection available")
            return []

        cursor = conn.cursor(cursor_factory=RealDictCursor)

        for date_str in dates:
            # Parse date and get day of week
            try:
                date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                day_name = date_obj.strftime('%A')  # e.g., 'Monday'
                day_name_upper = day_name.upper()    # e.g., 'MONDAY'

                logging.info(f"📅 CHECKING - {date_str} ({day_name})")

            except ValueError:
                logging.warning(f"⚠️  INVALID DATE FORMAT - {date_str}")
                continue

            # Check if date is blocked
            cursor.execute("""
                SELECT reason FROM blocked_dates
                WHERE date = %s
            """, (date_str,))

            blocked = cursor.fetchone()
            if blocked:
                logging.info(f"🚫 DATE BLOCKED - {date_str}: {blocked.get('reason', 'No reason given')}")
                continue

            # Check day of week restrictions
            # Wednesday: No visitor bookings
            # Saturday/Sunday: Limited visitor times
            if date_obj.weekday() == 2:  # Wednesday
                logging.info(f"🚫 DATE EXCLUDED - {date_str} ({day_name}): No visitor bookings on Wednesdays")
                continue

            # Query available tee times from recurring weekly template
            logging.info(f"🔎 QUERYING - tee_sheet.tee_times for day_of_week = '{day_name_upper}'")

            cursor.execute("""
                SELECT
                    id,
                    day_of_week,
                    tee_time,
                    period,
                    max_players,
                    is_available,
                    notes
                FROM tee_sheet.tee_times
                WHERE day_of_week = %s
                AND is_available = TRUE
                AND max_players >= %s
                ORDER BY tee_time ASC
            """, (day_name_upper, players))

            date_results = cursor.fetchall()
            slots_found = len(date_results)

            logging.info(f"📊 QUERY RESULT - Found {slots_found} tee time template(s) for {day_name_upper}")

            if slots_found > 0:
                logging.info(f"✅ AVAILABILITY - {date_str} ({day_name}): {slots_found} tee time(s) found")
                for slot in date_results:
                    # Convert time object to string
                    time_str = str(slot['tee_time'])
                    if len(time_str) == 8:  # HH:MM:SS format
                        time_str = time_str[:5]  # Convert to HH:MM

                    available_slots = slot['max_players']

                    logging.info(f"   • {time_str} ({slot['period']}) - {available_slots} slots - £{PER_PLAYER_FEE:.2f} per player")

                    results.append({
                        'date': date_str,
                        'time': time_str,
                        'available_slots': available_slots,
                        'green_fee': PER_PLAYER_FEE
                    })
            else:
                logging.info(f"❌ NO AVAILABILITY - {date_str} ({day_name}): No tee times found for {day_name_upper} with {players}+ slots")

        cursor.close()

        logging.info(f"📋 TOTAL RESULTS - {len(results)} available tee time(s) across all dates")
        return results

    except Exception as e:
        logging.error(f"❌ DATABASE ERROR - {e}")
        logging.exception("Full traceback:")
        return []
    finally:
        if conn:
            release_db_connection(conn)


def save_booking_to_db(booking_data: dict):
    """Save booking to PostgreSQL"""
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return False

        cursor = conn.cursor()
        
        if 'booking_id' not in booking_data or not booking_data['booking_id']:
            booking_id = generate_booking_id(booking_data['guest_email'], booking_data['timestamp'])
            booking_data['booking_id'] = booking_id
        else:
            booking_id = booking_data['booking_id']

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
        return booking_id

    except Exception as e:
        logging.error(f"Failed to save booking: {e}")
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
            return False

        cursor = conn.cursor()
        set_clauses = []
        params = {'booking_id': booking_id}

        for key, value in updates.items():
            if key in ['status', 'note', 'players', 'total', 'date', 'tee_time']:
                set_clauses.append(f"{key} = %({key})s")
                params[key] = value

        if not set_clauses:
            return False

        set_clauses.append("updated_at = CURRENT_TIMESTAMP")

        cursor.execute(f"""
            UPDATE bookings SET {', '.join(set_clauses)}
            WHERE booking_id = %(booking_id)s
        """, params)

        conn.commit()
        cursor.close()
        return cursor.rowcount > 0

    except Exception as e:
        logging.error(f"Database update failed: {e}")
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
    """Royal Portrush Golf Club branded email header"""
    return f"""
    <!DOCTYPE html>
    <html xmlns="http://www.w3.org/1999/xhtml">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Royal Portrush Golf Club - Booking</title>
        <style type="text/css">
            body {{
                margin: 0; padding: 0; width: 100%;
                font-family: Georgia, 'Times New Roman', serif;
                background-color: {ROYAL_PORTRUSH_COLORS['light_grey']};
            }}
            .email-container {{
                background: #ffffff;
                border-radius: 12px;
                max-width: 800px;
                margin: 20px auto;
                box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            }}
            .header {{
                background: linear-gradient(135deg, {ROYAL_PORTRUSH_COLORS['navy_primary']} 0%, {ROYAL_PORTRUSH_COLORS['burgundy']} 100%);
                padding: 40px 30px;
                text-align: center;
                border-radius: 12px 12px 0 0;
            }}
            .header h1 {{ color: #ffffff; font-size: 28px; margin: 0 0 10px 0; }}
            .header p {{ color: {ROYAL_PORTRUSH_COLORS['off_white']}; font-size: 16px; margin: 0; }}
            .content {{ padding: 40px 30px; }}
            .info-box {{
                background: {ROYAL_PORTRUSH_COLORS['info_bg']};
                border-left: 4px solid {ROYAL_PORTRUSH_COLORS['navy_primary']};
                border-radius: 8px;
                padding: 20px;
                margin: 20px 0;
            }}
            .tee-table {{
                width: 100%;
                border-collapse: collapse;
                margin: 25px 0;
                border: 1px solid {ROYAL_PORTRUSH_COLORS['border_grey']};
            }}
            .tee-table thead {{
                background: {ROYAL_PORTRUSH_COLORS['navy_primary']};
                color: #ffffff;
            }}
            .tee-table th {{
                padding: 15px 12px;
                text-align: left;
                font-size: 12px;
                text-transform: uppercase;
            }}
            .tee-table td {{
                padding: 15px 12px;
                border-bottom: 1px solid {ROYAL_PORTRUSH_COLORS['border_grey']};
            }}
            .footer {{
                background: linear-gradient(135deg, {ROYAL_PORTRUSH_COLORS['navy_primary']} 0%, {ROYAL_PORTRUSH_COLORS['burgundy']} 100%);
                padding: 30px;
                text-align: center;
                color: #ffffff;
                border-radius: 0 0 12px 12px;
            }}
        </style>
    </head>
    <body>
        <table role="presentation" width="100%" style="background-color: {ROYAL_PORTRUSH_COLORS['light_grey']};">
            <tr>
                <td style="padding: 20px;">
                    <table class="email-container" align="center" width="800">
                        <tr>
                            <td class="header">
                                <img src="https://raw.githubusercontent.com/jimbobirecode/TeeMail-Assests/refs/heads/main/Royal-Portrush-Golf-Club-Logo.svg" alt="Royal Portrush Golf Club" style="max-width: 120px; margin-bottom: 20px;" />
                                <h1>Royal Portrush Golf Club</h1>
                                <p>Available Tee Times for Your Round</p>
                            </td>
                        </tr>
                        <tr>
                            <td class="content">
    """


def get_email_footer():
    """Royal Portrush Golf Club branded email footer"""
    return f"""
                            </td>
                        </tr>
                        <tr>
                            <td class="footer">
                                <p style="margin: 0 0 10px 0;">We look forward to welcoming you to Royal Portrush Golf Club!</p>
                                <p style="margin: 0 0 15px 0;">
                                    <strong style="color: {ROYAL_PORTRUSH_COLORS['championship_gold']};">Royal Portrush Golf Club</strong>
                                </p>
                                <p style="margin: 0; font-size: 13px;">
                                    Questions? Email us at 
                                    <a href="mailto:{CLUB_BOOKING_EMAIL}" style="color: {ROYAL_PORTRUSH_COLORS['championship_gold']};">{CLUB_BOOKING_EMAIL}</a>
                                </p>
                                <p style="margin-top: 15px; color: {ROYAL_PORTRUSH_COLORS['text_light']}; font-size: 11px;">
                                    Powered by TeeMail · Automated Visitor Booking
                                </p>
                            </td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """


def build_booking_link(date: str, time: str, players: int, guest_email: str, booking_id: str = None) -> str:
    """Generate mailto link for Book Now button"""
    tracking_email = f"{TRACKING_EMAIL_PREFIX}@bookings.teemail.io"
    subject = quote(f"BOOKING REQUEST - {date} at {time}")
    
    body_lines = [
        f"I would like to book the following tee time:",
        f"",
        f"Booking Details:",
        f"- Date: {date}",
        f"- Time: {time}",
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


def format_inquiry_email(results: list, player_count: int, guest_email: str, booking_id: str = None) -> str:
    """Generate inquiry email with available tee times"""
    html = get_email_header()
    
    # Get date range
    dates_list = sorted(list(set([r["date"] for r in results])))
    
    html += f"""
        <p style="color: {ROYAL_PORTRUSH_COLORS['text_dark']}; font-size: 16px; line-height: 1.8;">
            Thank you for your enquiry! We're delighted to share available tee times for your round at Royal Portrush.
        </p>
        
        <div class="info-box">
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
            <table class="tee-table">
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
            booking_link = build_booking_link(date, time, player_count, guest_email, booking_id)
            
            html += f"""
                <tr>
                    <td><strong style="color: {ROYAL_PORTRUSH_COLORS['navy_primary']};">{time}</strong></td>
                    <td>{player_count} players</td>
                    <td style="color: {ROYAL_PORTRUSH_COLORS['burgundy']}; font-weight: 700;">{CURRENCY_SYMBOL}{green_fee:.2f}</td>
                    <td style="text-align: center;">
                        <a href="{booking_link}" style="background: linear-gradient(135deg, {ROYAL_PORTRUSH_COLORS['burgundy']} 0%, {ROYAL_PORTRUSH_COLORS['navy_primary']} 100%); color: #ffffff; padding: 10px 20px; text-decoration: none; border-radius: 6px; font-weight: 600; font-size: 13px;">Book Now</a>
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
        
        <div class="info-box">
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
        
        <div class="info-box" style="border: 2px solid {ROYAL_PORTRUSH_COLORS['success_green']};">
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


def format_no_availability_email(player_count: int, dates: list = None) -> str:
    """Generate email when no availability found"""
    html = get_email_header()
    
    html += f"""
        <p style="color: {ROYAL_PORTRUSH_COLORS['text_dark']}; font-size: 16px; line-height: 1.8;">
            Thank you for your enquiry at <strong>Royal Portrush Golf Club</strong>.
        </p>
        
        <div style="background: #fef2f2; border-left: 4px solid #dc2626; border-radius: 8px; padding: 20px; margin: 25px 0;">
            <h3 style="color: #dc2626; margin: 0 0 12px 0;">⚠️ No Availability Found</h3>
            <p style="margin: 0;">Unfortunately, we do not have availability for <strong>{player_count} player(s)</strong> on your requested dates.</p>
        </div>
        
        <div class="info-box">
            <h3 style="color: {ROYAL_PORTRUSH_COLORS['navy_primary']}; margin: 0 0 12px 0;">💡 Suggestions</h3>
            <ul style="margin: 10px 0; padding-left: 20px; font-size: 14px; line-height: 1.8;">
                <li>Try different dates (note: no visitor bookings on Wednesdays or weekends)</li>
                <li>Consider a smaller group size</li>
                <li>Contact us directly for alternative options</li>
            </ul>
        </div>
        
        <div class="info-box">
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
    """Parse email to extract dates and player count"""
    full_text = f"{subject}\n{body}".lower()
    result = {'players': 4, 'dates': []}

    # Extract players
    player_match = re.search(r'(\d+)\s*(?:players?|people|golfers?)', full_text)
    if player_match:
        num = int(player_match.group(1))
        if 1 <= num <= 20:
            result['players'] = num
            logging.info(f"📊 PARSED - Players: {num}")
        else:
            logging.info(f"📊 PARSED - Players: {num} (out of range, using default 4)")
    else:
        logging.info(f"📊 PARSED - Players: 4 (default, none specified)")

    # Extract dates - multiple patterns
    date_patterns = [
        r'(\d{4}-\d{2}-\d{2})',  # ISO format
        r'(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})',  # DD/MM/YYYY
        r'((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?(?:\s+\d{2,4})?)',
    ]

    dates_found = []
    for pattern in date_patterns:
        for match in re.finditer(pattern, full_text, re.IGNORECASE):
            date_str = match.group(1).strip()
            try:
                # Try to parse the date
                if re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
                    parsed_date = datetime.strptime(date_str, '%Y-%m-%d')
                else:
                    parsed_date = date_parser.parse(date_str, fuzzy=True, dayfirst=True, default=datetime.now().replace(day=1))

                # Only future dates
                if parsed_date.date() >= datetime.now().date():
                    formatted = parsed_date.strftime('%Y-%m-%d')
                    if formatted not in dates_found:
                        dates_found.append(formatted)
            except:
                continue

    result['dates'] = dates_found

    if dates_found:
        logging.info(f"📅 PARSED - Dates found: {', '.join(dates_found)}")
    else:
        logging.info(f"📅 PARSED - No dates found in email")

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
        
        body = text_body if text_body else html_body
        message_id = extract_message_id(headers)
        
        logging.info("="*60)
        logging.info(f"INBOUND EMAIL - From: {from_email}")
        logging.info(f"Subject: {subject}")
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
                    logging.info(f"📧 SENDING EMAIL - Available Tee Times ({len(results)} options) to {sender_email}")
                    logging.info(f"   Booking ID: {booking_id}")
                    logging.info(f"   Players: {parsed['players']}")
                    logging.info(f"   Green Fee: £{PER_PLAYER_FEE:.2f} per player")
                    logging.info(f"   Total: £{parsed['players'] * PER_PLAYER_FEE:.2f}")

                    html_email = format_inquiry_email(results, parsed['players'], sender_email, booking_id)
                    subject_line = "Available Tee Times at Royal Portrush Golf Club"
                else:
                    logging.info(f"📧 SENDING EMAIL - No Availability to {sender_email}")
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
