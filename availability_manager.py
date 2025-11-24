#!/usr/bin/env python3
"""
Tee Time Availability Manager for Royal Portrush Golf Club
==========================================================

Works with the Email Bot's tee_times table structure.

TEE_TIMES TABLE STRUCTURE (from email bot):
    tee_times (
        id, club, date, time, max_players, 
        available_slots,  ← This gets decremented when booking is Confirmed
        is_available,     ← Set to FALSE when fully booked
        green_fee, notes, created_at, updated_at
    )

SLOT RESERVATION LOGIC:
- "Inquiry" / "Pending" / "Requested" = available_slots UNCHANGED
- "Confirmed" / "Booked" = available_slots DECREMENTED
- Revert to "Requested" or "Cancelled" = available_slots INCREMENTED back

WORKFLOW:
1. Guest emails inquiry → Status: "Inquiry" → Slots unchanged
2. Guest clicks Book Now → Status: "Requested" → Slots unchanged  
3. Staff confirms → Status: "Confirmed" → DECREMENT available_slots
4. Payment received → Status: "Booked" → Slots stay decremented
5. If cancelled → Status: "Cancelled" → INCREMENT available_slots back
"""

import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional, Tuple
import logging
import os
import re

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Statuses that RESERVE a slot (available_slots is decremented)
SLOT_RESERVING_STATUSES = ['Confirmed', 'Booked']

# Statuses that do NOT reserve a slot
NON_RESERVING_STATUSES = ['Inquiry', 'Pending', 'Requested', 'Rejected', 'Cancelled']


