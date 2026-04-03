"""
Snaggit AI Bot — v3 (Multi-Inspector)
=====================================
All state in Supabase. Multiple inspectors via join code.
Zones defined upfront. AI defect classification.
PDF sent to ALL members in Telegram.

Tables (Supabase):
  inspections        — one per inspection, meta + join code
  inspection_zones   — one per zone, defects[], assigned_to, status
  inspection_members — who joined this inspection

Flow:
  Lead:   /start → New → meta fields → add zones → get code → start/wait
  Member: /start → Join → enter code → pick zone → inspect
  Both:   pick zone → photo → AI → confirm → next defect / finish zone
  Any:    /finish → if all zones done → PDF → send to all members
"""

import json, os, logging, subprocess, asyncio, re, base64, random, string, textwrap
from datetime import datetime, timezone
from io import BytesIO
from uuid import uuid4

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_KEY", "")
SUPABASE_URL   = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY", "")
REPORT_DIR     = os.environ.get("REPORT_DIR", "/app/data")
ASSETS_DIR     = os.environ.get("ASSETS_DIR", REPORT_DIR)

# ── Supabase client ──────────────────────────────────────────────────────────
_SUPABASE = None
try:
    from supabase import create_client
    if SUPABASE_URL and SUPABASE_KEY:
        _SUPABASE = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase connected")
    else:
        logger.warning("SUPABASE_URL or SUPABASE_KEY not set")
except ImportError:
    logger.warning("supabase package not installed")

# ── Conversation states ──────────────────────────────────────────────────────
(
    START_MENU,                                           # 0
    # Meta fields (lead only)
    DATE, PROJECT_NAME, UNIT_NUMBER, PROPERTY_TYPE,       # 1-4
    CLIENT_NAME, CLIENT_EMAIL, REASON,                    # 5-7
    INSPECTOR, ADDRESS, DEVELOPER,                        # 8-10
    TOTAL_AREA, FLOOR_NUMBER, NUM_ROOMS, FURNISHED,       # 11-14
    YEAR_BUILT,                                           # 15
    # Zone setup (lead only)
    SETUP_ZONE_NAME, SETUP_ZONE_TYPE, SETUP_ZONES_DONE,  # 16-18
    # Join (member)
    JOIN_CODE,                                            # 19
    # Inspection
    PICK_ZONE,                                            # 20
    DEFECT_PHOTO, AI_CONFIRM, DEFECT_SEVERITY,            # 21-23
    DEFECT_DESC, AFTER_DEFECT,                            # 24-25
    # Resume
    RESUME_MENU,                                          # 26
) = range(27)

# ── Constants ────────────────────────────────────────────────────────────────
PROPERTY_TYPES = ["Apartment", "Villa", "Townhouse", "Penthouse", "Duplex", "Studio", "Office"]
FURNISHED_OPTIONS = ["Furnished", "Unfurnished", "Semi-furnished"]
SEVERITY_OPTIONS = ["minor", "medium", "critical"]

MEP_CHECKLISTS = {
    "electrical": [
        "Appliances — functional check",
        "Power sockets — condition and load check",
        "DB panel — overheating and safety check",
        "Lights — operation and condition check",
    ],
    "hvac": [
        "Ceiling space — visual inspection",
        "Thermal camera — heat anomaly detection",
        "Exhaust fans — airflow and operation check",
    ],
    "plumbing": [
        "Sinks & taps — leakage and pressure check",
        "Drainage pipes & balcony drainage — flow test",
        "Shower drainage — blockage and flow check",
        "Water heaters — functionality and safety check",
        "Toilet flush — proper operation test",
    ],
    "moisture": [
        "Wall moisture — dampness and leakage detection",
    ],
}

# ── AI Prompts ───────────────────────────────────────────────────────────────
DEFECT_ANALYSIS_PROMPT = """You are a professional property snagging inspector in Dubai writing a snagging report.

Analyse this defect photo. Return ONLY raw JSON, no markdown, no code fences:
{
  "severity": "minor" | "medium" | "critical",
  "description": "2-5 words, defect noun first",
  "confidence": "high" | "medium" | "low"
}

IGNORE COMPLETELY: stickers, coloured dots, numbered labels, tape, markers = inspector's own snagging stickers, NOT defects.
IGNORE: furniture, personal items, curtains, appliances (unless the appliance itself is damaged).

SEVERITY RULES:
- critical: structural crack through wall/floor, active water leak/damp, exposed wiring, broken glass, lock won't work
- medium: paint run/drip/large patch missing, silicone gap at wet area, tile crack/grout gap, warped/misaligned door or cabinet, plaster undulation, loose fixture
- minor: small scratch/scuff under 5cm, paint splash on tile, dust/debris, hairline surface crack, slight overspray

DESCRIPTION EXAMPLES:
"Paint run" / "Silicone gap at bath" / "Tile crack" / "Surface marking on frame" / "Grout gap" / "Plaster undulation" / "Skirting loose" / "Paint splash on tile"

NEVER say: damage, risk, poor, bad, broken, compromised.
Use: surface marking, gap, crack, run, incomplete, loose, misaligned, stained."""

MEP_DEFECT_ANALYSIS_PROMPT = """You are a professional property snagging inspector in Dubai testing MEP systems.
This photo is from a MEP zone (Electrical / HVAC / Plumbing / Wall Moisture).

MEP zones only flag items that genuinely FAIL a functional test.
If the item appears functional, return compliant.

IGNORE COMPLETELY: stickers, coloured dots, numbered labels, tape, markers = inspector's stickers.

Return ONLY raw JSON:
{
  "severity": "compliant" | "minor" | "medium" | "critical",
  "description": "2-5 words or 'Functional and compliant'",
  "confidence": "high" | "medium" | "low"
}

MEP severity:
- compliant: item is functional, no visible defect
- critical: non-functional (no power, no water, no airflow, active leak)
- medium: functional but visibly faulty (loose socket, dripping tap, noisy unit)
- minor: cosmetic MEP only (scratched plate, loose cover, label missing)"""


# ══════════════════════════════════════════════════════════════════════════════
#  SUPABASE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _sb():
    """Return Supabase client or raise."""
    if not _SUPABASE:
        raise RuntimeError("Supabase not connected")
    return _SUPABASE


def generate_join_code() -> str:
    """6-char uppercase alphanumeric code."""
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


# ── Inspection CRUD ──────────────────────────────────────────────────────────

def create_inspection(user_id: str, meta: dict) -> dict:
    """Create a new inspection row, return it."""
    code = generate_join_code()
    row = {
        "code": code,
        "status": "setup",
        "meta": meta,
        "created_by": user_id,
    }
    res = _sb().table("inspections").insert(row).execute()
    return res.data[0]


