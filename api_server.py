#!/usr/bin/env python3
"""
WhatsApp Ticket Microservice
API endpoint for uploading WhatsApp chat exports.
Processes tickets immediately and stores in database.

POST /upload-whatsapp-zip
  - Accept ZIP file upload
  - Process immediately
  - Return status + inserted count

Usage:
  python3 api_server.py

Then POST to:
  curl -X POST -F "file=@chat.zip" http://localhost:5001/upload-whatsapp-zip
"""

import os
import json
import logging
import tempfile
import shutil
import time
import re
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from datetime import datetime
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from requests.auth import HTTPBasicAuth

load_dotenv()

# Import scheduler functions
from scheduler import (
    load_config, parse_chat, group_voice_with_images,
    extract_text_tickets, extract_grouped_context_tickets,
    extract_image_only_tickets, RunCostTracker
)
import anthropic
import zipfile

app = Flask(__name__)
CORS(app)
log = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

BASE = Path(__file__).parent
UPLOAD_DIR = BASE / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# Max file size: 50MB
MAX_FILE_SIZE = 50 * 1024 * 1024
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE


@app.route('/upload-whatsapp-zip', methods=['POST'])
def upload_whatsapp_zip():
    """
    Upload a WhatsApp chat export ZIP file for processing.

    
    Process:
    1. Extract tickets from WhatsApp chat (text, voice, images)
    2. Send ALL extracted tickets to Frappe API
    3. Frappe handles database storage

    Returns:
        {
            "status": "success" | "error",
            "group_name": str,
            "tickets_extracted": int,
            "cost": {...},
            "frappe": {
                "status": "success" | "error",
                "created": int,
                "failed": int
            },
            "timestamp": str,
            "error": str (if status=error)
        }
    """
    try:
        # Check if file was provided
        if 'file' not in request.files:
            return jsonify({
                "status": "error",
                "error": "No file provided. Use 'file' form field."
            }), 400

        file = request.files['file']

        # Validate filename
        if file.filename == '':
            return jsonify({
                "status": "error",
                "error": "Empty filename"
            }), 400

        if not file.filename.lower().endswith('.zip'):
            return jsonify({
                "status": "error",
                "error": "File must be a .zip file"
            }), 400

        # Save uploaded file
        # Use subdirectory instead of timestamp prefix to preserve original filename for group name extraction
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        upload_subdir = UPLOAD_DIR / timestamp
        upload_subdir.mkdir(exist_ok=True)
        temp_file = upload_subdir / file.filename
        file.save(str(temp_file))

        log.info(f"Received file: {file.filename} ({file.content_length} bytes)")

        # Process the ZIP file
        result = process_uploaded_zip(temp_file)
        log.info(f"Processing result: {result['tickets_extracted']} tickets extracted, Frappe status: {result['frappe']['status']}")

        # Clean up
        temp_file.unlink(missing_ok=True)

        return jsonify({
            "status": "success",
            **result
        }), 200

    except Exception as e:
        log.error(f"Upload error: {e}", exc_info=True)
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500


