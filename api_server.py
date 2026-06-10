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
    extract_image_only_tickets, insert_tickets_to_db,
    mark_raw_processed, store_raw_messages, create_message_thread_group, RunCostTracker
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

    Returns:
        {
            "status": "success" | "error",
            "group_name": str,
            "tickets_extracted": int,
            "tickets_inserted": int,
            "tickets_skipped": int,
            "cost": {...},
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
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        temp_file = UPLOAD_DIR / f"{timestamp}_{file.filename}"
        file.save(str(temp_file))

        log.info(f"Received file: {file.filename} ({file.content_length} bytes)")

        # Process the ZIP file
        result = process_uploaded_zip(temp_file)

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


def get_valid_customers(api_key, api_secret, frappe_base_url):
    """Get list of valid customers from Frappe."""
    try:
        auth = HTTPBasicAuth(api_key, api_secret)
        response = requests.get(
            f"{frappe_base_url}/api/resource/Customer",
            auth=auth,
            headers={"Content-Type": "application/json"},
            timeout=10,
            params={"limit_page_length": 100}
        )

        if response.status_code == 200:
            customers = response.json().get("data", [])
            customer_names = [c.get("name") for c in customers if c.get("name")]
            log.info(f"✅ Found {len(customer_names)} valid customers")
            return customer_names
        else:
            log.warning(f"⚠️ Could not fetch customers: {response.status_code}")
            return []
    except Exception as e:
        log.warning(f"⚠️ Error fetching customers: {e}")
        return []


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

    # Get valid customers from Frappe
    frappe_base_url = frappe_url.rsplit("/api/resource/", 1)[0]  # Extract base URL
    valid_customers = get_valid_customers(api_key, api_secret, frappe_base_url)

    if not valid_customers:
        log.warning("⚠️ Could not validate customers - will attempt to create anyway")
        default_customer = "ABC"  # Fallback
    else:
        default_customer = valid_customers[0]  # Use first customer as default

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

    try:
        log.info(f"📤 Sending {len(tickets)} tickets to Frappe: {frappe_url}")

        # Process each ticket
        for idx, ticket in enumerate(tickets):
            try:
                # Map WhatsApp fields to Frappe Pulse Support Ticket doctype
                description = f"[WhatsApp: {group_name}]\n\n{ticket.get('content', '')}"

                # Get customer - use valid one or default
                customer = ticket.get("customer")
                if customer not in valid_customers and valid_customers:
                    # If customer not in list, use default
                    customer = default_customer

                payload = {
                    "title": ticket.get("title", "Untitled Ticket"),
                    "description": description,
                    "status": "Open",
                    "severity": ticket.get("priority", "Medium"),
                    "module": ticket.get("module", "Selling"),
                    "customer": customer,  # Validated customer from ERP
                }

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
        group_name = raw_name or zip_path.stem

        if not timeline:
            raise ValueError("No messages found in chat file")

        log.info(f"Processing: {group_name} ({len(timeline)} messages)")

        # Store messages and create thread group
        media_refs = {e.get("idx"): e.get("filename") for e in timeline if e.get("filename")}
        store_raw_messages(group_name, group_name, timeline, media_refs)
        create_message_thread_group(group_name, group_name, timeline)

        # Extract tickets using parallel sub-agents
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

        # Insert to database
        if all_tickets:
            inserted, skipped = insert_tickets_to_db(group_name, all_tickets)
            mark_raw_processed(group_name)
        else:
            inserted, skipped = 0, 0

        cost_info = {
            "llm_cost": f"${tracker.total_llm_cost():.4f}",
            "whisper_cost": f"${tracker.whisper_cost():.4f}",
            "total_cost": f"${tracker.total_cost():.4f}",
        }

        # Send to Frappe listener for final processing (after DB insertion)
        frappe_result = {"status": "skipped"}
        if inserted > 0:
            log.info(f"📤 Syncing {inserted} tickets to Frappe...")
            frappe_result = send_to_frappe_listener(all_tickets, group_name, cost_info)

        return {
            "group_name": group_name,
            "tickets_extracted": len(all_tickets),
            "tickets_inserted": inserted,
            "tickets_skipped": skipped,
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