def get_inspection_by_code(code: str) -> dict | None:
    res = _sb().table("inspections").select("*").eq("code", code.upper().strip()).execute()
    return res.data[0] if res.data else None


def get_inspection_by_id(inspection_id: str) -> dict | None:
    res = _sb().table("inspections").select("*").eq("id", inspection_id).execute()
    return res.data[0] if res.data else None


def update_inspection(inspection_id: str, **kwargs):
    kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
    _sb().table("inspections").update(kwargs).eq("id", inspection_id).execute()


# ── Zones CRUD ───────────────────────────────────────────────────────────────

def add_zone(inspection_id: str, zone_number: int, name: str, zone_type: str = "regular") -> dict:
    row = {
        "inspection_id": inspection_id,
        "zone_number": zone_number,
        "name": name,
        "type": zone_type,
        "status": "pending",
        "defects": [],
    }
    res = _sb().table("inspection_zones").insert(row).execute()
    return res.data[0]


def get_zones(inspection_id: str) -> list:
    res = (_sb().table("inspection_zones")
           .select("*")
           .eq("inspection_id", inspection_id)
           .order("zone_number")
           .execute())
    return res.data


def get_zone_by_id(zone_id: str) -> dict | None:
    res = _sb().table("inspection_zones").select("*").eq("id", zone_id).execute()
    return res.data[0] if res.data else None


def update_zone(zone_id: str, **kwargs):
    kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
    _sb().table("inspection_zones").update(kwargs).eq("id", zone_id).execute()


def append_defect_to_zone(zone_id: str, defect: dict):
    """Append a defect to zone's defects JSONB array."""
    zone = get_zone_by_id(zone_id)
    defects = zone.get("defects") or []
    defects.append(defect)
    update_zone(zone_id, defects=defects)


def delete_last_defect_from_zone(zone_id: str) -> bool:
    zone = get_zone_by_id(zone_id)
    defects = zone.get("defects") or []
    if not defects:
        return False
    defects.pop()
    update_zone(zone_id, defects=defects)
    return True


# ── Members CRUD ─────────────────────────────────────────────────────────────

def add_member(inspection_id: str, user_id: str, name: str = "", role: str = "inspector"):
    _sb().table("inspection_members").upsert({
        "inspection_id": inspection_id,
        "user_id": user_id,
        "name": name,
        "role": role,
    }).execute()


def get_members(inspection_id: str) -> list:
    res = _sb().table("inspection_members").select("*").eq("inspection_id", inspection_id).execute()
    return res.data


def get_user_active_inspection(user_id: str) -> dict | None:
    """Find active (non-complete) inspection for this user."""
    res = (_sb().table("inspection_members")
           .select("inspection_id")
           .eq("user_id", user_id)
           .execute())
    for m in res.data:
        insp = get_inspection_by_id(m["inspection_id"])
        if insp and insp["status"] != "complete":
            return insp
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  AI ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

async def analyse_photo_with_ai(photo_bytes: bytes, is_mep: bool = False) -> dict:
    """Send photo to Claude Vision, return parsed JSON."""
    image_data = base64.standard_b64encode(photo_bytes).decode("utf-8")
    prompt = MEP_DEFECT_ANALYSIS_PROMPT if is_mep else DEFECT_ANALYSIS_PROMPT

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 300,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_data}},
                        {"type": "text", "text": prompt},
                    ],
                }],
            },
        )
        data = resp.json()

        if "content" not in data:
            logger.error(f"AI response missing 'content': {data}")
            return {"severity": "medium", "description": "AI analysis unavailable", "confidence": "low"}

        text = data["content"][0]["text"].strip()
        # Strip markdown code fences
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())


# ══════════════════════════════════════════════════════════════════════════════
#  AI OBSERVATION TEXTS (for PDF)
# ══════════════════════════════════════════════════════════════════════════════

