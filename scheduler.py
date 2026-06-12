"""
WhatsApp Ticket Agent — Automated Scheduler
============================================
Drop WhatsApp .zip exports into the inbox/ folder.
Every 12 hours (configurable), processes each zip:
  - text messages → Claude ticket extraction
  - voice notes   → OpenAI Whisper transcription → ticket extraction
  - images        → Claude vision analysis → ticket extraction
  - voice + nearby image → combined context → ticket extraction
  - all tickets   → closure detection against later messages
  - Excel report  → output/

Folder structure:
  inbox/       ← drop zip files here
  processed/   ← zips moved here after done
  output/      ← Excel files saved here
  config.json  ← settings
  .env         ← API keys

Run: python3 scheduler.py
"""

import os
from dotenv import load_dotenv
load_dotenv()

import json, shutil, logging, zipfile, re, time, base64, io, sys
import subprocess, tempfile, pymysql
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic
from PIL import Image
from apscheduler.schedulers.blocking import BlockingScheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── DATABASE FUNCTIONS ────────────────────────────────────────────────────────

def get_db_connection():
    """Get a database connection using env vars DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD."""
    required = ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise ValueError(f"Missing database config: {', '.join(missing)}")

    return pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def parse_timestamp(ts_str):
    """Parse timestamp string with multiple formats, fallback to now()."""
    formats = [
        "%d/%m/%Y, %H:%M:%S",
        "%m/%d/%y, %H:%M:%S",
        "%d/%m/%Y, %I:%M:%S %p",
        "%m/%d/%y, %I:%M:%S %p",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(ts_str.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return datetime.now()


def store_raw_messages(group_name, group_id, messages, media_refs):
    """Insert parsed messages into tabWhatsapp Message Thread table.

    Args:
        group_name: Display name for the group
        group_id: Unique ID for the group
        messages: List of parsed message objects from parse_chat()
        media_refs: Dict mapping message indices to media filenames
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        import uuid

        rows_inserted = 0
        for msg in messages:
            idx = msg.get("idx", 0)
            media_filename = media_refs.get(idx)
            media_path = None
            if media_filename:
                # Store relative path only (not full system path) to fit in varchar(140)
                media_path = f"media/{group_id}/{media_filename}"
                if len(media_path) > 140:
                    # If still too long, just store the filename
                    media_path = media_filename

            # Generate unique message ID
            message_id = f"wa-{uuid.uuid4().hex[:12]}"

            insert_query = """
                INSERT INTO `tabWhatsapp Message Thread`
                (name, group_id, group_name, sender_name, sender_phone, message_time,
                 message_type, text_content, media_path, media_filename, processed,
                 received_at, creation, docstatus, idx, owner)
                VALUES
                (%(name)s, %(group_id)s, %(group_name)s, %(sender_name)s, %(sender_phone)s,
                 %(message_time)s, %(message_type)s, %(text_content)s,
                 %(media_path)s, %(media_filename)s, 0,
                 %(received_at)s, %(creation)s, 0, 0, 'Administrator')
            """

            now = datetime.now()
            data = {
                "name": message_id,
                "group_id": group_id,
                "group_name": group_name,
                "sender_name": msg.get("sender", "Unknown"),
                "sender_phone": None,
                "message_time": parse_timestamp(msg.get("timestamp", "")),
                "message_type": msg.get("type", "text"),
                "text_content": msg.get("text", "") or msg.get("caption", ""),
                "media_path": media_path,
                "media_filename": media_filename,
                "received_at": now,
                "creation": now,
            }

            cursor.execute(insert_query, data)
            rows_inserted += 1

        conn.commit()
        log.info(f"  Stored {rows_inserted} message(s) in tabWhatsapp Message Thread")
        conn.close()

    except Exception as e:
        log.error(f"  ❌ Failed to store raw messages: {e}", exc_info=True)
        import traceback
        traceback.print_exc()
        raise


# ── ROSTER MATCHING FUNCTIONS ────────────────────────────────────────────────

def load_roster(filepath="client_roster.csv"):
    """Load branch manager roster from CSV file.

    Returns a list of dicts with keys: branch, manager_name, contact_no, city
    If file doesn't exist, prints warning and returns empty list.
    """
    import csv
    if not os.path.exists(filepath):
        log.warning(f"⚠️  Roster file not found: {filepath}. Continuing without roster matching.")
        return []

    try:
        roster = []
        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                roster.append(row)
        log.info(f"✅ Loaded roster: {len(roster)} branches")
        return roster
    except Exception as e:
        log.error(f"❌ Failed to load roster: {e}")
        return []


def normalize_phone(raw):
    """Normalize phone number to comparable core digits.

    - Remove spaces, dashes, plus, parentheses
    - Remove leading 92 (Pakistan country code)
    - Remove leading 0
    - Return remaining digits

    Examples (all normalize to same): 0370-1950510, +92 370 1950510, +923701950510
    """
    if not raw:
        return ""

    # Remove spaces, dashes, plus, parentheses
    normalized = re.sub(r'[\s\-+()]+', '', str(raw))

    # Remove leading 92 (Pakistan country code)
    if normalized.startswith('92'):
        normalized = normalized[2:]

    # Remove leading 0
    if normalized.startswith('0'):
        normalized = normalized[1:]

    return normalized


def match_sender_to_roster(sender, roster):
    """Match message sender (phone or name) to roster entry.

    Returns:
    - matched roster dict if unambiguous phone or name match
    - dict with "ambiguous": True and entry data if name matches multiple branches
    - None if no match

    Matching logic:
    1. Try PHONE match first (primary, reliable, unique) — normalize both, compare
    2. Try NAME match ONLY if:
       - Sender contains NO digits (pure name, not phone)
       - Manager name appears as whole word (word-boundary regex match)
    3. Handle duplicates:
       - If name matches multiple branches, return ambiguous marker
       - Do NOT guess which branch
    """
    if not sender or not roster:
        return None

    sender_str = str(sender).strip()
    sender_normalized_phone = normalize_phone(sender_str)

    # 1. PHONE MATCH (primary, keep exactly as is)
    if sender_normalized_phone:
        for entry in roster:
            roster_phone = normalize_phone(entry.get("contact_no", ""))
            if sender_normalized_phone == roster_phone:
                return entry

    # 2. NAME MATCH (strict, word-boundary only)
    # Only try name match if sender has NO digits (pure name, not phone-like)
    if not re.search(r'\d', sender_str):
        name_matches = []

        for entry in roster:
            manager_name = entry.get("manager_name", "").strip()

            # Use word-boundary regex: manager name must match as whole word
            # \b ensures "Ali" doesn't match "Khalil" or "Alison"
            # Case-insensitive matching
            pattern = r'\b' + re.escape(manager_name) + r'\b'
            if re.search(pattern, sender_str, re.IGNORECASE):
                name_matches.append(entry)

        # If exactly one match, return it
        if len(name_matches) == 1:
            return name_matches[0]

        # If multiple matches (ambiguous), return special marker
        if len(name_matches) > 1:
            return {
                "ambiguous": True,
                "sender": sender_str,
                "matches": name_matches,  # For logging/review
                "manager_name": name_matches[0].get("manager_name", "")
            }

    return None


def insert_tickets_to_db(group_name, tickets, roster=None):
    """Insert extracted tickets into tabPulse Support Ticket table.

    Returns:
        (inserted_count, skipped_count) where skipped = duplicates
    """
    # Module rotation pattern
    modules = ["POS", "Stock", "POS", "Payroll", "Selling", "Stock", "Manufacturing"]

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Get the highest SUP-XXXX number to continue sequencing
        cursor.execute("""
            SELECT MAX(CAST(SUBSTRING(name, 5) AS UNSIGNED)) as max_num
            FROM `tabPulse Support Ticket`
            WHERE name LIKE 'SUP-%'
        """)
        result = cursor.fetchone()
        next_num = (result.get('max_num') or 0) + 1 if result else 1

        inserted = 0
        skipped = 0
        client_tickets_phone = 0
        client_tickets_name = 0
        ambiguous_matches = []
        team_tickets = 0
        unmatched_senders = []

        for ticket in tickets:
            # ROSTER MATCHING: Match sender to branch manager
            sender = ticket.get("sender", "Unknown")
            matched_entry = match_sender_to_roster(sender, roster) if roster else None

            if matched_entry:
                # Check if this is an ambiguous match (multiple branches share manager name)
                if matched_entry.get("ambiguous"):
                    raised_by = "client"
                    matched_branch = "Ambiguous - needs review"
                    matched_manager = matched_entry.get("manager_name", "")
                    matched_city = ""
                    ambiguous_matches.append(sender)
                else:
                    # Unambiguous match (phone or unique name)
                    raised_by = "client"
                    matched_branch = matched_entry.get("branch", "Unknown")
                    matched_manager = matched_entry.get("manager_name", "")
                    matched_city = matched_entry.get("city", "")

                    # Track match type (phone vs name)
                    # If it has digits after normalization, it was a phone match
                    if normalize_phone(sender):
                        client_tickets_phone += 1
                    else:
                        client_tickets_name += 1
            else:
                raised_by = "team"
                matched_branch = "Unknown"
                matched_manager = ""
                matched_city = ""
                team_tickets += 1
                unmatched_senders.append(sender)

            # Add roster data to ticket for ai_analysis
            ticket["raised_by"] = raised_by
            ticket["matched_branch"] = matched_branch
            ticket["matched_manager"] = matched_manager
            ticket["matched_city"] = matched_city

            # Generate SUP-XXXX name with sequential numbering
            name = f"SUP-{next_num:04d}"
            next_num += 1

            # Rotate through modules
            module = modules[(int(name.split('-')[1]) - 1) % len(modules)]

            # Build description from all available content
            parts = []
            if ticket.get("raw_message"):
                parts.append(f"Message: {ticket['raw_message']}")
            if ticket.get("voice_transcript"):
                parts.append(f"Voice transcript: {ticket['voice_transcript']}")
            if ticket.get("image_context"):
                parts.append(f"Image context: {ticket['image_context']}")
            actual_description = "\n\n".join(parts)

            # Get the full normalised title and create a concise version
            full_title = str(ticket.get("normalised", ""))

            # Extract concise title: find the core issue (after branch/date info)
            # Pattern: look for "is missing", "lacks", "no", "missing" and extract from there
            import re as regex_module
            concise_title = full_title

            # Try to extract just the issue part (remove branch name, dates, etc at the start)
            # Look for common issue markers
            issue_match = regex_module.search(r'((?:is |has |with |showing |printed |dated |branch )*)?(.*?(?:missing|lacks|no |without|needs|requires|issue|problem|error|fail).*?)(?:\(|$)', full_title, regex_module.IGNORECASE)
            if issue_match:
                concise_title = issue_match.group(2).strip()
            else:
                # Fallback: take last sentence or last part after commas
                sentences = full_title.split('.')
                concise_title = sentences[-1].strip() if sentences else full_title

                # Remove common prefixes (branch names, dates, locations)
                concise_title = regex_module.sub(r'^.*?(branch|receipt|dated|said|shows?|shows?|printed|showing)\s+', '', concise_title, flags=regex_module.IGNORECASE).strip()

            concise_title = concise_title[:140]  # Ensure it fits in title field

            # Build final description: full title + two blank lines + actual description
            description = f"{full_title}\n\n{actual_description}"

            insert_query = """
                INSERT IGNORE INTO `tabPulse Support Ticket`
                (name, title, customer, project, module, category, severity, status,
                 description, screenshots, ai_analysis, creation, created_at, docstatus, idx, _user_tags)
                VALUES
                (%(name)s, %(title)s, %(customer)s, %(project)s, %(module)s, %(category)s,
                 %(severity)s, %(status)s, %(description)s, %(screenshots)s,
                 %(ai_analysis)s, NOW(), %(created_at)s, %(docstatus)s, %(idx)s, %(user_tags)s)
            """

            # Use matched_branch as customer/project if client, else use group_name
            customer = matched_branch if raised_by == "client" else group_name
            project = matched_branch if raised_by == "client" else group_name

            data = {
                "name": name,
                "title": concise_title,  # Use concise version for title
                "customer": str(customer)[:140],
                "project": str(project)[:140],
                "module": module,
                "category": str(ticket.get("category", "Uncategorized"))[:140],
                "severity": "High" if ticket.get("needs_review") else "Medium",
                "status": "Open",
                "description": description,  # Contains: full_title + blank lines + actual description
                "screenshots": json.dumps(ticket.get("media_files", [])) if ticket.get("media_files") else None,
                "ai_analysis": json.dumps(ticket, ensure_ascii=False, default=str),
                "created_at": parse_timestamp(ticket.get("timestamp", "")),
                "docstatus": 0,
                "idx": 0,
                "user_tags": raised_by,
            }

            cursor.execute(insert_query, data)
            if cursor.rowcount == 1:
                inserted += 1
            else:
                skipped += 1

        conn.commit()
        log.info(f"  DB insert: {inserted} inserted, {skipped} skipped (duplicates)")

        # SUMMARY REPORT
        log.info("")
        log.info("📊 Tickets by source:")
        log.info(f"   Client tickets (matched by phone):  {client_tickets_phone}")
        log.info(f"   Client tickets (matched by name):   {client_tickets_name}")
        if ambiguous_matches:
            log.info(f"   Ambiguous matches (needs review):   {len(ambiguous_matches)}")
        log.info(f"   Team tickets (unmatched):           {team_tickets}")

        if ambiguous_matches:
            log.info("")
            log.info("⚠️  Ambiguous matches (multiple branches share this manager name):")
            for sender in ambiguous_matches:
                log.info(f"   - {sender}")

        if unmatched_senders:
            log.info("")
            log.info("⚠️  Unmatched senders flagged as team:")
            for sender in unmatched_senders:
                log.info(f"   - {sender}")
        log.info("")

        conn.close()
        return inserted, skipped

    except Exception as e:
        log.error(f"  Failed to insert tickets: {e}", exc_info=True)
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return 0, 0


def create_message_thread_group(group_name, group_id, timeline):
    """Create a group entry in tabWhatsapp Raw Message for the parsed chat.

    Args:
        group_name: Display name for the group
        group_id: Unique ID for the group
        timeline: List of parsed messages
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        import uuid

        if not timeline:
            return

        # Get date range of messages
        timestamps = [parse_timestamp(msg.get("timestamp", "")) for msg in timeline]
        from_datetime = min(timestamps) if timestamps else datetime.now()
        to_datetime = max(timestamps) if timestamps else datetime.now()

        # Generate unique thread ID
        thread_id = f"wa-thread-{uuid.uuid4().hex[:12]}"

        insert_query = """
            INSERT INTO `tabWhatsapp Raw Message`
            (name, group_id, group_name, chat_thread_name, from_datetime, to_datetime,
             creation, docstatus, idx, owner)
            VALUES
            (%(name)s, %(group_id)s, %(group_name)s, %(chat_thread_name)s,
             %(from_datetime)s, %(to_datetime)s,
             %(creation)s, 0, 0, 'Administrator')
        """

        now = datetime.now()
        data = {
            "name": thread_id,
            "group_id": group_id,
            "group_name": group_name,
            "chat_thread_name": group_name,
            "from_datetime": from_datetime,
            "to_datetime": to_datetime,
            "creation": now,
        }

        cursor.execute(insert_query, data)
        conn.commit()
        log.info(f"  Created message thread group: {thread_id}")
        conn.close()

    except Exception as e:
        log.error(f"  Failed to create thread group: {e}", exc_info=True)


def mark_raw_processed(group_id):
    """Mark all messages for a group as processed."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "UPDATE `tabWhatsapp Message Thread` SET processed = 1 WHERE group_id = %s AND processed = 0",
            (group_id,)
        )
        conn.commit()
        log.info(f"  Marked {cursor.rowcount} message(s) as processed")
        conn.close()

    except Exception as e:
        log.error(f"  Failed to mark processed: {e}", exc_info=True)


# ── COST TRACKER ──────────────────────────────────────────────────────────────

class RunCostTracker:
    """Track API usage and compute costs for a single group run."""
    def __init__(self):
        self.prompt_tokens   = 0
        self.response_tokens = 0
        self.whisper_seconds = 0
        self.image_calls     = 0

    def add_llm_call(self, response):
        """Record LLM tokens from a Claude API response."""
        if hasattr(response, "usage") and response.usage:
            self.prompt_tokens   += response.usage.input_tokens
            self.response_tokens += response.usage.output_tokens

    def add_whisper(self, duration_seconds):
        """Record Whisper transcription duration."""
        self.whisper_seconds += duration_seconds

    def add_image(self):
        """Count a vision image analysis call."""
        self.image_calls += 1

    def total_llm_cost(self):
        """Compute LLM cost in USD."""
        input_cost  = (self.prompt_tokens  / 1_000_000) * COST_INPUT_PER_MILLION
        output_cost = (self.response_tokens / 1_000_000) * COST_OUTPUT_PER_MILLION
        return input_cost + output_cost

    def whisper_cost(self):
        """Compute Whisper cost in USD."""
        return (self.whisper_seconds / 60) * WHISPER_COST_PER_MINUTE

    def total_cost(self):
        """Compute total run cost in USD."""
        return self.total_llm_cost() + self.whisper_cost()

    def report(self, group_name, tickets_extracted, tickets_inserted, tickets_skipped):
        """Print a formatted cost and usage report."""
        llm   = self.total_llm_cost()
        whisp = self.whisper_cost()
        total = self.total_cost()
        lines = [
            "",
            "─" * 60,
            f"  Run summary — {group_name}",
            "─" * 60,
            f"  Tickets extracted:              {tickets_extracted:>6}",
            f"  Tickets inserted to DB:         {tickets_inserted:>6}",
            f"  Tickets skipped (duplicate):    {tickets_skipped:>6}",
            "",
            "  Token usage:",
            f"    Prompt tokens:          {self.prompt_tokens:>10,}",
            f"    Response tokens:        {self.response_tokens:>10,}",
            f"    Total tokens:           {self.prompt_tokens + self.response_tokens:>10,}",
            "",
            "  Cost breakdown:",
            f"    LLM (text + images):    ${llm:>10.4f}",
            f"    Whisper transcription:  ${whisp:>10.4f}",
            f"    Total run cost:         ${total:>10.4f}",
            "─" * 60,
            "",
        ]
        for line in lines:
            print(line)


BASE      = Path(__file__).parent
INBOX     = BASE / "inbox"
PROCESSED = BASE / "processed"
OUTPUT    = BASE / "output"
CONFIG_F  = BASE / "config.json"

for d in (INBOX, PROCESSED, OUTPUT):
    d.mkdir(exist_ok=True)

# ── CATEGORIES & COLOURS ──────────────────────────────────────────────────────
CATEGORIES = ["Hardware", "Shift / Access", "Finance", "Tax / Compliance",
              "Pricing", "UI / Branding", "Connectivity", "Uncategorized"]

CAT_COLORS = {
    "Hardware":         ("FFF0E6", "C75000"),
    "Shift / Access":   ("E6F0FF", "1A5CB3"),
    "Finance":          ("E6FAF0", "0A7040"),
    "Tax / Compliance": ("FDE8E8", "B01020"),
    "Pricing":          ("F0E6FF", "6020A0"),
    "UI / Branding":    ("E6F9FF", "006080"),
    "Connectivity":     ("FFFBE6", "806000"),
    "Uncategorized":    ("F2F2F2", "555555"),
}

SOURCE_ICONS = {"text": "💬", "voice": "🎙", "image": "📷", "voice+image": "🎙📷"}

# ── MEDIA CONFIG ──────────────────────────────────────────────────────────────
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VOICE_EXTS = {".opus", ".m4a", ".ogg", ".mp3"}
VIDEO_EXTS = {".mp4", ".3gp", ".mov"}

# ── COST TRACKING ─────────────────────────────────────────────────────────────
COST_INPUT_PER_MILLION  = 3.00    # claude-sonnet-4-6 input tokens
COST_OUTPUT_PER_MILLION = 15.00   # claude-sonnet-4-6 output tokens
WHISPER_COST_PER_MINUTE = 0.006   # OpenAI whisper-1

VOICE_IMAGE_LOOKBACK  = 20   # messages to look back for related images
VOICE_IMAGE_TIME_MINS = 90   # max minutes between image and voice
BURST_GAP_MINS        = 3    # images sent within this many minutes are one burst

# ── TEXT EXTRACTION PROMPT ────────────────────────────────────────────────────
TEXT_PROMPT = """You are a support ticket extractor for a restaurant ERP/POS system in Pakistan (Sweet Creme chain using Lucrum ERP).

Extract only messages that describe a problem, bug, or pending issue needing resolution.

LANGUAGE: Messages may be in English, Roman Urdu, Urdu script, or mixed. Treat typos charitably.
- "priter" = printer, "shif" = shift, "depozit" = deposit
- "kaam nahi kar rha" = not working, "masla hai" = problem
- "nazar nahi aa rha" = not appearing, "galat" = wrong
- "band ho gaya" = stopped/shut down, "error aa rha hai" = getting an error

CATEGORIES:
- Hardware: printer, POS terminal, receipt printer, device
- Shift / Access: shift opening/closing, login, POS ID deactivated, shift blocked by offline invoice
- Finance: deposit not reflecting, expense issues, closing balance Rs 0, large cash differences unreconciled
- Tax / Compliance: PRA disconnected, QR code missing/not scanning, tax logo missing (PRA/SRB/FBR/KPRA/AJK), NTN/SNTN not printing, AJK token expired, wrong tax rate
- Pricing: wrong item price, wrong bill amount, KOT cancel due to wrong amount
- UI / Branding: logo not showing, branch address missing from receipt, branding image missing
- Connectivity: server down, NEXNODE offline, internet slow, integration disconnected, offline invoice not syncing
- Uncategorized: does not clearly fit any above

IGNORE: greetings, ok/noted/thanks/haan/ji, status follow-up questions, resolution messages (fixed/resolved/theek ho gaya/ho gaya/done), general chat.

SPECIAL RULE: If one message describes TWO separate problems, create TWO ticket objects.

OUTPUT: Respond ONLY with a valid JSON array. No markdown, no backticks.
Each ticket:
{"sender":"name","timestamp":"ts","raw_message":"original text","normalised":"clean English one sentence","category":"exact name","confidence":90,"needs_review":false,"source_type":"text","media_files":[],"voice_transcript":"","image_context":""}
If no tickets: []"""

# ── IMAGE EXTRACTION PROMPT ───────────────────────────────────────────────────
IMAGE_PROMPT = """You are a support ticket extractor for Sweet Creme restaurant chain using Lucrum ERP in Pakistan.

You are given a screenshot that a staff member sent in WhatsApp along with their caption (what they typed alongside the image).

CRITICAL RULE — ONE TICKET PER IMAGE+CAPTION:
The caption and the image together describe ONE single issue. The person sent the image as evidence of the problem they described in their caption. You must combine them into a single ticket. Do NOT create separate tickets for the caption and the image.
- "normalised" = clean English summary combining what they said (caption) AND what is visible in the image
- "raw_message" = their caption text + "[Screenshot shows: brief description of what is visible]"
Only create two tickets if the image clearly shows two completely unrelated problems with no connection to each other.

WHAT TO LOOK FOR in the image:
- Missing QR codes on receipts (QR code should always be present)
- Missing tax authority logos (PRA, SRB, FBR, KPRA, AJK logos absent from bill)
- Wrong or missing NTN / SNTN numbers on bills
- Missing branch address on receipts
- Server errors — Cloudflare 521, "Web server is down", host unreachable
- Shift closing blocked — "Shift Closing is Blocked", offline invoice, Submit button disabled
- Large cash differences shown in red during shift closing
- NEXNODE status showing red or offline on POS screen
- Wrong business name in PRA verification app
- Deposit showing Rs 0 in ledger or POS session report
- KOT or invoice cannot be cancelled, wrong amount written on bill
- Test items ("TESTER") being processed on live production system
- Any other visible error message or compliance issue

IGNORE: Eid greetings, staff schedules, normal working screens, correct receipts where QR code and all logos are present and no errors are visible.

OUTPUT: ONLY a valid JSON array. No markdown, no backticks.
{"sender":"name","timestamp":"ts","raw_message":"caption text [Screenshot shows: what is visible]","normalised":"combined clean English description","category":"exact name","confidence":90,"needs_review":false,"source_type":"image","media_files":["filename.jpg"],"voice_transcript":"","image_context":"description of what the image showed"}
If no problem visible: []"""


# ── CONFIG LOADER ─────────────────────────────────────────────────────────────
def load_config():
    if not CONFIG_F.exists():
        log.error(f"config.json not found at {CONFIG_F}")
        raise SystemExit(1)
    with open(CONFIG_F) as f:
        cfg = json.load(f)

    env_file = BASE / ".env"
    env_lines = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env_lines[k.strip()] = v.strip()

    def _resolve(cfg_key, env_key):
        if not cfg.get(cfg_key):
            cfg[cfg_key] = env_lines.get(env_key) or os.environ.get(env_key, "")

    _resolve("anthropic_api_key", "ANTHROPIC_API_KEY")
    _resolve("openai_api_key",    "OPENAI_API_KEY")

    if not cfg.get("anthropic_api_key"):
        log.error("anthropic_api_key is not set in config.json, .env, or environment.")
        raise SystemExit(1)
    return cfg


# ── TIMESTAMP PARSER ──────────────────────────────────────────────────────────
_TS_FORMATS = [
    "%d/%m/%Y, %I:%M:%S %p",
    "%d/%m/%Y, %I:%M %p",
    "%m/%d/%Y, %I:%M:%S %p",
    "%m/%d/%Y, %I:%M %p",
    "%d/%m/%Y, %H:%M:%S",
    "%d/%m/%Y, %H:%M",
]

def _parse_ts(ts_str):
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(ts_str.strip(), fmt)
        except ValueError:
            pass
    return None


# ── CHAT PARSER → UNIFIED TIMELINE ───────────────────────────────────────────
def parse_chat(filepath):
    """
    Parse a WhatsApp _chat.txt into a unified timeline.
    Returns: (group_name, timeline)
    Each entry: idx, timestamp, sender, type, text, filename, caption
    """
    content = None
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            content = filepath.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            pass
    if content is None:
        log.error(f"Could not decode {filepath.name}")
        return "", []

    stem = re.sub(r"WhatsApp Chat with |WhatsApp Chat - ", "", filepath.stem).strip()
    group_name = "" if stem in ("_chat", "") else stem

    MSG_RE = re.compile(
        r"[‎‏]?\[(\d{1,2}[\/\.\-]\d{1,2}[\/\.\-]\d{2,4},?\s+"
        r"\d{1,2}:\d{2}(?::\d{2})?[ \s]?[APap][Mm]?)\]"
        r"[ \s]*([^:]+?):\s*(.*)"
    )
    ATTACH_RE = re.compile(r"[‎]?<attached:\s*([\w\-\.\s&()]+?)>")

    SKIP_PHRASES = [
        "messages and calls are end-to-end encrypted",
        "created this group", "added you", "left", "joined using",
        "changed the subject", "changed this group", "image omitted",
        "video omitted", "audio omitted", "sticker omitted", "gif omitted",
        "file omitted",
    ]

    timeline = []
    idx = 0
    pending = None

    def _flush(msg):
        nonlocal idx
        if not msg:
            return
        raw_text = msg["text"].lstrip("‎").strip()
        lower = raw_text.lower()
        if any(p in lower for p in SKIP_PHRASES):
            return
        if msg["sender"] in ("", "You"):
            return

        attach = ATTACH_RE.search(raw_text)
        if attach:
            fname   = attach.group(1).strip()
            ext     = Path(fname).suffix.lower()
            caption = ATTACH_RE.sub("", raw_text).strip().lstrip("‎").strip()
            if ext in IMAGE_EXTS:
                mtype = "image"
            elif ext in VOICE_EXTS:
                mtype = "voice"
            elif ext in VIDEO_EXTS:
                mtype = "video"
            else:
                mtype = "document"
            timeline.append({
                "idx": idx, "timestamp": msg["timestamp"],
                "sender": msg["sender"], "type": mtype,
                "text": caption, "filename": fname, "caption": caption,
            })
        else:
            if not raw_text:
                return
            timeline.append({
                "idx": idx, "timestamp": msg["timestamp"],
                "sender": msg["sender"], "type": "text",
                "text": raw_text, "filename": None, "caption": "",
            })
        idx += 1

    for line in content.splitlines():
        line    = line.rstrip("\r")
        stripped = line.lstrip("‎‏‪‫‬")
        m = MSG_RE.match(stripped)
        if m:
            _flush(pending)
            pending = {
                "timestamp": m.group(1).strip(),
                "sender":    m.group(2).strip().lstrip(" ~").strip(),
                "text":      m.group(3).strip(),
            }
        elif pending and stripped and not stripped.startswith("‎["):
            pending["text"] += " " + stripped.lstrip("‎").strip()

    _flush(pending)
    return group_name, timeline


# ── VOICE + IMAGE GROUPING ────────────────────────────────────────────────────
def group_voice_with_images(timeline):
    """
    For each voice message, pair it with at most one burst of images from the
    same sender that immediately precede it.

    Algorithm:
      1. Scan backwards from the voice note to find the NEAREST previous image
         from the same sender within VOICE_IMAGE_TIME_MINS minutes.
      2. From that nearest image, expand backwards to collect any back-to-back
         images (same sender, gap ≤ BURST_GAP_MINS between consecutive images).
      3. Stop expanding as soon as the gap between consecutive images exceeds
         BURST_GAP_MINS — earlier unrelated images are left as standalone.

    A voice note with no qualifying previous image is kept as source_type="voice".
    Images not claimed by any voice note remain standalone.
    """
    claimed = set()
    voice_groups = []

    for i, entry in enumerate(timeline):
        if entry["type"] != "voice":
            continue

        voice_dt = _parse_ts(entry["timestamp"])
        start    = max(0, i - VOICE_IMAGE_LOOKBACK)

        # ── Step 1: find the NEAREST previous image from the same sender ──
        # Timestamps must be parseable; if either can't be parsed, skip.
        nearest_j  = None
        nearest_dt = None
        for j in range(i - 1, start - 1, -1):
            cand = timeline[j]
            if cand["type"] != "image" or cand["idx"] in claimed:
                continue
            if cand["sender"] != entry["sender"]:
                continue
            cand_dt = _parse_ts(cand["timestamp"])
            if not voice_dt or not cand_dt:
                continue   # can't verify time window — skip
            diff = (voice_dt - cand_dt).total_seconds() / 60
            if not (0 <= diff <= VOICE_IMAGE_TIME_MINS):
                continue
            nearest_j  = j
            nearest_dt = cand_dt
            break   # first match scanning backwards = nearest

        if nearest_j is None:
            voice_groups.append({"voice": entry, "images": []})
            continue

        # ── Step 2: claim nearest image and expand burst backwards ────────
        burst   = [timeline[nearest_j]]
        claimed.add(timeline[nearest_j]["idx"])
        prev_dt = nearest_dt

        for j in range(nearest_j - 1, start - 1, -1):
            cand = timeline[j]
            if cand["type"] != "image" or cand["idx"] in claimed:
                continue
            if cand["sender"] != entry["sender"]:
                continue
            cand_dt = _parse_ts(cand["timestamp"])
            if not cand_dt:
                continue   # can't verify burst gap — skip
            # Stop if the gap between consecutive burst images is too large
            gap = (prev_dt - cand_dt).total_seconds() / 60
            if gap > BURST_GAP_MINS:
                break
            burst.insert(0, cand)   # prepend to keep chronological order
            claimed.add(cand["idx"])
            prev_dt = cand_dt

        voice_groups.append({"voice": entry, "images": burst})

    standalone_images = [
        e for e in timeline
        if e["type"] == "image" and e["idx"] not in claimed
    ]
    return voice_groups, standalone_images


# ── IMAGE ENCODING ────────────────────────────────────────────────────────────
def _encode_image_b64(path, max_px=1200):
    with Image.open(path) as img:
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_px:
            s = max_px / max(w, h)
            img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"


# ── VOICE TRANSCRIPTION ───────────────────────────────────────────────────────

# Formats accepted directly by the OpenAI Whisper API
_OPENAI_AUDIO_FORMATS = {".flac", ".m4a", ".mp3", ".mp4", ".mpeg",
                          ".mpga", ".oga", ".ogg", ".wav", ".webm"}


def _convert_to_wav(src_path, tmp_dir):
    """Convert audio file to 16 kHz mono WAV. Returns Path to WAV or raises."""
    work = Path(tmp_dir or tempfile.mkdtemp()) / "openai_convert"
    work.mkdir(exist_ok=True)
    wav = work / "audio.wav"
    subprocess.run(
        ["ffmpeg", "-i", str(src_path), "-ar", "16000", "-ac", "1",
         str(wav), "-y", "-loglevel", "error"],
        check=True, capture_output=True,
    )
    return wav


def transcribe_voice(audio_path, openai_api_key=None, tmp_dir=None):
    audio_path = Path(audio_path)
    if not audio_path.exists():
        log.warning(f"  Voice file missing: {audio_path.name}")
        return None

    # ── OpenAI Whisper API ────────────────────────────────────────────────
    if openai_api_key:
        openai_input = audio_path

        # Convert to WAV if the format is not accepted by OpenAI
        if audio_path.suffix.lower() not in _OPENAI_AUDIO_FORMATS:
            try:
                log.info(f"  Converting {audio_path.suffix.upper()} to WAV for OpenAI: {audio_path.name}")
                openai_input = _convert_to_wav(audio_path, tmp_dir)
            except Exception as e:
                log.warning(f"  Conversion failed for {audio_path.name}: {e} — will try local fallback")
                openai_input = None

        if openai_input and openai_input.exists():
            try:
                from openai import OpenAI
                oa = OpenAI(api_key=openai_api_key)

                # Prefer translation endpoint: always returns English text
                try:
                    with open(openai_input, "rb") as f:
                        result = oa.audio.translations.create(
                            model="whisper-1", file=f, response_format="text",
                        )
                    transcript = (result if isinstance(result, str) else result.text).strip()
                    log.info(f"  [OpenAI Whisper] {audio_path.name}: {transcript[:80]}...")
                    return transcript
                except Exception:
                    pass  # fall through to transcription

                # Fallback: transcription in original language
                with open(openai_input, "rb") as f:
                    result = oa.audio.transcriptions.create(
                        model="whisper-1", file=f, response_format="text",
                    )
                transcript = (result if isinstance(result, str) else result.text).strip()
                log.info(f"  [OpenAI Whisper] {audio_path.name}: {transcript[:80]}...")
                return transcript

            except Exception as e:
                log.warning(f"  OpenAI Whisper failed for {audio_path.name}: {e} — trying local fallback")

    # ── Local whisper CLI fallback ────────────────────────────────────────
    if not (shutil.which("whisper") and shutil.which("ffmpeg")):
        log.warning(f"  Skipping {audio_path.name}: no OpenAI key and no local whisper/ffmpeg")
        return None

    try:
        work = Path(tmp_dir or tempfile.mkdtemp()) / "whisper_tmp"
        work.mkdir(exist_ok=True)
        wav = work / "audio.wav"
        subprocess.run(
            ["ffmpeg", "-i", str(audio_path), "-ar", "16000", "-ac", "1",
             str(wav), "-y", "-loglevel", "error"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["whisper", str(wav), "--model", "small", "--output_format", "txt",
             "--output_dir", str(work), "--fp16", "False"],
            capture_output=True, text=True, timeout=120,
        )
        txt_file = work / "audio.txt"
        if txt_file.exists():
            transcript = txt_file.read_text().strip()
            log.info(f"  [local whisper fallback] {audio_path.name}: {transcript[:80]}...")
            return transcript
    except Exception as e:
        log.warning(f"  Local whisper failed for {audio_path.name}: {e}")
    return None


# ── TICKET NORMALISATION ──────────────────────────────────────────────────────
def normalise_ticket(t):
    cat_map = {c.lower(): c for c in CATEGORIES}
    cat = cat_map.get(str(t.get("category", "")).strip().lower(), "Uncategorized")

    try:
        conf = int(float(str(t.get("confidence", 70))))
    except Exception:
        conf = 70

    nr = t.get("needs_review", False)
    if isinstance(nr, str):
        nr = nr.lower() in ("true", "yes", "1")
    if conf < 65:          # force review for low-confidence tickets
        nr = True

    mf = t.get("media_files", [])
    return {
        "sender":            str(t.get("sender", "Unknown")),
        "timestamp":         str(t.get("timestamp", "")),
        "raw_message":       str(t.get("raw_message", "")),
        "normalised":        str(t.get("normalised", "")),
        "category":          cat,
        "confidence":        conf,
        "needs_review":      bool(nr),
        "source_type":       str(t.get("source_type", t.get("source", "text"))).strip() or "text",
        "media_files":       mf if isinstance(mf, list) else [],
        "voice_transcript":  str(t.get("voice_transcript", "")),
        "image_context":     str(t.get("image_context", "")),
    }


# ── CLAUDE API ────────────────────────────────────────────────────────────────
def call_claude(client, messages_content, system_prompt=None):
    if system_prompt is None:
        system_prompt = TEXT_PROMPT
    for attempt in range(3):
        try:
            r = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4000,
                system=system_prompt,
                messages=[{"role": "user", "content": messages_content}],
            )
            # Store response for cost tracking
            client._last_response = r
            raw = r.content[0].text.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE).strip()
            raw = re.sub(r"\s*```\s*$", "", raw, flags=re.IGNORECASE).strip()
            if not raw.startswith("["):
                found = re.search(r"\[.*\]", raw, re.DOTALL)
                raw = found.group(0) if found else "[]"
            result = json.loads(raw)
            if isinstance(result, dict):
                result = [result]
            return result
        except Exception as e:
            if attempt == 2:
                log.warning(f"Claude call failed: {e}")
                return []
            time.sleep(2 ** attempt)
    return []


# ── TICKET EXTRACTION — TEXT ──────────────────────────────────────────────────
def extract_text_tickets(client, text_msgs, tracker=None):
    all_tickets = []
    chunks = [text_msgs[i:i + 60] for i in range(0, len(text_msgs), 60)]
    log.info(f"  Text: {len(chunks)} chunk(s)")
    for i, chunk in enumerate(chunks, 1):
        chat = "\n".join(
            f"[{m['timestamp']}] {m['sender']}: {m['text']}" for m in chunk
        )
        tickets = call_claude(client, f"Extract support tickets:\n\n{chat}", TEXT_PROMPT)
        for t in tickets:
            t.setdefault("source_type", "text")
        all_tickets.extend(tickets)

        # Track usage and print per-chunk progress
        msg = f"    Chunk {i}/{len(chunks)} ({len(chunk)} messages) → {len(tickets)} ticket(s)"
        if tracker and hasattr(client, '_last_response'):
            r = client._last_response
            if hasattr(r, 'usage') and r.usage:
                tracker.add_llm_call(r)
                in_cost = (r.usage.input_tokens / 1_000_000) * COST_INPUT_PER_MILLION
                out_cost = (r.usage.output_tokens / 1_000_000) * COST_OUTPUT_PER_MILLION
                msg += f" | tokens: {r.usage.input_tokens}↑ {r.usage.output_tokens}↓ | cost: ${in_cost + out_cost:.4f}"
        log.info(msg)
    return all_tickets


# ── TICKET EXTRACTION — VOICE + NEARBY IMAGES ────────────────────────────────
def extract_grouped_context_tickets(client, group, media_dir, openai_key=None, tmp_dir=None, tracker=None):
    voice_entry   = group["voice"]
    image_entries = group["images"]

    transcript = transcribe_voice(
        media_dir / voice_entry["filename"], openai_key, tmp_dir
    )
    if tracker and transcript:
        # Estimate Whisper cost: .opus files ~16kbps, so bytes/2000 ≈ seconds
        audio_file = media_dir / voice_entry["filename"]
        if audio_file.exists():
            try:
                duration_sec = audio_file.stat().st_size / 2000
                tracker.add_whisper(duration_sec)
            except Exception:
                tracker.add_whisper(30)  # fallback estimate
    if not transcript and not image_entries:
        return []

    content         = []
    image_filenames = []

    for img in image_entries:
        img_path = media_dir / img["filename"]
        if not img_path.exists():
            continue
        try:
            b64, mime = _encode_image_b64(img_path)
            content.append({
                "type":   "image",
                "source": {"type": "base64", "media_type": mime, "data": b64},
            })
            image_filenames.append(img["filename"])
            if tracker:
                tracker.add_image()
        except Exception as e:
            log.warning(f"  Failed to load image {img['filename']}: {e}")

    source = "voice+image" if image_filenames else "voice"
    parts  = [f"Voice message from {voice_entry['sender']} at {voice_entry['timestamp']}."]
    if transcript:
        parts.append(f"Voice transcript: {transcript}")
    if image_filenames:
        parts.append(
            f"The following screenshot(s) were sent by the same person just before "
            f"this voice note and likely show the issue: {', '.join(image_filenames)}"
        )
    parts.append(
        f"Extract support tickets. Set source_type to '{source}'. "
        "If screenshots are present, describe visible content in image_context."
    )
    content.append({"type": "text", "text": "\n".join(parts)})

    # Use IMAGE_PROMPT when images are present, TEXT_PROMPT for voice-only
    prompt  = IMAGE_PROMPT if image_filenames else TEXT_PROMPT
    tickets = call_claude(client, content, prompt)

    if tracker and hasattr(client, '_last_response'):
        tracker.add_llm_call(client._last_response)

    media_files = [voice_entry["filename"]] + image_filenames
    for t in tickets:
        t["source_type"]     = source
        t.setdefault("sender",    voice_entry["sender"])
        t.setdefault("timestamp", voice_entry["timestamp"])
        t["voice_transcript"] = transcript or ""
        t["media_files"]      = media_files
        if not t.get("raw_message"):
            t["raw_message"] = f"[Voice] {transcript or '(no transcript)'}"
    return tickets


# ── TICKET EXTRACTION — STANDALONE IMAGE ─────────────────────────────────────
def extract_image_only_tickets(client, img_entry, media_dir, tracker=None):
    img_path = media_dir / img_entry["filename"]
    if not img_path.exists():
        return []
    try:
        b64, mime = _encode_image_b64(img_path)
        if tracker:
            tracker.add_image()
        caption   = img_entry.get("caption", "")
        content   = [
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
            {"type": "text", "text": (
                f"Screenshot sent by: {img_entry['sender']} at {img_entry['timestamp']}."
                + (f"\nCaption: {caption}" if caption else "")
            )},
        ]
        tickets = call_claude(client, content, IMAGE_PROMPT)

        if tracker and hasattr(client, '_last_response'):
            tracker.add_llm_call(client._last_response)

        for t in tickets:
            t["source_type"] = "image"
            t.setdefault("sender",    img_entry["sender"])
            t.setdefault("timestamp", img_entry["timestamp"])
            t["media_files"] = [img_entry["filename"]]
            if not t.get("raw_message"):
                t["raw_message"] = f"[Image: {img_entry['filename']}]"
        return tickets
    except Exception as e:
        log.warning(f"  Image extraction failed {img_entry['filename']}: {e}")
        return []


# ── PARALLEL EXTRACTION SUB-AGENTS ────────────────────────────────────────────

def _extract_text_agent(config, text_msgs, tracker):
    """Sub-agent: Extract tickets from text messages (parallel)."""
    log.info("  🔵 Text agent started")
    try:
        client = anthropic.Anthropic(api_key=config["anthropic_api_key"])
        tickets = extract_text_tickets(client, text_msgs, tracker)
        log.info(f"  🔵 Text agent: {len(tickets)} tickets")
        return tickets
    except Exception as e:
        log.error(f"  🔵 Text agent failed: {e}")
        return []


def _extract_voice_image_agent(config, timeline, media_dir, tmp, tracker):
    """Sub-agent: Extract tickets from voice + image groups (parallel)."""
    log.info("  🟢 Voice+Image agent started")
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

        log.info(f"  🟢 Voice+Image agent: {len(all_tickets)} tickets")
        return all_tickets
    except Exception as e:
        log.error(f"  🟢 Voice+Image agent failed: {e}")
        return []


def _extract_image_agent(config, timeline, media_dir, tracker):
    """Sub-agent: Extract tickets from standalone images (parallel)."""
    log.info("  🟡 Image agent started")
    try:
        client = anthropic.Anthropic(api_key=config["anthropic_api_key"])

        _, standalone_images = group_voice_with_images(timeline)
        all_tickets = []

        for img in standalone_images:
            tickets = extract_image_only_tickets(client, img, media_dir, tracker)
            all_tickets.extend(tickets)

        log.info(f"  🟡 Image agent: {len(all_tickets)} tickets")
        return all_tickets
    except Exception as e:
        log.error(f"  🟡 Image agent failed: {e}")
        return []



# ── PROCESS ONE ZIP ───────────────────────────────────────────────────────────
def process_zip(zip_path, config):
    log.info(f"Processing: {zip_path.name}")
    tracker = RunCostTracker()
    tmp = tempfile.mkdtemp(prefix="wa_agent_")
    try:
        # Filter macOS system files before extracting
        with zipfile.ZipFile(zip_path) as zf:
            members = [
                m for m in zf.namelist()
                if not m.startswith("__MACOSX")
                and not os.path.basename(m).startswith("._")
            ]
            zf.extractall(tmp, members=members)

        txt_files = list(Path(tmp).rglob("*.txt"))
        if not txt_files:
            log.warning(f"No .txt in {zip_path.name}")
            return

        chat_txt  = txt_files[0]
        media_dir = chat_txt.parent

        raw_name, timeline = parse_chat(chat_txt)
        group_name = raw_name or re.sub(
            r"^WhatsApp\s+Chat[\s\-–]+", "", zip_path.stem
        ).strip() or zip_path.stem

        text_msgs  = [e for e in timeline if e["type"] == "text"]
        image_msgs = [e for e in timeline if e["type"] == "image"]
        voice_msgs = [e for e in timeline if e["type"] == "voice"]
        log.info(
            f"  {group_name} | "
            f"text:{len(text_msgs)} image:{len(image_msgs)} voice:{len(voice_msgs)}"
        )

        # Store messages and create thread group in database
        media_refs = {e.get("idx"): e.get("filename") for e in timeline if e.get("filename")}
        store_raw_messages(group_name, group_name, timeline, media_refs)
        create_message_thread_group(group_name, group_name, timeline)

        # Run extraction agents in parallel
        log.info("  🚀 Starting parallel extraction agents")
        all_tickets = []

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(_extract_text_agent, config, text_msgs, tracker): "text",
                executor.submit(_extract_voice_image_agent, config, timeline, media_dir, tmp, tracker): "voice_image",
                executor.submit(_extract_image_agent, config, timeline, media_dir, tracker): "image",
            }

            # Collect results in order as they complete
            results = {}
            for future in as_completed(futures):
                agent_type = futures[future]
                tickets = future.result()
                results[agent_type] = tickets

        # Merge results in consistent order (text, voice_image, image)
        all_tickets.extend(results.get("text", []))
        all_tickets.extend(results.get("voice_image", []))
        all_tickets.extend(results.get("image", []))

        log.info(f"  ✅ Total tickets: {len(all_tickets)}")

        if not all_tickets:
            log.info("  No tickets found — skipping database insert")
            return

        # 5. Load roster and insert tickets to database
        roster = load_roster()
        inserted, skipped = insert_tickets_to_db(group_name, all_tickets, roster=roster)
        mark_raw_processed(group_name)

        # Print cost report
        tracker.report(
            group_name=group_name,
            tickets_extracted=len(all_tickets),
            tickets_inserted=inserted,
            tickets_skipped=skipped,
        )

        ts   = datetime.now().strftime("%Y%m%d_%H%M")
        dest = PROCESSED / f"{zip_path.stem}_{ts}.zip"
        shutil.move(str(zip_path), str(dest))
        log.info(f"  Moved to processed/")

    except Exception as e:
        log.error(f"Failed: {zip_path.name}: {e}", exc_info=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── WEBHOOK-BASED PROCESSING ──────────────────────────────────────────────────

def process_from_webhook(group_id, config):
    """
    Process unprocessed messages for one group from messages.db.

    Mirrors process_zip() after the parse step:
      text msgs → Claude extraction
      voice+image groups → Whisper + Claude
      standalone images → Claude Vision
      → Excel → delivery

    Calls mark_as_processed(group_id) only after successful delivery.
    """
    try:
        from db_reader import get_unprocessed_messages, mark_as_processed
    except ImportError:
        log.warning("db_reader.py not found — skipping webhook processing")
        return

    group_name, timeline, media_dir = get_unprocessed_messages(group_id)

    if not timeline:
        return  # nothing to process — skip silently

    text_msgs  = [e for e in timeline if e["type"] == "text"]
    image_msgs = [e for e in timeline if e["type"] == "image"]
    voice_msgs = [e for e in timeline if e["type"] == "voice"]
    log.info(
        f"  [webhook] {group_name} | "
        f"text:{len(text_msgs)} image:{len(image_msgs)} voice:{len(voice_msgs)}"
    )

    tmp = tempfile.mkdtemp(prefix="wa_webhook_")
    tracker = RunCostTracker()

    try:
        # Run extraction agents in parallel
        log.info("  🚀 Starting parallel extraction agents")
        all_tickets = []

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(_extract_text_agent, config, text_msgs, tracker): "text",
                executor.submit(_extract_voice_image_agent, config, timeline, media_dir, tmp, tracker): "voice_image",
                executor.submit(_extract_image_agent, config, timeline, media_dir, tracker): "image",
            }

            # Collect results in order as they complete
            results = {}
            for future in as_completed(futures):
                agent_type = futures[future]
                tickets = future.result()
                results[agent_type] = tickets

        # Merge results in consistent order (text, voice_image, image)
        all_tickets = []
        all_tickets.extend(results.get("text", []))
        all_tickets.extend(results.get("voice_image", []))
        all_tickets.extend(results.get("image", []))

        log.info(f"  ✅ Total tickets: {len(all_tickets)}")

        if not all_tickets:
            log.info("  No tickets found — skipping database insert")
            mark_as_processed(group_id)
            return

        # 4. Load roster and insert tickets to database
        roster = load_roster()
        inserted, skipped = insert_tickets_to_db(group_name, all_tickets, roster=roster)

        # Print cost report
        tracker.report(
            group_name=group_name,
            tickets_extracted=len(all_tickets),
            tickets_inserted=inserted,
            tickets_skipped=skipped,
        )

        # Mark processed only after successful insertion
        mark_as_processed(group_id)
        log.info(f"  [{group_name}] {inserted} inserted, {skipped} skipped (duplicates)")

    except Exception as e:
        log.error(f"Webhook processing failed for group {group_id}: {e}", exc_info=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── SCHEDULER LOOP ────────────────────────────────────────────────────────────

def run_job():
    """Single scheduler run: process ZIP inbox then webhook database messages."""
    log.info("=== Ticket agent run started ===")
    config = load_config()

    # 1. ZIP inbox (original flow — unchanged)
    zips = sorted(INBOX.glob("*.zip"))
    if zips:
        log.info(f"Found {len(zips)} zip(s)")
        for z in zips:
            process_zip(z, config)
    else:
        log.info("Inbox empty — no ZIPs to process")

    # 2. Webhook database messages
    try:
        from db_reader import get_groups_with_unprocessed
        groups = get_groups_with_unprocessed()
        if groups:
            log.info(f"Found {len(groups)} group(s) with unprocessed webhook messages")
            for gid in groups:
                process_from_webhook(gid, config)
        else:
            log.info("No unprocessed webhook messages")
    except ImportError:
        pass  # db_reader optional — skip silently if not present

    log.info("=== Run complete ===")


def start_scheduler():
    """
    Start the scheduler in background mode for use with run.py.
    Runs run_job() immediately on start, then repeats on the configured interval.
    Blocks the calling thread until interrupted.
    """
    from apscheduler.schedulers.background import BackgroundScheduler
    import time

    config = load_config()
    hours  = config.get("schedule_hours", 12)
    log.info(f"Starting background scheduler — interval: {hours}h")

    run_job()   # run immediately on startup

    sched = BackgroundScheduler()
    sched.add_job(run_job, "interval", hours=hours,
                  id="ticket_agent", max_instances=1)
    sched.start()
    log.info(f"Scheduler running — next run in {hours} hours")

    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()
        log.info("Scheduler stopped")


if __name__ == "__main__":
    print("Testing database connection...")
    try:
        conn = get_db_connection()
        conn.close()
        print("Database connection OK")
    except Exception as e:
        print(f"Database connection FAILED: {e}")
        print("Check DB_* variables in your .env file")
        sys.exit(1)

    config = load_config()
    hours  = config.get("schedule_hours", 12)
    log.info(f"Starting agent — interval: {hours}h")
    run_job()
    scheduler = BlockingScheduler()
    scheduler.add_job(run_job, "interval", hours=hours,
                      id="ticket_agent", max_instances=1)
    log.info(f"Next run in {hours} hours. Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Agent stopped")