class AvailabilityManager:
    """
    Manages tee time availability using the tee_times table.
    
    When a booking moves to 'Confirmed':
    - Decrement available_slots in tee_times
    - Set is_available = FALSE if available_slots reaches 0
    
    When a booking reverts from 'Confirmed' to 'Requested' or 'Cancelled':
    - Increment available_slots in tee_times
    - Set is_available = TRUE if it was FALSE
    """
    
    def __init__(self, db_connection_string: str = None):
        self.db_conn = db_connection_string or os.getenv("DATABASE_URL")
        self.DEFAULT_COURSE_ID = os.getenv("DEFAULT_COURSE_ID", "royalportrush")
    
    def get_connection(self):
        """Get database connection"""
        return psycopg2.connect(self.db_conn)
    
    def _normalize_time(self, time_str: str) -> str:
        """Normalize time format (handle '10:00 AM' vs '10:00')"""
        if not time_str:
            return time_str
        
        time_str = str(time_str).strip()
        
        # Extract just HH:MM
        match = re.search(r'(\d{1,2}:\d{2})', time_str)
        if match:
            return match.group(1)
        
        return time_str
    
    def _normalize_date(self, date_input) -> str:
        """Convert date to string format YYYY-MM-DD"""
        if isinstance(date_input, str):
            return date_input
        if hasattr(date_input, 'strftime'):
            return date_input.strftime('%Y-%m-%d')
        return str(date_input)
    
    def check_slot_availability(
        self, 
        requested_date, 
        requested_time: str, 
        num_players: int,
        club_id: str = None
    ) -> Dict:
        """
        Check if a specific time slot has enough available_slots.
        
        Reads directly from tee_times.available_slots column.
        
        Returns:
            {
                'available': bool,
                'available_slots': int,
                'max_players': int,
                'can_accommodate': bool,
                'tee_time_id': int or None
            }
        """
        club_id = club_id or self.DEFAULT_COURSE_ID
        date_str = self._normalize_date(requested_date)
        time_str = self._normalize_time(requested_time)
        
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Query the tee_times table directly
                cur.execute("""
                    SELECT 
                        id,
                        date,
                        time,
                        max_players,
                        available_slots,
                        is_available,
                        green_fee
                    FROM tee_times
                    WHERE club = %s 
                    AND date = %s
                    AND time = %s
                """, (club_id, date_str, time_str))
                
                slot = cur.fetchone()
                
                if not slot:
                    # No tee time exists for this date/time
                    return {
                        'available': False,
                        'available_slots': 0,
                        'max_players': 0,
                        'can_accommodate': False,
                        'tee_time_id': None,
                        'reason': 'Tee time slot does not exist'
                    }
                
                available = slot['available_slots'] or 0
                max_players = slot['max_players'] or 4
                is_available = slot['is_available']
                
                can_accommodate = is_available and available >= num_players
                
                return {
                    'available': is_available and available > 0,
                    'available_slots': available,
                    'max_players': max_players,
                    'can_accommodate': can_accommodate,
                    'tee_time_id': slot['id'],
                    'green_fee': float(slot['green_fee']) if slot['green_fee'] else None,
                    'requested_players': num_players,
                    'date': date_str,
                    'time': time_str
                }
    
    def can_confirm_booking(
        self,
        booking_id: str,
        club_id: str = None
    ) -> Tuple[bool, str, Dict]:
        """
        Check if a booking can be moved from 'Requested' to 'Confirmed'.
        
        This checks the tee_times.available_slots to see if there's room.
        
        Returns:
            (can_confirm: bool, message: str, availability: dict)
        """
        club_id = club_id or self.DEFAULT_COURSE_ID
        
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Get the booking details
                cur.execute("""
                    SELECT booking_id, date, tee_time, players, status, club
                    FROM bookings
                    WHERE booking_id = %s
                """, (booking_id,))
                
                booking = cur.fetchone()
                
                if not booking:
                    return False, "Booking not found", {}
                
                current_status = booking['status']
                
                # Already confirmed or booked - no need to check
                if current_status in SLOT_RESERVING_STATUSES:
                    return False, f"Booking is already {current_status}", {}
                
                # Must be in Requested/Inquiry/Pending to confirm
                if current_status not in ['Requested', 'Inquiry', 'Pending']:
                    return False, f"Cannot confirm booking with status '{current_status}'", {}
                
                booking_date = booking['date']
                booking_time = booking['tee_time']
                players = booking['players'] or 1
                
                if not booking_date or not booking_time:
                    return False, "Booking has no date or time set", {}
                
                # Check availability in tee_times table
                availability = self.check_slot_availability(
                    requested_date=booking_date,
                    requested_time=booking_time,
                    num_players=players,
                    club_id=booking['club'] or club_id
                )
                
                if availability['can_accommodate']:
                    return True, f"Slot available - {availability['available_slots']} spots remaining", availability
                else:
                    if availability['tee_time_id'] is None:
                        return False, "Tee time slot does not exist in system", availability
                    else:
                        return False, f"Only {availability['available_slots']} spots available, need {players}", availability
    
    def confirm_booking(
        self,
        booking_id: str,
        confirmed_by: str
    ) -> Tuple[bool, str]:
        """
        Move booking from 'Requested' to 'Confirmed' and DECREMENT available_slots.
        
        This is the key function - it:
        1. Checks availability
        2. Updates booking status to 'Confirmed'
        3. Decrements tee_times.available_slots
        4. Sets is_available = FALSE if slots reach 0
        
        Returns:
            (success: bool, message: str)
        """
        # First check if we can confirm
        can_confirm, message, availability = self.can_confirm_booking(booking_id)
        
        if not can_confirm:
            logger.warning(f"Cannot confirm booking {booking_id}: {message}")
            return False, message
        
        tee_time_id = availability.get('tee_time_id')
        players = availability.get('requested_players', 1)
        
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    # Start transaction
                    
                    # 1. Update booking status
                    cur.execute("""
                        UPDATE bookings
                        SET status = 'Confirmed',
                            updated_at = NOW(),
                            updated_by = %s,
                            customer_confirmed_at = NOW()
                        WHERE booking_id = %s
                        AND status IN ('Requested', 'Inquiry', 'Pending')
                    """, (confirmed_by, booking_id))
                    
                    if cur.rowcount == 0:
                        conn.rollback()
                        return False, "Booking not found or already confirmed"
                    
                    # 2. Decrement available_slots in tee_times
                    cur.execute("""
                        UPDATE tee_times
                        SET available_slots = available_slots - %s,
                            is_available = CASE 
                                WHEN available_slots - %s <= 0 THEN FALSE 
                                ELSE TRUE 
                            END,
                            updated_at = NOW()
                        WHERE id = %s
                        AND available_slots >= %s
                    """, (players, players, tee_time_id, players))
                    
                    if cur.rowcount == 0:
                        conn.rollback()
                        return False, "Failed to update tee time slots - may have been booked by someone else"
                    
                    conn.commit()
                    
                    new_available = availability['available_slots'] - players
                    logger.info(f"Booking {booking_id} confirmed by {confirmed_by}")
                    logger.info(f"Tee time {tee_time_id}: {availability['available_slots']} → {new_available} slots")
                    
                    return True, f"Booking confirmed! {new_available} spots remaining for this time."
                    
                except Exception as e:
                    conn.rollback()
                    logger.error(f"Error confirming booking: {e}")
                    return False, str(e)
    
    def release_booking_slot(
        self,
        booking_id: str,
        released_by: str,
        new_status: str = 'Requested'
    ) -> Tuple[bool, str]:
        """
        Release a slot when reverting from 'Confirmed' back to 'Requested' or 'Cancelled'.
        
        This INCREMENTS available_slots back.
        
        Use this when:
        - Staff clicks "← Requested" to revert a confirmed booking
        - Staff cancels a confirmed booking
        """
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                try:
                    # Get booking details
                    cur.execute("""
                        SELECT booking_id, date, tee_time, players, status, club
                        FROM bookings
                        WHERE booking_id = %s
                    """, (booking_id,))
                    
                    booking = cur.fetchone()
                    
                    if not booking:
                        return False, "Booking not found"
                    
                    current_status = booking['status']
                    
                    # Only release slots if currently Confirmed or Booked
                    if current_status not in SLOT_RESERVING_STATUSES:
                        # Just update status, no slot release needed
                        cur.execute("""
                            UPDATE bookings
                            SET status = %s, updated_at = NOW(), updated_by = %s
                            WHERE booking_id = %s
                        """, (new_status, released_by, booking_id))
                        conn.commit()
                        return True, f"Status changed to {new_status}"
                    
                    players = booking['players'] or 1
                    booking_date = booking['date']
                    booking_time = self._normalize_time(booking['tee_time'])
                    club_id = booking['club'] or self.DEFAULT_COURSE_ID
                    
                    # Find the tee_time record
                    date_str = self._normalize_date(booking_date)
                    
                    cur.execute("""
                        SELECT id, max_players, available_slots
                        FROM tee_times
                        WHERE club = %s AND date = %s AND time = %s
                    """, (club_id, date_str, booking_time))
                    
                    tee_time = cur.fetchone()
                    
                    if not tee_time:
                        # No tee_time record - just update booking status
                        cur.execute("""
                            UPDATE bookings
                            SET status = %s, updated_at = NOW(), updated_by = %s
                            WHERE booking_id = %s
                        """, (new_status, released_by, booking_id))
                        conn.commit()
                        return True, f"Status changed to {new_status} (no tee time record to update)"
                    
                    tee_time_id = tee_time['id']
                    max_players = tee_time['max_players']
                    
                    # 1. Update booking status
                    cur.execute("""
                        UPDATE bookings
                        SET status = %s, updated_at = NOW(), updated_by = %s
                        WHERE booking_id = %s
                    """, (new_status, released_by, booking_id))
                    
                    # 2. Increment available_slots (but don't exceed max_players)
                    cur.execute("""
                        UPDATE tee_times
                        SET available_slots = LEAST(available_slots + %s, max_players),
                            is_available = TRUE,
                            updated_at = NOW()
                        WHERE id = %s
                    """, (players, tee_time_id))
                    
                    conn.commit()
                    
                    new_available = min(tee_time['available_slots'] + players, max_players)
                    logger.info(f"Booking {booking_id} released by {released_by}")
                    logger.info(f"Tee time {tee_time_id}: slots restored to {new_available}")
                    
                    return True, f"Slot released! {new_available} spots now available."
                    
                except Exception as e:
                    conn.rollback()
                    logger.error(f"Error releasing slot: {e}")
                    return False, str(e)
    
    def get_available_times_for_date(
        self, 
        requested_date,
        min_players: int = 1,
        club_id: str = None
    ) -> List[Dict]:
        """
        Get all available time slots for a specific date.
        
        Reads directly from tee_times table.
        """
        club_id = club_id or self.DEFAULT_COURSE_ID
        date_str = self._normalize_date(requested_date)
        
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT 
                        id,
                        time,
                        max_players,
                        available_slots,
                        green_fee
                    FROM tee_times
                    WHERE club = %s 
                    AND date = %s
                    AND is_available = TRUE
                    AND available_slots >= %s
                    ORDER BY time ASC
                """, (club_id, date_str, min_players))
                
                slots = cur.fetchall()
                
                return [
                    {
                        'tee_time_id': slot['id'],
                        'time': slot['time'],
                        'max_players': slot['max_players'],
                        'available_slots': slot['available_slots'],
                        'green_fee': float(slot['green_fee']) if slot['green_fee'] else None,
                        'date': date_str
                    }
                    for slot in slots
                ]
    
    def get_daily_availability_report(
        self,
        start_date,
        end_date,
        club_id: str = None
    ) -> List[Dict]:
        """
        Get availability report for a date range.
        """
        club_id = club_id or self.DEFAULT_COURSE_ID
        start_str = self._normalize_date(start_date)
        end_str = self._normalize_date(end_date)
        
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT 
                        date,
                        COUNT(*) as slot_count,
                        SUM(max_players) as total_capacity,
                        SUM(available_slots) as total_available,
                        SUM(max_players - available_slots) as total_booked
                    FROM tee_times
                    WHERE club = %s
                    AND date >= %s
                    AND date <= %s
                    GROUP BY date
                    ORDER BY date ASC
                """, (club_id, start_str, end_str))
                
                results = cur.fetchall()
                
                report = []
                for row in results:
                    total_capacity = row['total_capacity'] or 0
                    total_booked = row['total_booked'] or 0
                    utilization = (total_booked / total_capacity * 100) if total_capacity > 0 else 0
                    
                    date_obj = row['date']
                    day_name = date_obj.strftime('%A') if hasattr(date_obj, 'strftime') else ''
                    
                    report.append({
                        'date': self._normalize_date(row['date']),
                        'day': day_name,
                        'slot_count': row['slot_count'],
                        'total_capacity': int(total_capacity),
                        'total_available': int(row['total_available'] or 0),
                        'total_booked': int(total_booked),
                        'utilization_pct': round(utilization, 1)
                    })
                
                return report