async def generate_ai_texts(meta: dict, zones: list) -> dict:
    """Generate summary, general condition, and urgent items texts."""
    # Build a concise summary of all defects
    all_defects = []
    for z in zones:
        for d in (z.get("defects") or []):
            all_defects.append(f"{z['name']}: {d.get('description', '?')} ({d.get('severity', '?')})")

    defect_summary = "\n".join(all_defects[:40]) if all_defects else "No defects found."
    total = len(all_defects)
    unit = meta.get("unit", "?")
    project = meta.get("project", "?")
    reason = meta.get("reason", "handover")

    prompt = textwrap.dedent(f"""\
    You are writing the summary section of a property snagging report for a Dubai property inspection.

    Project: {project}
    Unit: {unit}
    Total defects: {total}
    Reason: {reason}

    Defects by zone:
    {defect_summary}

    Write THREE short paragraphs in professional English. No markdown, no bullets, plain text only.
    Return ONLY raw JSON:
    {{
      "summary_obs": "2-3 sentences. Overview of unit condition and total comments found.",
      "general_condition": "2-3 sentences. Property condition, number of zones inspected, key areas of concern.",
      "urgent": "1-2 sentences listing the most critical/medium items that need immediate attention. If none, say so."
    }}
    """)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 600,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            data = resp.json()
            text = data["content"][0]["text"].strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text.strip())
    except Exception as e:
        logger.error(f"AI texts failed: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════════════════
#  KEYBOARD HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def inline_kb(options: list, prefix: str, columns: int = 2) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text=opt, callback_data=f"{prefix}:{opt}") for opt in options]
    rows = [buttons[i:i+columns] for i in range(0, len(buttons), columns)]
    return InlineKeyboardMarkup(rows)


def zone_picker_kb(zones: list, user_id: str) -> InlineKeyboardMarkup:
    """Build keyboard showing available zones. Taken zones show who has them."""
    buttons = []
    for z in zones:
        status = z["status"]
        assigned = z.get("assigned_to")
        name = z["name"]
        ztype = " ⚡" if z["type"] == "mep" else ""

        if status == "done":
            label = f"✅ {z['zone_number']}. {name}{ztype}"
            buttons.append([InlineKeyboardButton(text=label, callback_data=f"zone:done:{z['id']}")])
        elif assigned and assigned != user_id:
            label = f"🔒 {z['zone_number']}. {name}{ztype} (taken)"
            buttons.append([InlineKeyboardButton(text=label, callback_data=f"zone:taken:{z['id']}")])
        else:
            n_defects = len(z.get("defects") or [])
            extra = f" ({n_defects} defects)" if n_defects else ""
            label = f"📍 {z['zone_number']}. {name}{ztype}{extra}"
            buttons.append([InlineKeyboardButton(text=label, callback_data=f"zone:pick:{z['id']}")])

    buttons.append([InlineKeyboardButton(text="🏁 Finish inspection", callback_data="zone:finish")])
    return InlineKeyboardMarkup(buttons)


# ══════════════════════════════════════════════════════════════════════════════
#  TEXT CLEANING (for PDF generation)
# ══════════════════════════════════════════════════════════════════════════════

def clean_unicode(text: str) -> str:
    """Replace problematic Unicode chars with ASCII equivalents."""
    if not text:
        return ""
    replacements = {
        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
        "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u00a0": " ",
        "\u00b2": "sq", "\u00b0": " deg", "\u2032": "'", "\u2033": '"',
        "\u200b": "", "\ufeff": "",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text


def trunc(text: str, max_len: int = 80) -> str:
    """Truncate and clean text for PDF."""
    text = clean_unicode(str(text or ""))
    text = text.replace("\n", " ").replace("\r", " ").strip()
    return text[:max_len] if len(text) > max_len else text


# ══════════════════════════════════════════════════════════════════════════════
#  BOT HANDLERS — START / NEW / JOIN
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point. Show start menu."""
    user_id = str(update.effective_user.id)
    context.user_data.clear()

    # Check for active inspection
    active = get_user_active_inspection(user_id) if _SUPABASE else None

    buttons = [
        [InlineKeyboardButton("🆕 New inspection", callback_data="start:new")],
        [InlineKeyboardButton("🔗 Join inspection", callback_data="start:join")],
    ]
    if active:
        buttons.insert(0, [InlineKeyboardButton(
            f"▶️ Resume: {active['meta'].get('project', '?')} — Unit {active['meta'].get('unit', '?')}",
            callback_data=f"start:resume:{active['id']}"
        )])

    await update.message.reply_text(
        "👋 <b>Snaggit AI — Property Inspection Bot</b>\n\n"
        "Choose an option:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return START_MENU


async def start_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle start menu selection."""
    query = update.callback_query
    await query.answer()
    data = query.data  # start:new / start:join / start:resume:<id>

    if data == "start:new":
        context.user_data["_meta"] = {}
        context.user_data["_role"] = "lead"
        await query.edit_message_text(
            "📅 <b>Step 1/15 — Date of Inspection</b>\n\nEnter date (e.g. 19.03.2026):",
            parse_mode="HTML",
        )
        return DATE

    elif data == "start:join":
        await query.edit_message_text(
            "🔗 <b>Join Inspection</b>\n\nEnter the 6-character join code:",
            parse_mode="HTML",
        )
        return JOIN_CODE

    elif data.startswith("start:resume:"):
        inspection_id = data.split(":", 2)[2]
        context.user_data["_inspection_id"] = inspection_id
        return await _show_zone_picker(query, context, inspection_id)

    return START_MENU


# ══════════════════════════════════════════════════════════════════════════════
#  META FIELD HANDLERS (lead only, 15 steps)
# ══════════════════════════════════════════════════════════════════════════════

META_FIELDS = [
    # (state, key, step_num, prompt, options_or_None)
    (DATE,          "date",      1,  "📅 Date of Inspection\n(e.g. 19.03.2026)", None),
    (PROJECT_NAME,  "project",   2,  "🏗 Project Name", None),
    (UNIT_NUMBER,   "unit",      3,  "🔢 Unit Number", None),
    (PROPERTY_TYPE, "type",      4,  "🏠 Property Type", PROPERTY_TYPES),
    (CLIENT_NAME,   "client",    5,  "👤 Client Name", None),
    (CLIENT_EMAIL,  "email",     6,  "📧 Client Email", None),
    (REASON,        "reason",    7,  "📋 Reason for Inspection", None),
    (INSPECTOR,     "inspector", 8,  "🧑‍🔧 Inspector Name", None),
    (ADDRESS,       "address",   9,  "📍 Property Address\n(e.g. Sobha Hartland, MBR City, Dubai)", None),
    (DEVELOPER,     "developer", 10, "🏢 Developer\n(e.g. Emaar, Sobha, Damac)", None),
    (TOTAL_AREA,    "area",      11, "📐 Total Area\n(e.g. 1200 sq ft)", None),
    (FLOOR_NUMBER,  "floor",     12, "🏢 Floor Number", None),
    (NUM_ROOMS,     "rooms",     13, "🛏 Number of Rooms\n(e.g. 1 Bedroom)", None),
    (FURNISHED,     "furnished", 14, "🛋 Furnished?", FURNISHED_OPTIONS),
    (YEAR_BUILT,    "year",      15, "📅 Year Built", None),
]

_META_BY_STATE = {state: i for i, (state, *_) in enumerate(META_FIELDS)}


def _next_meta_prompt(step_index: int) -> tuple:
    """Return (state, prompt_text, reply_markup_or_None) for the next step."""
    if step_index >= len(META_FIELDS):
        return None, None, None
    state, key, num, prompt, options = META_FIELDS[step_index]
    full_prompt = f"<b>Step {num}/15 — {prompt}</b>"
    markup = inline_kb(options, "meta") if options else None
    return state, full_prompt, markup


async def _handle_meta_text(update: Update, context: ContextTypes.DEFAULT_TYPE, current_state: int) -> int:
    """Generic handler for text-based meta fields."""
    idx = _META_BY_STATE.get(current_state)
    if idx is None:
        return current_state
    _, key, *_ = META_FIELDS[idx]
    context.user_data["_meta"][key] = update.message.text.strip()

    # Move to next
    next_idx = idx + 1
    next_state, prompt, markup = _next_meta_prompt(next_idx)
    if next_state is None:
        return await _meta_done(update, context)
    await update.message.reply_text(prompt, parse_mode="HTML", reply_markup=markup)
    return next_state


async def _handle_meta_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, current_state: int) -> int:
    """Generic handler for callback-based meta fields (property type, furnished)."""
    query = update.callback_query
    await query.answer()
    idx = _META_BY_STATE.get(current_state)
    if idx is None:
        return current_state
    _, key, *_ = META_FIELDS[idx]
    value = query.data.split(":", 1)[1]
    context.user_data["_meta"][key] = value

    next_idx = idx + 1
    next_state, prompt, markup = _next_meta_prompt(next_idx)
    if next_state is None:
        return await _meta_done_from_callback(query, context)
    await query.edit_message_text(prompt, parse_mode="HTML", reply_markup=markup)
    return next_state


async def _meta_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """All 15 meta fields collected. Move to zone setup."""
    await update.message.reply_text(
        "✅ <b>Meta data complete!</b>\n\n"
        "Now let's define the inspection zones.\n\n"
        "📍 <b>Enter the name of Zone 1</b>\n(e.g. Entrance, Master Bedroom, Kitchen):",
        parse_mode="HTML",
    )
    context.user_data["_zone_count"] = 0
    context.user_data["_zones_setup"] = []
    return SETUP_ZONE_NAME


async def _meta_done_from_callback(query, context: ContextTypes.DEFAULT_TYPE) -> int:
    await query.edit_message_text(
        "✅ <b>Meta data complete!</b>\n\n"
        "Now let's define the inspection zones.\n\n"
        "📍 <b>Enter the name of Zone 1</b>\n(e.g. Entrance, Master Bedroom, Kitchen):",
        parse_mode="HTML",
    )
    context.user_data["_zone_count"] = 0
    context.user_data["_zones_setup"] = []
    return SETUP_ZONE_NAME


# Individual meta handlers — they all delegate to the generic ones
async def h_date(u, c):       return await _handle_meta_text(u, c, DATE)
async def h_project(u, c):    return await _handle_meta_text(u, c, PROJECT_NAME)
async def h_unit(u, c):       return await _handle_meta_text(u, c, UNIT_NUMBER)
async def h_type(u, c):       return await _handle_meta_callback(u, c, PROPERTY_TYPE)
async def h_client(u, c):     return await _handle_meta_text(u, c, CLIENT_NAME)
async def h_email(u, c):      return await _handle_meta_text(u, c, CLIENT_EMAIL)
async def h_reason(u, c):     return await _handle_meta_text(u, c, REASON)
async def h_inspector(u, c):  return await _handle_meta_text(u, c, INSPECTOR)
async def h_address(u, c):    return await _handle_meta_text(u, c, ADDRESS)
async def h_developer(u, c):  return await _handle_meta_text(u, c, DEVELOPER)
async def h_area(u, c):       return await _handle_meta_text(u, c, TOTAL_AREA)
async def h_floor(u, c):      return await _handle_meta_text(u, c, FLOOR_NUMBER)
async def h_rooms(u, c):      return await _handle_meta_text(u, c, NUM_ROOMS)
async def h_furnished(u, c):  return await _handle_meta_callback(u, c, FURNISHED)
async def h_year(u, c):       return await _handle_meta_text(u, c, YEAR_BUILT)


# ══════════════════════════════════════════════════════════════════════════════
#  ZONE SETUP (lead only)
# ══════════════════════════════════════════════════════════════════════════════

async def setup_zone_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive zone name, ask for type."""
    name = update.message.text.strip()
    context.user_data["_zone_count"] += 1
    context.user_data["_pending_zone_name"] = name

    await update.message.reply_text(
        f"Zone {context.user_data['_zone_count']}: <b>{name}</b>\n\nWhat type?",
        parse_mode="HTML",
        reply_markup=inline_kb(["Regular", "MEP"], "ztype"),
    )
    return SETUP_ZONE_TYPE


async def setup_zone_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive zone type, ask for next zone or done."""
    query = update.callback_query
    await query.answer()
    ztype = query.data.split(":", 1)[1].lower()
    name = context.user_data.pop("_pending_zone_name", "Zone")

    context.user_data["_zones_setup"].append({"name": name, "type": ztype})

    zones_so_far = context.user_data["_zones_setup"]
    zone_list = "\n".join(
        f"  {i+1}. {z['name']} {'⚡' if z['type'] == 'mep' else '📍'}"
        for i, z in enumerate(zones_so_far)
    )

    await query.edit_message_text(
        f"<b>Zones defined:</b>\n{zone_list}\n\n"
        "📍 Enter name of next zone, or press ✅ Done:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Done — start inspection", callback_data="zones:done")],
        ]),
    )
    return SETUP_ZONES_DONE


