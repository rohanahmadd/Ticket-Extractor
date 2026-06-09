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
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from datetime import datetime
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

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

        return {
            "group_name": group_name,
            "tickets_extracted": len(all_tickets),
            "tickets_inserted": inserted,
            "tickets_skipped": skipped,
            "cost": {
                "llm_cost": f"${tracker.total_llm_cost():.4f}",
                "whisper_cost": f"${tracker.whisper_cost():.4f}",
                "total_cost": f"${tracker.total_cost():.4f}",
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
