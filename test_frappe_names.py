#!/usr/bin/env python3
"""Test different doctype names."""

import requests
import json
import os
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

load_dotenv()

frappe_url = os.environ.get("FRAPPE_LISTENER_URL")
api_key = os.environ.get("FRAPPE_API_KEY")
api_secret = os.environ.get("FRAPPE_API_SECRET")

frappe_base_url = frappe_url.rsplit("/api/resource/", 1)[0]
auth = HTTPBasicAuth(api_key, api_secret) if api_key else None

# Try different doctype names
doctype_names = [
    "Whatsapp Customer Group",
    "WhatsApp Customer Group",
    "WhatsApp Customer Group Map",
    "WhatsApp Group Mapping",
    "Customer Group Map",
    "Whatsapp Group Mapping",
]

print("Testing different doctype names...\n")

for doctype in doctype_names:
    response = requests.get(
        f"{frappe_base_url}/api/resource/{doctype}",
        auth=auth,
        headers={"Content-Type": "application/json"},
        params={"limit_page_length": 1}
    )
    status = response.status_code
    symbol = "✅" if status == 200 else "❌" if status == 500 else "⚠️"
    print(f"{symbol} {doctype:<40} → {status}")

    if status == 200:
        data = response.json()
        print(f"   Found {len(data.get('data', []))} records")
        if data.get('data'):
            print(f"   Record keys: {list(data['data'][0].keys())[:5]}")
    elif status == 500:
        # Try to extract the error
        error = response.json().get("exception", "Unknown error")
        if "does not exist" in error.lower():
            print(f"   → Doctype not found")

print("\nDone!")