async def setup_zones_add_more(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User typed another zone name instead of pressing Done."""
    return await setup_zone_name(update, context)


async def setup_zones_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """All zones defined. Create inspection in Supabase, show join code."""
    query = update.callback_query
    await query.answer()

    user_id = str(update.effective_user.id)
    meta = context.user_data["_meta"]
    zones_setup = context.user_data["_zones_setup"]

    # Create inspection
    inspection = create_inspection(user_id, meta)
    inspection_id = inspection["id"]
    code = inspection["code"]

    # Add zones
    for i, z in enumerate(zones_setup, 1):
        add_zone(inspection_id, i, z["name"], z["type"])

    # Add lead as member
    add_member(inspection_id, user_id, meta.get("inspector", "Lead"), "lead")

    context.user_data["_inspection_id"] = inspection_id

    # Update inspection status
    update_inspection(inspection_id, status="active")

    zone_list = "\n".join(
        f"  {i+1}. {z['name']} {'⚡' if z['type'] == 'mep' else '📍'}"
        for i, z in enumerate(zones_setup)
    )

    await query.edit_message_text(
        f"✅ <b>Inspection created!</b>\n\n"
        f"🏗 {meta.get('project', '?')} — Unit {meta.get('unit', '?')}\n"
        f"📋 {len(zones_setup)} zones defined:\n{zone_list}\n\n"
        f"🔑 <b>Join code: <code>{code}</code></b>\n"
        f"Share this code with other inspectors.\n\n"
        f"Press Start to begin inspecting:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Start inspecting", callback_data="zones:start")],
        ]),
    )
    return PICK_ZONE


async def _begin_zone_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Transition to zone picker after 'Start inspecting' button."""
    query = update.callback_query
    await query.answer()
    inspection_id = context.user_data.get("_inspection_id")
    return await _show_zone_picker(query, context, inspection_id)


async def _show_zone_picker(message_or_query, context, inspection_id: str) -> int:
    """Show the zone selection keyboard."""
    user_id = str(message_or_query.from_user.id) if hasattr(message_or_query, "from_user") else str(context._user_id)
    zones = get_zones(inspection_id)
    context.user_data["_inspection_id"] = inspection_id

    kb = zone_picker_kb(zones, user_id)

    # Count progress
    done_zones = sum(1 for z in zones if z["status"] == "done")
    total_zones = len(zones)
    total_defects = sum(len(z.get("defects") or []) for z in zones)

    text = (
        f"📋 <b>Pick a zone to inspect</b>\n\n"
        f"Progress: {done_zones}/{total_zones} zones done | {total_defects} defects total"
    )

    if hasattr(message_or_query, "edit_message_text"):
        await message_or_query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message_or_query.reply_text(text, parse_mode="HTML", reply_markup=kb)

    return PICK_ZONE


# ══════════════════════════════════════════════════════════════════════════════
#  JOIN INSPECTION
# ══════════════════════════════════════════════════════════════════════════════

async def join_code_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User entered a join code."""
    code = update.message.text.strip().upper()

    inspection = get_inspection_by_code(code)
    if not inspection:
        await update.message.reply_text(
            "❌ Code not found. Check the code and try again:",
            parse_mode="HTML",
        )
        return JOIN_CODE

    user_id = str(update.effective_user.id)
    user_name = update.effective_user.first_name or "Inspector"

    # Add as member
    add_member(inspection["id"], user_id, user_name, "inspector")
    context.user_data["_inspection_id"] = inspection["id"]

    meta = inspection.get("meta", {})
    await update.message.reply_text(
        f"✅ <b>Joined!</b>\n\n"
        f"🏗 {meta.get('project', '?')} — Unit {meta.get('unit', '?')}\n"
        f"Press Start to pick a zone:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Start inspecting", callback_data="zones:start")],
        ]),
    )
    return PICK_ZONE


# ══════════════════════════════════════════════════════════════════════════════
#  ZONE PICKING & INSPECTION
# ══════════════════════════════════════════════════════════════════════════════

async def zone_pick_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle zone picker callbacks."""
    query = update.callback_query
    await query.answer()
    data = query.data  # zone:pick:<id> / zone:done:<id> / zone:taken:<id> / zone:finish

    if data == "zone:finish":
        return await _try_finish(query, context)

    if data.startswith("zone:start"):
        # "Start inspecting" button
        inspection_id = context.user_data.get("_inspection_id")
        return await _show_zone_picker(query, context, inspection_id)

    parts = data.split(":", 2)
    action = parts[1]
    zone_id = parts[2]

    if action == "done":
        await query.answer("This zone is already completed.", show_alert=True)
        return PICK_ZONE

    if action == "taken":
        await query.answer("This zone is being inspected by another team member.", show_alert=True)
        return PICK_ZONE

    if action == "pick":
        user_id = str(update.effective_user.id)
        zone = get_zone_by_id(zone_id)

        # Assign zone to this user (or re-enter if already assigned)
        update_zone(zone_id, assigned_to=user_id, status="in_progress")
        context.user_data["_current_zone_id"] = zone_id

        # Show zone info
        is_mep = zone["type"] == "mep"
        context.user_data["_is_mep"] = is_mep

        if is_mep:
            checklist_text = _get_mep_checklist_text(zone["name"])
            existing = len(zone.get("defects") or [])
            await query.edit_message_text(
                f"⚡ <b>MEP Zone {zone['zone_number']}: {zone['name']}</b>\n\n"
                f"{checklist_text}"
                f"{'📸 ' + str(existing) + ' items already recorded. ' if existing else ''}"
                "📸 Send photo of each item you're testing.\n"
                "AI will mark it <b>compliant</b> or flag a defect.\n\n"
                "Send a photo, or /skip to mark all compliant:",
                parse_mode="HTML",
            )
        else:
            existing = len(zone.get("defects") or [])
            await query.edit_message_text(
                f"📍 <b>Zone {zone['zone_number']}: {zone['name']}</b>\n\n"
                f"{'📸 ' + str(existing) + ' defects already recorded. ' if existing else ''}"
                "📸 Send a photo of the defect.\n"
                "Or /skip if no photo available:",
                parse_mode="HTML",
            )
        return DEFECT_PHOTO

    return PICK_ZONE


def _get_mep_checklist_text(zone_name: str) -> str:
    """Return formatted MEP checklist for this zone type."""
    key = zone_name.lower().strip()
    for k, items in MEP_CHECKLISTS.items():
        if k in key:
            lines = "\n".join(f"  ☐ {item}" for item in items)
            return f"📋 <b>Checklist:</b>\n{lines}\n\n"
    # If no match, show all categories
    return ""


# ══════════════════════════════════════════════════════════════════════════════
#  DEFECT FLOW: photo → AI → confirm → severity → description → after
# ══════════════════════════════════════════════════════════════════════════════

async def defect_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive defect photo, send to AI."""
    if not update.message.photo:
        await update.message.reply_text("📸 Please send a photo. Or /skip if no photo available.")
        return DEFECT_PHOTO

    # Get the largest photo
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)

    # Download to memory
    bio = BytesIO()
    await file.download_to_memory(bio)
    photo_bytes = bio.getvalue()

    # Store photo info
    context.user_data["_temp_photo_file_id"] = photo.file_id
    context.user_data["_temp_photo_bytes"] = photo_bytes

    # AI analysis
    is_mep = context.user_data.get("_is_mep", False)
    await update.message.reply_text("🤖 Analyzing photo...")

    try:
        result = await analyse_photo_with_ai(photo_bytes, is_mep=is_mep)
    except Exception as e:
        logger.error(f"AI analysis failed: {e}")
        result = {"severity": "medium", "description": "AI unavailable", "confidence": "low"}

    context.user_data["_ai_result"] = result
    severity = result.get("severity", "medium")
    desc = result.get("description", "?")
    confidence = result.get("confidence", "?")

    # Build confirmation message
    sev_emoji = {"critical": "🔴", "medium": "🟠", "minor": "🟡", "compliant": "🟢"}.get(severity, "⚪")

    buttons = [
        [InlineKeyboardButton(f"✅ Confirm: {sev_emoji} {severity} — {desc}", callback_data="ai:confirm")],
        [InlineKeyboardButton("✏️ Change severity", callback_data="ai:severity")],
        [InlineKeyboardButton("📝 Edit description", callback_data="ai:desc")],
    ]
    if is_mep and severity == "compliant":
        buttons = [
            [InlineKeyboardButton(f"✅ Confirm: 🟢 compliant", callback_data="ai:confirm")],
            [InlineKeyboardButton("⚠️ Actually a defect", callback_data="ai:severity")],
        ]

    await update.message.reply_text(
        f"🤖 <b>AI Analysis</b> (confidence: {confidence})\n\n"
        f"Severity: {sev_emoji} <b>{severity}</b>\n"
        f"Description: <b>{desc}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return AI_CONFIRM


async def skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Skip photo — go to manual severity."""
    context.user_data["_temp_photo_file_id"] = None
    context.user_data["_temp_photo_bytes"] = None
    context.user_data["_ai_result"] = None

    is_mep = context.user_data.get("_is_mep", False)
    if is_mep:
        # For MEP skip = mark zone as done (all compliant)
        zone_id = context.user_data.get("_current_zone_id")
        update_zone(zone_id, status="done")
        inspection_id = context.user_data["_inspection_id"]

        await update.message.reply_text("✅ Zone marked as all compliant.")
        return await _show_zone_picker_msg(update, context, inspection_id)

    await update.message.reply_text(
        "Select severity:",
        reply_markup=inline_kb(SEVERITY_OPTIONS, "sev"),
    )
    return DEFECT_SEVERITY


async def ai_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle AI confirmation callbacks."""
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]  # confirm / severity / desc

    if action == "confirm":
        result = context.user_data.get("_ai_result", {})
        return await _save_defect(query, context, result.get("severity", "medium"), result.get("description", "?"))

    elif action == "severity":
        await query.edit_message_text(
            "Select severity manually:",
            reply_markup=inline_kb(SEVERITY_OPTIONS, "sev"),
        )
        return DEFECT_SEVERITY

    elif action == "desc":
        await query.edit_message_text("📝 Type the defect description:")
        return DEFECT_DESC

    return AI_CONFIRM


async def defect_severity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Manual severity selection."""
    query = update.callback_query
    await query.answer()
    severity = query.data.split(":", 1)[1]
    context.user_data["_manual_severity"] = severity

    # Check if we have AI description to use
    ai = context.user_data.get("_ai_result")
    if ai and ai.get("description"):
        await query.edit_message_text(
            f"Severity: <b>{severity}</b>\n\n"
            f"AI suggested: <b>{ai['description']}</b>\n"
            "Use this description or type your own:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"✅ Use: {ai['description']}", callback_data=f"usedesc:{ai['description']}")],
            ]),
        )
    else:
        await query.edit_message_text(
            f"Severity: <b>{severity}</b>\n\n📝 Type the defect description:",
            parse_mode="HTML",
        )
    return DEFECT_DESC


