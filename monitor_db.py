#!/usr/bin/env python3
"""Monitor database tables in real-time."""

import pymysql
from dotenv import load_dotenv
import os
import time
from datetime import datetime

load_dotenv()

conn = pymysql.connect(
    host=os.environ["DB_HOST"],
    port=int(os.environ["DB_PORT"]),
    user=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"],
    database=os.environ["DB_NAME"],
    cursorclass=pymysql.cursors.DictCursor,
)

def show_stats():
    """Display current database stats."""
    cursor = conn.cursor()

    print("\n" + "=" * 70)
    print(f"DATABASE STATUS — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    # Raw messages stats
    cursor.execute("SELECT COUNT(*) as total, SUM(processed) as processed FROM whatsapp_raw_messages")
    raw_stats = cursor.fetchone()
    total_msgs = raw_stats.get('total', 0) or 0
    processed_msgs = raw_stats.get('processed', 0) or 0
    unprocessed_msgs = total_msgs - processed_msgs

    print(f"\n📨 WHATSAPP_RAW_MESSAGES:")
    print(f"   Total messages:       {total_msgs}")
    print(f"   Processed:            {processed_msgs}")
    print(f"   Pending:              {unprocessed_msgs}")

    if unprocessed_msgs > 0:
        cursor.execute("""
            SELECT group_id, COUNT(*) as count
            FROM whatsapp_raw_messages
            WHERE processed = 0
            GROUP BY group_id
        """)
        groups = cursor.fetchall()
        print(f"\n   Unprocessed by group:")
        for group in groups:
            print(f"      • {group['group_id']}: {group['count']} message(s)")

    # Recent messages
    cursor.execute("""
        SELECT id, group_name, sender_name, message_type, received_at
        FROM whatsapp_raw_messages
        ORDER BY received_at DESC
        LIMIT 3
    """)
    recent = cursor.fetchall()
    if recent:
        print(f"\n   Latest messages:")
        for msg in recent:
            print(f"      • [{msg['message_type']:6}] {msg['sender_name']} → {msg['group_name']}")

    # Tickets stats
    cursor.execute("SELECT COUNT(*) as total FROM `tabPulse Support Ticket` WHERE module = 'WhatsApp'")
    ticket_stats = cursor.fetchone()
    total_tickets = ticket_stats.get('total', 0) or 0

    print(f"\n🎫 TABPULSE SUPPORT TICKET (WhatsApp module):")
    print(f"   Total tickets:        {total_tickets}")

    # Tickets by status
    cursor.execute("""
        SELECT status, COUNT(*) as count
        FROM `tabPulse Support Ticket`
        WHERE module = 'WhatsApp'
        GROUP BY status
    """)
    statuses = cursor.fetchall()
    if statuses:
        print(f"   By status:")
        for status in statuses:
            print(f"      • {status['status']}: {status['count']}")

    # Tickets by category
    cursor.execute("""
        SELECT category, COUNT(*) as count
        FROM `tabPulse Support Ticket`
        WHERE module = 'WhatsApp'
        GROUP BY category
        ORDER BY count DESC
        LIMIT 5
    """)
    categories = cursor.fetchall()
    if categories:
        print(f"   Top categories:")
        for cat in categories:
            print(f"      • {cat['category']}: {cat['count']}")

    # Recent tickets
    cursor.execute("""
        SELECT name, title, category, status, creation
        FROM `tabPulse Support Ticket`
        WHERE module = 'WhatsApp'
        ORDER BY creation DESC
        LIMIT 3
    """)
    recent_tickets = cursor.fetchall()
    if recent_tickets:
        print(f"\n   Latest tickets:")
        for ticket in recent_tickets:
            title = ticket['title'][:40] + "..." if len(ticket['title']) > 40 else ticket['title']
            print(f"      • [{ticket['status']:6}] {title}")
            print(f"        ID: {ticket['name']}")

    print("\n" + "=" * 70)

try:
    while True:
        show_stats()
        print("\n(Refreshing in 10 seconds... Press Ctrl+C to stop)\n")
        time.sleep(10)
except KeyboardInterrupt:
    print("\n\nMonitoring stopped.")
finally:
    conn.close()
