#!/usr/bin/env python3
"""Test database connection with detailed error messages."""

import os
from dotenv import load_dotenv
import pymysql
import sys

load_dotenv()

print("=" * 60)
print("DATABASE CONNECTION TEST")
print("=" * 60)

# Check env vars are loaded
required = ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"]
print("\n1. Checking environment variables:")
for var in required:
    value = os.environ.get(var)
    if var == "DB_PASSWORD":
        display = "***" if value else "(not set)"
    else:
        display = value or "(not set)"
    status = "✓" if value else "✗"
    print(f"   {status} {var:20} = {display}")

missing = [k for k in required if not os.environ.get(k)]
if missing:
    print(f"\n✗ MISSING: {', '.join(missing)}")
    sys.exit(1)

print("\n2. Attempting connection to MySQL...")
try:
    conn = pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
    )
    print(f"   ✓ Connected to {os.environ['DB_HOST']}:{os.environ['DB_PORT']}")

    # Test query
    cursor = conn.cursor()
    cursor.execute("SELECT 1 as test")
    result = cursor.fetchone()
    print(f"   ✓ Query successful: {result}")

    # List tables
    cursor.execute("SHOW TABLES")
    tables = cursor.fetchall()
    print(f"   ✓ Found {len(tables)} table(s)")

    # Check for our tables
    table_names = [t.get('Tables_in_' + os.environ['DB_NAME']) for t in tables]
    if 'whatsapp_raw_messages' in table_names:
        print(f"   ✓ whatsapp_raw_messages table exists")
    else:
        print(f"   ⚠ whatsapp_raw_messages table NOT found")

    if 'tabPulse Support Ticket' in table_names:
        print(f"   ✓ tabPulse Support Ticket table exists")
    else:
        print(f"   ⚠ tabPulse Support Ticket table NOT found")

    conn.close()
    print("\n✅ SUCCESS! Database connection is working.")

except pymysql.err.OperationalError as e:
    error_code, error_msg = e.args
    print(f"\n✗ MYSQL ERROR {error_code}: {error_msg}")

    if error_code == 1045:
        print("   → Check username and password")
    elif error_code == 1049:
        print("   → Database does not exist")
    elif error_code == 2003:
        print("   → Cannot reach server (network/firewall issue)")
    elif error_code == 2006:
        print("   → Connection lost (server may have closed it)")

    sys.exit(1)

except Exception as e:
    print(f"\n✗ ERROR: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