async def defect_desc_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive typed description."""
    desc = update.message.text.strip()
    severity = context.user_data.get("_manual_severity", "medium")
    return await _save_defect_msg(update, context, severity, desc)


async def defect_desc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Use AI description."""
    query = update.callback_query
    await query.answer()
    desc = query.data.split(":", 1)[1]
    severity = context.user_data.get("_manual_severity", "medium")
    return await _save_defect(query, context, severity, desc)


async def _save_defect(query, context, severity: str, description: str) -> int:
    """Save defect to Supabase and show after-defect menu."""
    zone_id = context.user_data.get("_current_zone_id")
    photo_file_id = context.user_data.get("_temp_photo_file_id")

    defect = {
        "id": str(uuid4())[:8],
        "severity": severity,
        "description": clean_unicode(description),
        "photo_file_id": photo_file_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    append_defect_to_zone(zone_id, defect)

    # Clear temp
    context.user_data.pop("_temp_photo_bytes", None)
    context.user_data.pop("_ai_result", None)

    zone = get_zone_by_id(zone_id)
    count = len(zone.get("defects") or [])
    sev_emoji = {"critical": "🔴", "medium": "🟠", "minor": "🟡", "compliant": "🟢"}.get(severity, "⚪")

    buttons = [
        [InlineKeyboardButton("📸 Add another defect", callback_data="after:photo")],
        [InlineKeyboardButton("🔄 Switch zone", callback_data="after:switch")],
        [InlineKeyboardButton("✅ Finish this zone", callback_data="after:finishzone")],
        [InlineKeyboardButton("🗑 Delete last defect", callback_data="after:delete")],
    ]

    await query.edit_message_text(
        f"✅ Defect #{count} saved\n"
        f"{sev_emoji} {severity} — {description}\n\n"
        f"<b>Zone: {zone['name']}</b> ({count} defects)\n"
        "What's next?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return AFTER_DEFECT


async def _save_defect_msg(update: Update, context, severity: str, description: str) -> int:
    """Save defect (from message context, not callback)."""
    zone_id = context.user_data.get("_current_zone_id")
    photo_file_id = context.user_data.get("_temp_photo_file_id")

    defect = {
        "id": str(uuid4())[:8],
        "severity": severity,
        "description": clean_unicode(description),
        "photo_file_id": photo_file_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    append_defect_to_zone(zone_id, defect)
    context.user_data.pop("_temp_photo_bytes", None)
    context.user_data.pop("_ai_result", None)

    zone = get_zone_by_id(zone_id)
    count = len(zone.get("defects") or [])
    sev_emoji = {"critical": "🔴", "medium": "🟠", "minor": "🟡"}.get(severity, "⚪")

    buttons = [
        [InlineKeyboardButton("📸 Add another defect", callback_data="after:photo")],
        [InlineKeyboardButton("🔄 Switch zone", callback_data="after:switch")],
        [InlineKeyboardButton("✅ Finish this zone", callback_data="after:finishzone")],
        [InlineKeyboardButton("🗑 Delete last defect", callback_data="after:delete")],
    ]

    await update.message.reply_text(
        f"✅ Defect #{count} saved\n"
        f"{sev_emoji} {severity} — {description}\n\n"
        f"<b>Zone: {zone['name']}</b> ({count} defects)\n"
        "What's next?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return AFTER_DEFECT


# ══════════════════════════════════════════════════════════════════════════════
#  AFTER DEFECT ACTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def after_defect_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle after-defect menu."""
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "photo":
        zone = get_zone_by_id(context.user_data["_current_zone_id"])
        await query.edit_message_text(
            f"📍 <b>{zone['name']}</b>\n\n📸 Send photo of the next defect:",
            parse_mode="HTML",
        )
        return DEFECT_PHOTO

    elif action == "switch":
        inspection_id = context.user_data["_inspection_id"]
        # Release current zone assignment but keep it in_progress
        return await _show_zone_picker(query, context, inspection_id)

    elif action == "finishzone":
        zone_id = context.user_data["_current_zone_id"]
        update_zone(zone_id, status="done", assigned_to=None)
        inspection_id = context.user_data["_inspection_id"]
        zone = get_zone_by_id(zone_id)
        n = len(zone.get("defects") or [])
        await query.edit_message_text(
            f"✅ <b>Zone {zone['name']}</b> completed with {n} defects.",
            parse_mode="HTML",
        )
        return await _show_zone_picker_query(query, context, inspection_id)

    elif action == "delete":
        zone_id = context.user_data["_current_zone_id"]
        deleted = delete_last_defect_from_zone(zone_id)
        if deleted:
            zone = get_zone_by_id(zone_id)
            n = len(zone.get("defects") or [])
            await query.edit_message_text(
                f"🗑 Last defect deleted. Zone <b>{zone['name']}</b> now has {n} defects.\n\n"
                "📸 Send next photo, or:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Switch zone", callback_data="after:switch")],
                    [InlineKeyboardButton("✅ Finish this zone", callback_data="after:finishzone")],
                ]),
            )
            return DEFECT_PHOTO
        else:
            await query.answer("No defects to delete.", show_alert=True)
            return AFTER_DEFECT

    return AFTER_DEFECT