def send_to_frappe_listener(tickets, group_name, cost_info):
    """Send extracted tickets to Frappe using REST API.

    Uses official Frappe REST API endpoints:
    POST /api/resource/Pulse Support Ticket

    Authentication: Token-based (API Key + Secret)

    Args:
        tickets: List of extracted ticket dicts
        group_name: WhatsApp group name
        cost_info: Dict with llm_cost, whisper_cost, total_cost

    Returns:
        dict with status, created_count, failed_count, errors
    """
    frappe_url = os.environ.get("FRAPPE_LISTENER_URL")
    if not frappe_url:
        log.warning("⏭️ FRAPPE_LISTENER_URL not set - skipping Frappe sync")
        return {"status": "skipped", "reason": "FRAPPE_LISTENER_URL not configured"}

    # Get auth credentials
    api_key = os.environ.get("FRAPPE_API_KEY")
    api_secret = os.environ.get("FRAPPE_API_SECRET")
    csrf_token = os.environ.get("FRAPPE_CSRF_TOKEN")

    if not (api_key or csrf_token):
        log.error("❌ No Frappe auth found: set FRAPPE_API_KEY/SECRET or FRAPPE_CSRF_TOKEN")
        return {"status": "error", "error": "No Frappe authentication configured"}

    # Get valid customers and WhatsApp group mappings from Frappe
    frappe_base_url = frappe_url.rsplit("/api/resource/", 1)[0]
    auth_temp = HTTPBasicAuth(api_key, api_secret) if api_key else None

    valid_customers = []
    whatsapp_group_mapping = {}  # Maps whatsapp_group → erp_customer
    default_customer = "ABC"

    try:
        # Fetch all customers
        response = requests.get(
            f"{frappe_base_url}/api/resource/Customer",
            auth=auth_temp,
            headers={"Content-Type": "application/json"},
            timeout=10,
            params={"limit_page_length": 100}
        )
        if response.status_code == 200:
            customers = response.json().get("data", [])
            valid_customers = [c.get("name") for c in customers if c.get("name")]
            default_customer = valid_customers[0] if valid_customers else "ABC"
            log.info(f"✅ Found {len(valid_customers)} customers in Frappe")
        else:
            log.warning(f"⚠️ Could not fetch customers (status {response.status_code}), using ABC")

        # Fetch WhatsApp Customer Group mapping (it's a Single doctype, not a list)
        log.info(f"📡 Fetching WhatsApp Customer Group mapping...")
        response = requests.get(
            f"{frappe_base_url}/api/resource/Whatsapp Customer Group/Whatsapp Customer Group",
            auth=auth_temp,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        log.info(f"📡 Response status: {response.status_code}")
        if response.status_code == 200:
            resp_json = response.json()
            doc = resp_json.get("data", {})
            # Extract the child table
            mappings = doc.get("whatsapp_group_mapping", [])
            log.info(f"📡 Found {len(mappings)} mappings in Whatsapp Customer Group")
            for mapping in mappings:
                whatsapp_group = mapping.get("whatsapp_group", "").strip()
                erp_customer = mapping.get("erp_customer", "").strip()
                if whatsapp_group and erp_customer:
                    whatsapp_group_mapping[whatsapp_group] = erp_customer
                    log.debug(f"    ✓ Mapping: '{whatsapp_group}' → '{erp_customer}'")
            log.info(f"✅ Loaded {len(whatsapp_group_mapping)} WhatsApp group mappings")
            if whatsapp_group_mapping:
                log.info(f"   Available: {list(whatsapp_group_mapping.keys())}")
        else:
            log.warning(f"⚠️ Could not fetch WhatsApp group mappings (status {response.status_code})")
            try:
                log.warning(f"   Response: {response.text[:300]}")
            except:
                pass
    except Exception as e:
        log.warning(f"⚠️ Error fetching mappings: {e}, will use fallback")

    # Prepare headers (standard Frappe REST API headers)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    auth = None

    if api_key and api_secret:
        # Token-based auth (recommended)
        auth = HTTPBasicAuth(api_key, api_secret)
        log.info(f"🔗 Frappe: Token-based auth")
    elif csrf_token:
        # CSRF-based auth (session-based)
        headers["X-Frappe-CSRF-Token"] = csrf_token
        log.info(f"🔗 Frappe: CSRF token auth")

    created = []
    failed = []

    # Module rotation (same as MariaDB)
    modules = ["POS", "Stock", "POS", "Payroll", "Selling", "Stock", "Manufacturing"]
    ticket_counter = 1

    try:
        log.info(f"📤 Sending {len(tickets)} tickets to Frappe: {frappe_url}")

        # Process each ticket using EXACT same schema as MariaDB
        for idx, ticket in enumerate(tickets):
            try:
                # Use EXACT same field mapping as insert_tickets_to_db()
                ticket_counter += 1
                module = modules[(ticket_counter - 1) % len(modules)]

                # Build description from raw_message, voice_transcript, image_context
                parts = []
                if ticket.get("raw_message"):
                    parts.append(f"Message: {ticket['raw_message']}")
                if ticket.get("voice_transcript"):
                    parts.append(f"Voice transcript: {ticket['voice_transcript']}")
                if ticket.get("image_context"):
                    parts.append(f"Image context: {ticket['image_context']}")

                # Fallback to content if no structured fields
                if not parts and ticket.get("content"):
                    parts.append(f"Message: {ticket['content']}")

                actual_description = "\n\n".join(parts) if parts else ticket.get("content", "")

                # Extract full title and create concise version
                full_title = str(ticket.get("normalised", ticket.get("title", "Untitled")))

                # Create concise title by extracting core issue
                # Look for key phrases like "missing", "lacks", "no", etc.
                concise_title = full_title
                issue_match = re.search(
                    r'((?:is |has |with |showing |printed |dated |branch )*)?(.*?(?:missing|lacks|no |without|needs|requires|issue|problem|error|fail).*?)(?:\(|$)',
                    full_title,
                    re.IGNORECASE
                )
                if issue_match:
                    concise_title = issue_match.group(2).strip()
                else:
                    # Fallback: remove common prefixes
                    concise_title = re.sub(
                        r'^.*?(branch|receipt|dated|said|shows?|printed|showing)\s+',
                        '',
                        full_title,
                        flags=re.IGNORECASE
                    ).strip()

                concise_title = concise_title[:140]  # Ensure it fits

                # Build description: full title + two blank lines + actual description
                description = f"{full_title}\n\n{actual_description}"

                # Map WhatsApp group to ERP customer using the mapping
                # First try WhatsApp group mapping, then valid customers, then fallback to ABC
                log.debug(f"Looking up customer for group_name: '{group_name}'")
                log.debug(f"  Available mappings: {list(whatsapp_group_mapping.keys())}")
                log.debug(f"  Available customers: {valid_customers[:5]}... ({len(valid_customers)} total)")

                if group_name in whatsapp_group_mapping:
                    customer_to_use = whatsapp_group_mapping[group_name]
                    log.info(f"  ✓ Using mapped customer: {customer_to_use} for group: {group_name}")
                elif group_name in valid_customers:
                    customer_to_use = group_name
                    log.info(f"  ✓ Using group_name as customer (found in valid customers)")
                else:
                    customer_to_use = default_customer
                    log.info(f"  ⚠️ Using fallback customer: {default_customer} (group '{group_name}' not found)")

                # Map category to valid Frappe values
                valid_categories = ["Bug", "Question", "Request", "Training", "Escalation"]
                extracted_category = str(ticket.get("category", "Request"))
                category_to_use = extracted_category if extracted_category in valid_categories else "Request"

                payload = {
                    "title": concise_title,  # Use concise version
                    "customer": str(customer_to_use)[:140],
                    "module": module,
                    "category": category_to_use,
                    "severity": "High" if ticket.get("needs_review") else "Medium",
                    "status": "Open",
                    "description": description,  # Contains: full_title + blank lines + actual description
                    "ai_analysis": json.dumps(ticket, ensure_ascii=False, default=str),
                }

                # Only add project if group_name exists as a valid project
                if group_name in valid_customers:  # Use same validation as customer
                    payload["project"] = str(group_name)[:140]

                # Log the payload being sent
                log.info(f"📋 Payload {idx+1}/10: {json.dumps(payload, indent=2, ensure_ascii=False)}")

                # Send to Frappe
                response = requests.post(
                    frappe_url,
                    json=payload,
                    headers=headers,
                    auth=auth,
                    timeout=15
                )

                if response.status_code == 201:
                    # Success - document created
                    doc = response.json().get("data", {})
                    ticket_name = doc.get("name", "Unknown")
                    created.append(ticket_name)
                    log.info(f"  ✅ {idx+1}/{len(tickets)}: {ticket_name}")

                elif response.status_code == 200:
                    # Sometimes Frappe returns 200 instead of 201
                    doc = response.json().get("data", {})
                    ticket_name = doc.get("name", "Unknown")
                    created.append(ticket_name)
                    log.info(f"  ✅ {idx+1}/{len(tickets)}: {ticket_name}")

                else:
                    # Error
                    error_data = response.json()
                    error_msg = error_data.get("message", error_data.get("exc", str(response.status_code)))
                    failed.append({
                        "title": ticket.get("title"),
                        "error": error_msg,
                        "code": response.status_code
                    })
                    log.error(f"  ❌ {idx+1}/{len(tickets)}: {error_msg}")

                # Rate limiting: 100ms between requests to avoid overwhelming server
                import time
                if idx < len(tickets) - 1:
                    time.sleep(0.1)

            except requests.Timeout:
                failed.append({
                    "title": ticket.get("title"),
                    "error": "Request timeout (15s)"
                })
                log.error(f"  ⏱️ {idx+1}/{len(tickets)}: Timeout")

            except Exception as e:
                failed.append({
                    "title": ticket.get("title"),
                    "error": str(e)
                })
                log.error(f"  ❌ {idx+1}/{len(tickets)}: {e}")

        # Summary
        log.info(f"📊 Frappe sync complete: {len(created)}/{len(tickets)} successful")

        return {
            "status": "success" if len(created) > 0 else "partial",
            "created_count": len(created),
            "failed_count": len(failed),
            "created_tickets": created,
            "failed_tickets": failed,
        }

    except Exception as e:
        log.error(f"❌ Frappe integration failed: {e}", exc_info=True)
        return {
            "status": "error",
            "error": str(e),
            "created_count": len(created),
            "failed_count": len(failed)
        }


def extract_text_agent(config, text_msgs, tracker):
    """Sub-agent: Extract tickets from text messages."""
    log.info("🔵 Text extraction agent started")
    try:
        client = anthropic.Anthropic(api_key=config["anthropic_api_key"])
        tickets = extract_text_tickets(client, text_msgs, tracker)
        log.info(f"🔵 Text agent completed: {len(tickets)} tickets")
        return {"type": "text", "tickets": tickets, "error": None}
    except Exception as e:
        log.error(f"🔵 Text agent failed: {e}", exc_info=True)
        return {"type": "text", "tickets": [], "error": str(e)}


def extract_voice_image_agent(config, timeline, media_dir, tmp, tracker):
    """Sub-agent: Extract tickets from voice + image groups."""
    log.info("🟢 Voice+Image extraction agent started")
    try:
        client = anthropic.Anthropic(api_key=config["anthropic_api_key"])
        openai_key = config.get("openai_api_key", "")

        voice_groups, _ = group_voice_with_images(timeline)
        all_tickets = []

        for grp in voice_groups:
            tickets = extract_grouped_context_tickets(
                client, grp, media_dir, openai_key, tmp, tracker
            )
            all_tickets.extend(tickets)

        log.info(f"🟢 Voice+Image agent completed: {len(all_tickets)} tickets")
        return {"type": "voice_image", "tickets": all_tickets, "error": None}
    except Exception as e:
        log.error(f"🟢 Voice+Image agent failed: {e}", exc_info=True)
        return {"type": "voice_image", "tickets": [], "error": str(e)}


def extract_image_agent(config, timeline, media_dir, tracker):
    """Sub-agent: Extract tickets from standalone images."""
    log.info("🟡 Image extraction agent started")
    try:
        client = anthropic.Anthropic(api_key=config["anthropic_api_key"])

        _, standalone_images = group_voice_with_images(timeline)
        all_tickets = []

        for img in standalone_images:
            tickets = extract_image_only_tickets(client, img, media_dir, tracker)
            all_tickets.extend(tickets)

        log.info(f"🟡 Image agent completed: {len(all_tickets)} tickets")
        return {"type": "image", "tickets": all_tickets, "error": None}
    except Exception as e:
        log.error(f"🟡 Image agent failed: {e}", exc_info=True)
        return {"type": "image", "tickets": [], "error": str(e)}


def process_uploaded_zip(zip_path):
    """Process uploaded ZIP and return results."""
    config = load_config()
    tracker = RunCostTracker()
    tmp = tempfile.mkdtemp(prefix="wa_upload_")

    try:
        # Extract ZIP
        with zipfile.ZipFile(zip_path) as zf:
            members = [
                m for m in zf.namelist()
                if not m.startswith("__MACOSX")
                and not os.path.basename(m).startswith("._")
            ]
            zf.extractall(tmp, members=members)

        txt_files = list(Path(tmp).rglob("*.txt"))
        if not txt_files:
            raise ValueError("No .txt chat file found in ZIP")

        chat_txt = txt_files[0]
        media_dir = chat_txt.parent

        # Parse chat
        raw_name, timeline = parse_chat(chat_txt)
        # Fallback to ZIP filename if parsing failed, then clean it up
        fallback_name = zip_path.stem
        log.debug(f"ZIP stem (before cleaning): '{fallback_name}'")

        # Remove "WhatsApp Chat - " or "WhatsApp Chat with " if present
        fallback_name = fallback_name.replace("WhatsApp Chat - ", "").replace("WhatsApp Chat with ", "")
        log.debug(f"After removing prefix: '{fallback_name}'")

        # Remove upload timestamp from beginning (YYYYMMDD_HHMM_)
        fallback_name = re.sub(r'^\d{8}_\d{4,6}_', '', fallback_name)
        log.debug(f"After removing start timestamp: '{fallback_name}'")

        # Remove trailing backup timestamps (_YYYYMMDD_HHMM or similar)
        fallback_name = re.sub(r'_\d{8}_\d{4,6}.*', '', fallback_name).strip()
        log.debug(f"After removing trailing timestamps: '{fallback_name}'")

        group_name = raw_name or fallback_name
        log.info(f"📋 Group: '{group_name}' (raw_name='{raw_name or 'None'}', fallback='{fallback_name}')")

        if not timeline:
            raise ValueError("No messages found in chat file")

        log.info(f"Processing: {group_name} ({len(timeline)} messages)")

        # Extract tickets using parallel sub-agents (NO MariaDB - send directly to Frappe)
        log.info("🚀 Starting parallel extraction agents")
        text_msgs = [e for e in timeline if e["type"] == "text"]

        all_tickets = []
        results = {}

        # Run extraction agents in parallel
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(extract_text_agent, config, text_msgs, tracker): "text",
                executor.submit(extract_voice_image_agent, config, timeline, media_dir, tmp, tracker): "voice_image",
                executor.submit(extract_image_agent, config, timeline, media_dir, tracker): "image",
            }

            # Collect results as agents complete
            for future in as_completed(futures):
                agent_type = futures[future]
                result = future.result()
                results[agent_type] = result
                log.info(f"Agent '{agent_type}' returned {len(result['tickets'])} tickets")

        # Merge results in order (text, voice_image, image) to maintain consistency
        all_tickets.extend(results["text"]["tickets"])
        all_tickets.extend(results["voice_image"]["tickets"])
        all_tickets.extend(results["image"]["tickets"])

        log.info(f"✅ Extracted {len(all_tickets)} ticket(s) from {len(results)} agents")

        cost_info = {
            "llm_cost": f"${tracker.total_llm_cost():.4f}",
            "whisper_cost": f"${tracker.whisper_cost():.4f}",
            "total_cost": f"${tracker.total_cost():.4f}",
        }

        # Send directly to Frappe API (bypass MariaDB)
        frappe_result = {"status": "skipped"}
        if all_tickets:
            log.info(f"📤 Sending {len(all_tickets)} tickets directly to Frappe API...")
            frappe_result = send_to_frappe_listener(all_tickets, group_name, cost_info)

        return {
            "group_name": group_name,
            "tickets_extracted": len(all_tickets),
            "cost": cost_info,
            "frappe": {
                "status": frappe_result.get("status"),
                "created": frappe_result.get("created_count", 0),
                "failed": frappe_result.get("failed_count", 0),
                "details": frappe_result.get("failed_tickets", []) if frappe_result.get("failed_tickets") else None
            },
            "timestamp": datetime.now().isoformat(),
        }

    except Exception as e:
        log.error(f"Processing error: {e}", exc_info=True)
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return jsonify({
        "status": "healthy",
        "service": "WhatsApp Ticket Microservice",
        "timestamp": datetime.now().isoformat(),
    }), 200


@app.route('/', methods=['GET'])
def index():
    """Serve the UI or API docs."""
    ui_file = BASE / "upload_ui.html"
    if ui_file.exists():
        return send_file(str(ui_file), mimetype='text/html')
    else:
        # Fallback to API documentation if UI not found
        return jsonify({
            "service": "WhatsApp Ticket Microservice",
            "version": "1.0",
            "ui": "Visit http://localhost:5001/ in your browser",
            "endpoints": {
                "POST /upload-whatsapp-zip": {
                    "description": "Upload WhatsApp chat export ZIP file",
                    "parameters": {
                        "file": "ZIP file (form field)"
                    },
                    "example": "curl -X POST -F 'file=@chat.zip' http://localhost:5001/upload-whatsapp-zip"
                },
                "GET /health": {
                    "description": "Health check endpoint"
                }
            }
        }), 200


if __name__ == "__main__":
    port = int(os.environ.get("API_PORT", 5001))
    log.info(f"Starting WhatsApp Ticket Microservice on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
