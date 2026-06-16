# WhatsApp Support Ticket Agent - Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    UPLOAD LAYER                                  │
├─────────────────────────────────────────────────────────────────┤
│  • Web UI (upload_ui.html)                                      │
│  • CLI Client (upload_client.py)                                │
│  • Both → POST to /upload-whatsapp-zip endpoint                 │
└────────────────────┬────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                   API SERVER (Flask)                             │
│                    api_server.py                                │
├─────────────────────────────────────────────────────────────────┤
│  PORT: 5001                                                      │
│                                                                  │
│  Key Functions:                                                  │
│  ├─ /upload-whatsapp-zip (POST)                                │
│  ├─ /health (GET)                                              │
│  └─ / (GET - serves upload UI)                                 │
│                                                                  │
│  Responsibilities:                                               │
│  ├─ Extract ZIP file                                           │
│  ├─ Parse chat.txt (group name extraction)                    │
│  ├─ Call scheduler.py extraction agents                        │
│  ├─ Fetch Frappe data (customers, mappings)                   │
│  ├─ Map group_name → customer (via mapping table)             │
│  └─ Send tickets to Frappe API                                │
└────────────────────┬────────────────────────────────────────────┘
                     │
          ┌──────────┴──────────┐
          │                     │
          ▼                     ▼
    ┌──────────────┐    ┌──────────────────┐
    │ scheduler.py │    │  Frappe Instance │
    │              │    │  (pm.lucrumerp)  │
    └──────────────┘    └──────────────────┘
          │                     │
          │                     │
          ▼                     ▼
```

---

## Component Details

### 1. **UPLOAD LAYER** (Client-side)
**Files:** `upload_ui.html`, `upload_client.py`

**Purpose:** Accept WhatsApp chat ZIP files from users

**How it works:**
- Web UI: Drag-drop interface on http://localhost:5001
- CLI: `python3 upload_client.py path/to/chat.zip`
- Both send multipart form-data POST request

---

### 2. **API SERVER** (api_server.py)
**Port:** 5001 (Flask)

**Main Flow:**

```
Request → Extract ZIP
          ↓
       Parse chat.txt → Extract group_name
          ↓
       Call scheduler.py (3 parallel agents)
          ↓
       Get extracted tickets
          ↓
       Fetch Frappe data:
       ├─ Valid customers list
       ├─ WhatsApp group mapping table
       └─ Valid projects
          ↓
       Map each ticket:
       ├─ Look up group_name in mapping → get customer
       ├─ If not found, check if group_name is valid customer
       ├─ If not, use fallback "ABC"
       └─ Validate category (must be Bug/Question/Request/Training/Escalation)
          ↓
       Send to Frappe API:
       └─ POST /api/resource/Pulse Support Ticket (10 tickets per request)
          ↓
       Return result to client
