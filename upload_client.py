#!/usr/bin/env python3
"""Python client for uploading WhatsApp chat exports to the microservice."""

import sys
import os
import requests
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Get API URL from environment, fallback to localhost for local dev
API_URL = os.environ.get("API_URL", "http://localhost:5001/upload-whatsapp-zip")


def upload_zip(file_path, api_url=API_URL):
    """Upload a ZIP file to the API and return the response."""
    file_path = Path(file_path)

    if not file_path.exists():
        print(f"❌ File not found: {file_path}")
        sys.exit(1)

    if not file_path.suffix.lower() == ".zip":
        print(f"❌ File must be a .zip file, got: {file_path.suffix}")
        sys.exit(1)

    print(f"📤 Uploading: {file_path.name}")
    print(f"📍 API: {api_url}")
    print(f"📦 Size: {file_path.stat().st_size / (1024*1024):.1f} MB\n")

    try:
        with open(file_path, "rb") as f:
            files = {"file": f}
            response = requests.post(api_url, files=files, timeout=300)

        result = response.json()

        if response.status_code == 200 and result.get("status") == "success":
            print("✅ SUCCESS!\n")
            print(f"Group:            {result['group_name']}")
            print(f"Tickets extracted: {result['tickets_extracted']}")

            # Frappe sync results
            frappe_status = result.get('frappe', {})
            created = frappe_status.get('created', 0)
            failed = frappe_status.get('failed', 0)
            if created > 0 or failed > 0:
                print(f"\n📤 Frappe Sync:")
                print(f"   Created:  {created}")
                print(f"   Failed:   {failed}")
                if frappe_status.get('details'):
                    print(f"   Errors:   {frappe_status['details']}")

            print(f"\n💰 Cost:")
            print(f"   LLM:     {result['cost']['llm_cost']}")
            print(f"   Whisper: {result['cost']['whisper_cost']}")
            print(f"   Total:   {result['cost']['total_cost']}")
            print(f"\n⏰ Timestamp: {result['timestamp']}")
            return True
        else:
            print(f"❌ Error: {result.get('error', 'Unknown error')}")
            print(f"Response: {json.dumps(result, indent=2)}")
            return False

    except requests.exceptions.ConnectionError:
        print(f"❌ Connection error. Is the API running?")
        print(f"   Start with: python3 api_server.py")
        sys.exit(1)
    except requests.exceptions.Timeout:
        print(f"❌ Request timed out (file too large or processing took too long)")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 upload_client.py <path/to/chat.zip>")
        print("\nExample:")
        print("  python3 upload_client.py ~/Downloads/WhatsApp_Chat_Support.zip")
        sys.exit(1)

    upload_zip(sys.argv[1])