async def _show_zone_picker_msg(update: Update, context, inspection_id: str) -> int:
    """Show zone picker from a message context."""
    user_id = str(update.effective_user.id)
    zones = get_zones(inspection_id)
    kb = zone_picker_kb(zones, user_id)
    done_zones = sum(1 for z in zones if z["status"] == "done")
    total_defects = sum(len(z.get("defects") or []) for z in zones)

    await update.message.reply_text(
        f"📋 <b>Pick a zone to inspect</b>\n\n"
        f"Progress: {done_zones}/{len(zones)} zones done | {total_defects} defects total",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return PICK_ZONE


async def _show_zone_picker_query(query, context, inspection_id: str) -> int:
    """Show zone picker from a callback query context."""
    user_id = str(query.from_user.id)
    zones = get_zones(inspection_id)
    kb = zone_picker_kb(zones, user_id)
    done_zones = sum(1 for z in zones if z["status"] == "done")
    total_defects = sum(len(z.get("defects") or []) for z in zones)

    await query.message.reply_text(
        f"📋 <b>Pick a zone to inspect</b>\n\n"
        f"Progress: {done_zones}/{len(zones)} zones done | {total_defects} defects total",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return PICK_ZONE


# ══════════════════════════════════════════════════════════════════════════════
#  FINISH / PDF GENERATION
# ══════════════════════════════════════════════════════════════════════════════

async def _try_finish(query, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Check if all zones are done, if so generate PDF."""
    inspection_id = context.user_data.get("_inspection_id")
    zones = get_zones(inspection_id)

    not_done = [z for z in zones if z["status"] != "done"]
    if not_done:
        names = ", ".join(z["name"] for z in not_done)
        await query.edit_message_text(
            f"⚠️ <b>Cannot finish yet</b>\n\n"
            f"These zones are not complete: {names}\n\n"
            "Finish all zones first, or press the zone to mark it done.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to zones", callback_data="zones:start")],
            ]),
        )
        return PICK_ZONE

    # All zones done — generate PDF
    await query.edit_message_text("⏳ <b>Generating report...</b>\n\nDownloading photos and building PDF...", parse_mode="HTML")

    inspection = get_inspection_by_id(inspection_id)
    meta = inspection.get("meta", {})

    # Count severities
    sev_counts = {"critical": 0, "medium": 0, "minor": 0, "compliant": 0}
    total = 0
    for z in zones:
        for d in (z.get("defects") or []):
            sev = d.get("severity", "minor")
            sev_counts[sev] = sev_counts.get(sev, 0) + 1
            if sev != "compliant":
                total += 1

    # Generate AI observation texts
    await query.message.reply_text("⏳ Generating AI observations...")
    ai_texts = await generate_ai_texts(meta, zones)

    # Download photos
    await query.message.reply_text("⏳ Downloading photos...")
    photos_dir = os.path.join(REPORT_DIR, "photos")
    os.makedirs(photos_dir, exist_ok=True)
    photo_count = 0

    for z in zones:
        for d in (z.get("defects") or []):
            fid = d.get("photo_file_id")
            if fid:
                try:
                    tf = await context.bot.get_file(fid)
                    path = os.path.join(photos_dir, f"{fid}.jpg")
                    await tf.download_to_drive(path)
                    d["photo_path"] = path
                    photo_count += 1
                except Exception as e:
                    logger.warning(f"Photo download failed for {fid}: {e}")
                    d["photo_path"] = ""

    await query.message.reply_text(f"📸 {photo_count} photos downloaded. Building PDF...")

    # Build PDF using generate_v5_newtempl.py
    try:
        pdf_path = await _build_pdf(meta, zones, sev_counts, total, ai_texts)
    except Exception as e:
        logger.error(f"PDF generation failed: {e}")
        # Save data to Supabase anyway
        update_inspection(inspection_id, status="complete")

        await query.message.reply_text(
            f"❌ PDF generation failed:\n\n{e}\n\n"
            f"Your data is safe in Supabase.\n"
            f"Run locally: <code>python3 generate_from_supabase.py {meta.get('unit', '?')}</code>",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    # Mark complete
    update_inspection(inspection_id, status="complete")

    # Send PDF to ALL members
    members = get_members(inspection_id)
    summary = (
        f"✅ <b>Inspection Report Complete!</b>\n\n"
        f"🏗 {meta.get('project', '?')} — Unit {meta.get('unit', '?')}\n"
        f"📅 {meta.get('date', '?')}\n"
        f"📊 {sev_counts['critical']} critical | {sev_counts['medium']} medium | {sev_counts['minor']} minor\n"
        f"📸 {photo_count} photos | {len(zones)} zones"
    )

    for member in members:
        try:
            chat_id = int(member["user_id"])
            await context.bot.send_document(
                chat_id=chat_id,
                document=open(pdf_path, "rb"),
                filename=os.path.basename(pdf_path),
                caption=summary,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Failed to send PDF to {member['user_id']}: {e}")

    await query.message.reply_text(
        f"{summary}\n\n📄 PDF sent to {len(members)} team member(s).",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def _build_pdf(meta: dict, zones: list, sev_counts: dict, total: int, ai_texts: dict) -> str:
    """Build PDF using generate_v5_newtempl.py template injection."""

    # Build areas list
    areas_list = []
    for z in zones:
        defects = []
        for d in (z.get("defects") or []):
            if d.get("severity") == "compliant":
                continue
            defects.append({
                "severity": d.get("severity", "minor"),
                "description": trunc(d.get("description", ""), 80),
                "photo": d.get("photo_path", ""),
            })

        # Build observation for zone
        n = len(defects)
        if n > 0:
            items = ", ".join(d["description"] for d in defects[:5])
            obs = f"The {z['name']} area has {n} comments noted. Comments include {items}. Mentioned comments should be rectified prior to handover."
        else:
            obs = f"Overall, the {z['name']} is in good condition. No comments were noted during inspection."

        areas_list.append({
            "num": str(z["zone_number"]),
            "name": z["name"],
            "defects": defects,
            "obs": obs,
        })

    # Texts
    summary_obs = ai_texts.get("summary_obs") or (
        f"Overall, the unit is in acceptable condition. A total of {total} comments were identified. "
        f"Mentioned comments should be rectified prior to {meta.get('reason', 'handover')}."
    )
    general_cond = ai_texts.get("general_condition") or (
        f"The property at {meta.get('address', '')} is in acceptable condition. "
        f"The inspection identified {total} comments across {len(zones)} zones."
    )
    urgent = ai_texts.get("urgent") or "No critical items identified."

    # Clean texts
    summary_obs = trunc(summary_obs, 500)
    general_cond = trunc(general_cond, 500)
    urgent = trunc(urgent, 500)

    def to_py(obj):
        """Convert Python object to Python literal string, ASCII-safe."""
        s = json.dumps(obj, indent=4, ensure_ascii=True)
        s = s.replace(": null", ": None").replace("null,", "None,")
        s = s.replace(": true", ": True").replace(": false", ": False")
        return s

    clean_meta = {}
    for k, v in meta.items():
        clean_meta[k] = trunc(str(v), 120)

    data_block = (
        "\n# " + "=" * 79 + "\n"
        "# REPORT DATA\n"
        "# " + "=" * 79 + "\n"
        "DATA = " + to_py(clean_meta) + "\n\n"
        "TOTALS = " + to_py({"critical": sev_counts["critical"], "medium": sev_counts["medium"],
                             "minor": sev_counts["minor"], "total": total,
                             "compliant": sev_counts.get("compliant", 0)}) + "\n\n"
        "SUMMARY_OBS = " + json.dumps(summary_obs, ensure_ascii=True) + "\n\n"
        "AREAS = " + to_py(areas_list) + "\n\n"
        "MEP_AREAS = []\n\n"
        "GENERAL_COND = " + json.dumps(general_cond, ensure_ascii=True) + "\n\n"
        "URGENT = " + json.dumps(urgent, ensure_ascii=True) + "\n"
    )

    # Read template
    gen_path = os.path.join(ASSETS_DIR, "generate_v5_newtempl.py")
    if not os.path.exists(gen_path):
        gen_path = os.path.join(REPORT_DIR, "generate_v5_newtempl.py")
    if not os.path.exists(gen_path):
        gen_path = "/app/generate_v5_newtempl.py"

    with open(gen_path, "r") as f:
        template = f.read()

    # Inject data section using lambda to avoid \u issues
    new_script = re.sub(
        r'# [═=]+\n# REPORT DATA.*?# [═=]+\n.*?(?=\n# [═=]+\n# BUILD PDF|\nclass\b|\ndef\b)',
        lambda m: data_block,
        template,
        count=1,
        flags=re.DOTALL,
    )

    # Write and execute
    unit_s = re.sub(r'[^\w\-]', '_', meta.get("unit", "unknown"))
    date_s = meta.get("date", "nodate").replace(".", "-")
    proj_s = re.sub(r'[^\w\-]', '_', meta.get("project", "Report"))[:20]
    out_pdf = os.path.join(REPORT_DIR, f"Report_{proj_s}_{unit_s}_{date_s}.pdf")
    tmp_py = os.path.join(REPORT_DIR, "_generate_tmp.py")

    # Set output path
    new_script = re.sub(r'OUTPUT_PDF\s*=\s*.*', f'OUTPUT_PDF = r"{out_pdf}"', new_script)

    with open(tmp_py, "w", encoding="utf-8") as f:
        f.write(new_script)

    # Run
    proc = await asyncio.create_subprocess_exec(
        "python3", tmp_py,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        raise RuntimeError(f"Generator failed (exit {proc.returncode}): {err[:500]}")

    if not os.path.exists(out_pdf):
        raise RuntimeError(f"PDF not found at {out_pdf}")

    return out_pdf


# ══════════════════════════════════════════════════════════════════════════════
#  CANCEL / BACK
# ══════════════════════════════════════════════════════════════════════════════

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("🛑 Inspection cancelled. Send /start to begin again.")
    return ConversationHandler.END


async def back_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Go back to zone picker from any inspection state."""
    inspection_id = context.user_data.get("_inspection_id")
    if inspection_id:
        return await _show_zone_picker_msg(update, context, inspection_id)
    await update.message.reply_text("Nothing to go back to. Send /start to begin.")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
#  APPLICATION SETUP
# ══════════════════════════════════════════════════════════════════════════════

def build_app():
    import warnings
    from telegram.warnings import PTBUserWarning
    warnings.filterwarnings("ignore", category=PTBUserWarning)

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .get_updates_read_timeout(30)
        .get_updates_write_timeout(30)
        .get_updates_connect_timeout(15)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            START_MENU: [CallbackQueryHandler(start_menu_handler, pattern=r"^start:")],

            # Meta fields
            DATE:          [MessageHandler(filters.TEXT & ~filters.COMMAND, h_date)],
            PROJECT_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, h_project)],
            UNIT_NUMBER:   [MessageHandler(filters.TEXT & ~filters.COMMAND, h_unit)],
            PROPERTY_TYPE: [CallbackQueryHandler(h_type, pattern=r"^meta:")],
            CLIENT_NAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, h_client)],
            CLIENT_EMAIL:  [MessageHandler(filters.TEXT & ~filters.COMMAND, h_email)],
            REASON:        [MessageHandler(filters.TEXT & ~filters.COMMAND, h_reason)],
            INSPECTOR:     [MessageHandler(filters.TEXT & ~filters.COMMAND, h_inspector)],
            ADDRESS:       [MessageHandler(filters.TEXT & ~filters.COMMAND, h_address)],
            DEVELOPER:     [MessageHandler(filters.TEXT & ~filters.COMMAND, h_developer)],
            TOTAL_AREA:    [MessageHandler(filters.TEXT & ~filters.COMMAND, h_area)],
            FLOOR_NUMBER:  [MessageHandler(filters.TEXT & ~filters.COMMAND, h_floor)],
            NUM_ROOMS:     [MessageHandler(filters.TEXT & ~filters.COMMAND, h_rooms)],
            FURNISHED:     [CallbackQueryHandler(h_furnished, pattern=r"^meta:")],
            YEAR_BUILT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, h_year)],

            # Zone setup
            SETUP_ZONE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_zone_name)],
            SETUP_ZONE_TYPE: [CallbackQueryHandler(setup_zone_type, pattern=r"^ztype:")],
            SETUP_ZONES_DONE: [
                CallbackQueryHandler(setup_zones_done, pattern=r"^zones:done$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, setup_zones_add_more),
            ],

            # Join
            JOIN_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, join_code_handler)],

            # Zone picking
            PICK_ZONE: [
                CallbackQueryHandler(zone_pick_handler, pattern=r"^zone:"),
                CallbackQueryHandler(_begin_zone_pick, pattern=r"^zones:start$"),
            ],

            # Defect flow
            DEFECT_PHOTO: [
                MessageHandler(filters.PHOTO, defect_photo),
                CommandHandler("skip", skip_photo),
            ],
            AI_CONFIRM: [CallbackQueryHandler(ai_confirm_handler, pattern=r"^ai:")],
            DEFECT_SEVERITY: [CallbackQueryHandler(defect_severity, pattern=r"^sev:")],
            DEFECT_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, defect_desc_text),
                CallbackQueryHandler(defect_desc_callback, pattern=r"^usedesc:"),
            ],
            AFTER_DEFECT: [
                CallbackQueryHandler(after_defect_handler, pattern=r"^after:"),
                MessageHandler(filters.PHOTO, defect_photo),  # Quick photo = add another
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("back", back_command),
            CommandHandler("start", start),
        ],
        per_message=False,
    )

    app.add_handler(conv)
    return app


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    app = build_app()
    await app.initialize()
    await app.start()
    await app.updater.start_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )
    logger.info("Snaggit Bot v3 (multi-inspector) running.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    import time
    logger.info("Snaggit Bot v3 started.")
    while True:
        try:
            asyncio.run(main())
        except (KeyboardInterrupt, SystemExit):
            logger.info("Bot stopped.")
            break
        except Exception as e:
            logger.error(f"Bot crashed: {e}. Restarting in 5s...")
            time.sleep(5)