```

**Key Functions:**

| Function | Purpose |
|----------|---------|
| `extract_group_name(zip_path)` | Extract & clean group name from ZIP filename |
| `send_to_frappe_listener(tickets, group_name)` | Fetch Frappe data, map customers, send tickets |
| `fetch_from_frappe()` | Get customers & mappings from Frappe |

**Group Name Cleaning Logic:**
```python
1. Remove "WhatsApp Chat - " prefix
2. Remove upload timestamp from START (YYYYMMDD_HHMM_)
3. Remove backup timestamps from END (_YYYYMMDD_HHMM...)
4. Result: Clean group name ready for lookup
```

---

### 3. **SCHEDULER/EXTRACTION ENGINE** (scheduler.py)
**Purpose:** Extract tickets from chat messages using parallel AI agents

**3 Parallel Agents:**

```
┌─────────────────────────────────────────────┐
│           Chat Timeline (589 messages)      │
├─────────────────────────────────────────────┤
│                    ↓                         │
│    Split by type (text/voice/image)        │
│                    ↓                         │
│  ┌─────────────┬──────────────┬───────────┐ │
│  ▼             ▼              ▼           ▼ │
│  Text         Voice+Image    Image      Meta│
│  Agent        Agent          Agent      (skip)│
│  │             │              │             │
│  ├─Extract   ├─Convert      ├─Analyze      │
│  │ tickets   │ OPUS→WAV     │ images       │
│  │ from text │ (OpenAI)     │ with Claude  │
│  │ (Claude)  │ Transcribe   │             │
│  │           │ (Whisper)    │             │
│  │           │ Use text +   │             │
│  │           │ transcript   │             │
│  │           │ (Claude)     │             │
│  │             │              │             │
│  └─────────────┴──────────────┴───────────┘ │
│                    ↓                         │
│            Merge all tickets               │
└─────────────────────────────────────────────┘
```

**Agent Output Format (per ticket):**
```json
{
  "sender": "Rohan Amir",
  "timestamp": "2026-06-02 14:32:41",
  "raw_message": "Original message text",
  "normalised": "Clean, structured title",
  "category": "Bug | Question | Request | Training | Escalation",
  "confidence": 95,
  "needs_review": false,
  "source_type": "text | voice | image",
  "voice_transcript": "...",
  "image_context": "...",
  "media_files": [...]
}
```

---

### 4. **FRAPPE INTEGRATION** (in api_server.py)

**Data Fetched:**

1. **Customers List**
   - Endpoint: `GET /api/resource/Customer`
   - Used to: Validate if group_name exists as customer
   - Returns: List of all customer names

2. **WhatsApp Group Mapping** (Single Doctype)
   - Endpoint: `GET /api/resource/Whatsapp Customer Group/Whatsapp Customer Group`
   - Structure:
     ```json
     {
       "whatsapp_group_mapping": [
         {
           "whatsapp_group": "Sweet Creme x Lucrum ERP",
           "erp_customer": "Sweet Creme"
         }
       ]
     }
     ```
   - Used to: Map group names to customers

3. **Ticket Creation**
   - Endpoint: `POST /api/resource/Pulse Support Ticket`
   - Creates ticket with all extracted fields

**Customer Lookup Logic (in order):**
```python
IF group_name in whatsapp_group_mapping:
    customer = mapping[group_name]  # Use mapped customer
ELIF group_name in valid_customers:
    customer = group_name  # Group name is itself a customer
ELSE:
    customer = "ABC"  # Fallback
```

---

## Data Flow Summary

```
ZIP File Input
     ↓
Extract group name + chat
     ↓
Run 3 parallel extraction agents (Claude, Whisper, OpenAI)
     ↓
Get ~10 tickets with extracted fields
     ↓
Fetch customer mapping from Frappe
     ↓
Map group_name → customer for each ticket
     ↓
Validate categories + fill module rotation
     ↓
Build Frappe payload (title, customer, category, etc)
     ↓
POST to Frappe API
     ↓
Return results to user (created count, failed count, cost)
```

---

## File Responsibilities

| File | Purpose | Dependencies |
|------|---------|--------------|
| `api_server.py` | REST API server, orchestration, Frappe sync | scheduler.py, requests |
| `scheduler.py` | 3 extraction agents (text, voice, image) | anthropic, openai |
| `upload_client.py` | CLI uploader tool | requests |
| `upload_ui.html` | Web upload interface | html/js |
| `test_frappe_api.py` | Debug tool for Frappe API | requests |

---

## Key Decisions & Tradeoffs

| Decision | Why | Tradeoff |
|----------|-----|----------|
| 3 parallel agents | Coverage - catches text, voice, images | More API calls = higher cost |
| Direct to Frappe API | Fast, reliable, no DB intermediate | Depends on Frappe REST API stability |
| Mapping table in Frappe | Single source of truth, easy to update | Requires Frappe instance |
| Group name as lookup key | Simple, human-readable | Requires exact matching with Frappe names |
| Fallback to "ABC" customer | Always creates ticket (no rejections) | May create tickets for unknown groups |

---

## Current Limitations & Future Improvements

**Current:**
- ✅ Group name mapping (customer identification)
- ✅ Category validation
- ✅ Module rotation
- ✅ Cost tracking

**Future (Ready to add):**
- ⏳ Phone number tracking (per customer/team)
- ⏳ Skip already-solved tickets (check status)
- ⏳ Team member identification
- ⏳ Client vs team message differentiation

---

## Environment Variables Required

```bash
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-proj-...
FRAPPE_LISTENER_URL=https://pm.lucrumerp.com/api/resource/Pulse Support Ticket
FRAPPE_API_KEY=...
FRAPPE_API_SECRET=...
```

