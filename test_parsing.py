#!/usr/bin/env python3
"""
Test Email Parsing - Verify date and player extraction
"""

import re
import logging
from datetime import datetime
from dateutil import parser as date_parser

logging.basicConfig(level=logging.INFO, format="%(message)s")

# Copy the parse_email_simple function here for testing
def parse_email_simple_test(subject: str, body: str):
    """Parse email to extract dates and player count - Test version"""
    full_text = f"{subject}\n{body}"
    full_text_lower = full_text.lower()
    result = {'players': 4, 'dates': []}

    print(f"\n{'='*70}")
    print(f"🔍 PARSING EMAIL")
    print(f"{'='*70}")
    print(f"Subject: {subject}")
    print(f"Body: {body[:150]}...")
    print()

    # Extract player count
    player_patterns = [
        r'(\d+)\s*(?:players?|people|persons?|golfers?|guests?)',
        r'(?:party|group)\s+of\s+(\d+)',
        r'(\d+)[-\s]ball',
        r'(?:foursome|four\s*ball)',
        r'(?:twosome|two\s*ball)',
        r'for\s+(\d+)',
        r'we\s+(?:are|have)\s+(\d+)',
    ]

    player_found = False
    for pattern in player_patterns:
        match = re.search(pattern, full_text_lower)
        if match:
            if pattern in [r'(?:foursome|four\s*ball)', r'(?:twosome|two\s*ball)']:
                num = 4 if 'four' in pattern else 2
            else:
                num = int(match.group(1))

            if 1 <= num <= 20:
                result['players'] = num
                player_found = True
                print(f"✅ PLAYERS: {num} (matched: '{pattern[:40]}')")
                break

    if not player_found:
        print(f"ℹ️  PLAYERS: 4 (default)")

    # Extract dates
    date_patterns = [
        (r'(\d{4}-\d{2}-\d{2})', 'iso'),
        (r'(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4})', 'dmy_full'),
        (r'(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2})(?!\d)', 'dmy_short'),
        (r'(\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{4})', 'dmy_named_year'),
        (r'((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?\s*,?\s+\d{4})', 'mdy_named_year'),
        (r'(\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*)', 'dmy_named'),
        (r'((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?)', 'mdy_named'),
    ]

    dates_found = []
    print()
    for pattern, pattern_name in date_patterns:
        for match in re.finditer(pattern, full_text_lower, re.IGNORECASE):
            date_str = match.group(1).strip()

            try:
                if pattern_name == 'iso':
                    parsed_date = datetime.strptime(date_str, '%Y-%m-%d')
                elif pattern_name.startswith('dmy'):
                    parsed_date = date_parser.parse(date_str, fuzzy=True, dayfirst=True, default=datetime.now().replace(day=1))
                elif pattern_name.startswith('mdy'):
                    parsed_date = date_parser.parse(date_str, fuzzy=True, dayfirst=False, default=datetime.now().replace(day=1))
                else:
                    parsed_date = date_parser.parse(date_str, fuzzy=True, dayfirst=True, default=datetime.now().replace(day=1))

                today = datetime.now().date()
                two_years_ahead = today.replace(year=today.year + 2)

                if parsed_date.date() >= today and parsed_date.date() <= two_years_ahead:
                    formatted = parsed_date.strftime('%Y-%m-%d')
                    if formatted not in dates_found:
                        dates_found.append(formatted)
                        print(f"✅ DATE: {formatted} <- '{date_str}' ({pattern_name})")

            except Exception as e:
                print(f"❌ FAILED: '{date_str}' ({pattern_name}): {e}")
                continue

    result['dates'] = sorted(dates_found)

    print()
    print(f"{'='*70}")
    print(f"📊 RESULT: {result['players']} players, {len(result['dates'])} date(s)")
    print(f"{'='*70}")
    return result


# Test cases
test_emails = [
    {
        "name": "UK Date Format with Players",
        "subject": "Tee Time Inquiry",
        "body": "Hi, we have 4 players and would like to book on 25/12/2025"
    },
    {
        "name": "US Date Format",
        "subject": "Booking Request",
        "body": "I need a tee time for 6 people on 12/25/2025"
    },
    {
        "name": "Natural Language Date",
        "subject": "Tee time",
        "body": "Can we book for December 25th 2025? Party of 8 golfers."
    },
    {
        "name": "Multiple Dates",
        "subject": "Availability check",
        "body": "Looking for availability on 1st January 2026 or 2nd January 2026 for 4 players"
    },
    {
        "name": "Foursome (4-ball)",
        "subject": "Booking",
        "body": "We'd like to book a foursome on Jan 15 2026"
    },
    {
        "name": "ISO Format",
        "subject": "Request",
        "body": "Booking for 2 people on 2026-01-20"
    },
    {
        "name": "Tricky Format",
        "subject": "Enquiry",
        "body": "We are 6 and want to play on the 3rd of March 2026"
    },
    {
        "name": "Short Year Format",
        "subject": "Booking",
        "body": "Group of 5 players for 15/03/26"
    },
    {
        "name": "Mixed Format",
        "subject": "Tee Times",
        "body": "Booking for 4 on April 21st, 2026 or 22/04/2026"
    },
]

if __name__ == "__main__":
    print("\n" + "="*70)
    print("EMAIL PARSING TEST SUITE")
    print("="*70)

    for i, test in enumerate(test_emails, 1):
        print(f"\n\n📧 TEST {i}: {test['name']}")
        result = parse_email_simple_test(test['subject'], test['body'])

        print(f"\n   PARSED: {result['players']} players")
        if result['dates']:
            print(f"   DATES:")
            for date in result['dates']:
                print(f"      - {date}")
        else:
            print(f"   DATES: None found")

    print("\n\n" + "="*70)
    print("✅ TEST SUITE COMPLETE")
    print("="*70)