# ========================================
# DASHBOARD INTEGRATION FUNCTION
# ========================================

def update_booking_status_with_availability(
    booking_id: str, 
    new_status: str, 
    updated_by: str,
    db_url: str = None
) -> Tuple[bool, str]:
    """
    Update booking status with proper slot management.
    
    USE THIS INSTEAD OF the simple update_booking_status() in your dashboard.
    
    Handles:
    - Moving TO 'Confirmed': Checks availability, decrements slots
    - Moving FROM 'Confirmed' to something else: Releases slots
    - Other status changes: Just updates status
    """
    db_url = db_url or os.getenv("DATABASE_URL")
    manager = AvailabilityManager(db_url)
    
    # First, get current booking status
    with manager.get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT status FROM bookings WHERE booking_id = %s", (booking_id,))
            result = cur.fetchone()
            current_status = result['status'] if result else None
    
    if not current_status:
        return False, "Booking not found"
    
    # CASE 1: Moving TO Confirmed (need to reserve slot)
    if new_status == 'Confirmed' and current_status not in SLOT_RESERVING_STATUSES:
        return manager.confirm_booking(booking_id, updated_by)
    
    # CASE 2: Moving FROM Confirmed/Booked to non-reserving status (need to release slot)
    if current_status in SLOT_RESERVING_STATUSES and new_status not in SLOT_RESERVING_STATUSES:
        return manager.release_booking_slot(booking_id, updated_by, new_status)
    
    # CASE 3: Other status changes (no slot impact)
    try:
        with manager.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE bookings 
                    SET status = %s, updated_at = NOW(), updated_by = %s
                    WHERE booking_id = %s
                """, (new_status, updated_by, booking_id))
                conn.commit()
        
        return True, f"Status updated to {new_status}"
        
    except Exception as e:
        logger.error(f"Error updating status: {e}")
        return False, str(e)


# ========================================
# EXAMPLE USAGE
# ========================================

if __name__ == "__main__":
    manager = AvailabilityManager()
    
    print("\n" + "="*60)
    print("AVAILABILITY MANAGER TEST")
    print("="*60)
    
    # Test: Check a slot
    test_date = "2025-11-24"
    test_time = "10:00"
    
    result = manager.check_slot_availability(test_date, test_time, 4)
    print(f"\nSlot {test_date} at {test_time}:")
    print(f"  Available: {result['available']}")
    print(f"  Spots: {result['available_slots']}/{result['max_players']}")
    print(f"  Can fit 4: {result['can_accommodate']}")
    
    # Test: Get available times
    print(f"\nAvailable times for {test_date}:")
    times = manager.get_available_times_for_date(test_date, min_players=2)
    for t in times[:5]:
        print(f"  {t['time']}: {t['available_slots']}/{t['max_players']} spots")
