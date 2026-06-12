#!/usr/bin/env python3
"""Test script to verify Frappe API returns WhatsApp group mappings correctly."""

import requests
import json
import os
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

load_dotenv()

frappe_url = os.environ.get("FRAPPE_LISTENER_URL")
api_key = os.environ.get("FRAPPE_API_KEY")
api_secret = os.environ.get("FRAPPE_API_SECRET")

if not frappe_url:
    print("❌ FRAPPE_LISTENER_URL not set")
    exit(1)

frappe_base_url = frappe_url.rsplit("/api/resource/", 1)[0]
auth = HTTPBasicAuth(api_key, api_secret) if api_key else None

print(f"🔍 Testing Frappe API...")
print(f"   Base URL: {frappe_base_url}")
print(f"   Auth: {'Token-based' if auth else 'None'}\n")

# Test 1: Fetch Whatsapp Customer Group with child tables
print("1️⃣  Fetching Whatsapp Customer Group (with fields parameter)...")
response = requests.get(
    f"{frappe_base_url}/api/resource/Whatsapp Customer Group",
    auth=auth,
    headers={"Content-Type": "application/json"},
    params={
        "limit_page_length": 100,
        "fields": '["name","whatsapp_group_mapping"]'
    }
)
print(f"   Status: {response.status_code}")
if response.status_code == 200:
    data = response.json()
    print(f"   Response keys: {list(data.keys())}")
    groups = data.get("data", [])
    print(f"   Groups found: {len(groups)}")
    if groups:
        for group in groups:
            print(f"\n   📋 Group: {group.get('name')}")
            mappings = group.get("whatsapp_group_mapping", [])
            print(f"      Mappings: {len(mappings)}")
            for m in mappings:
                print(f"        - {m.get('whatsapp_group')} → {m.get('erp_customer')}")
    print(f"\n   Full response:\n{json.dumps(data, indent=2)}\n")
else:
    print(f"   Error: {response.status_code}")
    print(f"   Response: {response.text[:500]}\n")

# Test 2: Fetch without fields parameter
print("2️⃣  Fetching Whatsapp Customer Group (without fields parameter)...")
response = requests.get(
    f"{frappe_base_url}/api/resource/Whatsapp Customer Group",
    auth=auth,
    headers={"Content-Type": "application/json"},
    params={"limit_page_length": 100}
)
print(f"   Status: {response.status_code}")
if response.status_code == 200:
    data = response.json()
    groups = data.get("data", [])
    print(f"   Groups found: {len(groups)}")
    if groups:
        group = groups[0]
        print(f"   First group keys: {list(group.keys())}")
        print(f"   Has 'whatsapp_group_mapping'? {'whatsapp_group_mapping' in group}")
        print(f"\n   Full response:\n{json.dumps(data, indent=2)[:800]}\n")
else:
    print(f"   Error: {response.status_code}\n")

print("✅ Test complete!")
