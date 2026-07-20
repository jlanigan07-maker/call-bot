"""
Missed-Call / Lead Follow-Up Automation — MVP sketch
======================================================

Product: when a home-service business (HVAC, plumbing, electrician, etc.)
misses a call or gets a web-form lead, this instantly texts the lead back,
asks a couple of qualifying questions using Claude, and either proposes a
booking time or escalates to the business owner if it's an emergency.

Stack:
- Flask: receives webhooks (Twilio call events, web form submissions, inbound SMS replies)
- Twilio: sends/receives SMS, detects missed calls
- Claude API (Anthropic): drafts the qualifying conversation AND flags emergencies
- SQLite for conversation state (see DB_PATH below - point it at a Render
  Persistent Disk in production so history survives restarts/redeploys)

Emergency detection uses two signals, either of which can trigger escalation:
1. A plain keyword match on the lead's raw message (fast, cheap, but only
   catches phrasings you thought to list).
2. Claude's own judgment, returned as part of its structured reply — this
   catches things the keyword list misses (e.g. "carbon monoxide detector
   going off" won't match any keyword, but Claude recognizes it as urgent).

This is a working sketch, not a finished product. TODOs mark the parts you'd
flesh out per client (calendar booking, persistent storage, multi-tenant config).
"""

import os
import json
import sqlite3
from flask import Flask, request, Response
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse
import anthropic

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config — in production, load one of these per client (multi-tenant)
# ---------------------------------------------------------------------------
BUSINESS_CONFIG = {
    "name": "Jake's HVAC Repair",
    "services": "AC repair, furnace repair, installs, duct cleaning",
    "service_area": "zip codes 12345, 12346, 12347",
    "hours": "Mon-Fri 8am-6pm",
    # Deliberately specific phrases only - no generic word like "emergency"
    # or "urgent" here, since those get said in casual NEGATED sentences
    # too ("no emergency, just wondering...") and would false-page the
    # owner. Claude's own judgment (see build_system_prompt) is what catches
    # general urgency; this list is a narrow backup for specific hazard
    # phrases that are almost never said in a non-urgent context.
    "emergency_keywords": [
        "no heat", "gas smell", "smell gas", "gas leak", "smell of gas",
        "rotten egg", "flooding", "no ac", "no cooling", "burning smell",
        "sparking", "carbon monoxide",
    ],
    "owner_phone": "+4386996244",       # gets escalation texts
    "booking_link": "https://calendly.com/jakes-hvac/service-call",  # MVP stand-in for real calendar write
}

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER")  # the business's Twilio number

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ---------------------------------------------------------------------------
# Persistent storage (SQLite) - replaces the old in-memory CONVERSATIONS dict
# ---------------------------------------------------------------------------
# DB_PATH should point at a Render Persistent Disk mount path in production
# (e.g. /var/data/conversations.db) so history survives restarts/redeploys.
# Defaults to a local file for running on your own machine.
DB_PATH = os.environ.get("DB_PATH", "conversations.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_phone ON messages(phone_number)")
    conn.commit()
    conn.close()


def get_history(phone_number: str) -> list:
    """Returns this phone number's full conversation as a list of {role, content} dicts."""
    conn = get_db()
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE phone_number = ? ORDER BY id ASC",
        (phone_number,),
    ).fetchall()
    conn.close()
    return [{"role": row["role"], "content": row["content"]} for row in rows]


def append_message(phone_number: str, role: str, content: str) -> None:
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (phone_number, role, content) VALUES (?, ?, ?)",
        (phone_number, role, content),
    )
    conn.commit()
    conn.close()


init_db()  # safe to call every startup - CREATE TABLE IF NOT EXISTS is a no-op if it already exists


# ---------------------------------------------------------------------------
# Claude prompt
# ---------------------------------------------------------------------------
def build_system_prompt(cfg: dict) -> str:
    return f"""You are the first-response SMS assistant for {cfg['name']}, a home services
business offering: {cfg['services']}. Service area: {cfg['service_area']}.
Business hours: {cfg['hours']}.

A lead just missed a call or submitted a form and you are texting them back
within seconds. Your job in this conversation:

1. Warmly acknowledge them and confirm you can help.
2. Ask 1-2 short qualifying questions to figure out: what service they need,
   their address/zip (to confirm it's in the service area), and urgency.
3. If it's a normal, non-emergency request and you have enough info, offer to
   book them using this link: {cfg['booking_link']}.
4. Keep every message under 300 characters, plain conversational text, no
   markdown, no emoji spam. This is a text message, not an email.
5. Never invent pricing, availability, or promises you can't back up — if
   asked for a specific price, say a tech will confirm exact pricing on the
   call/visit.
6. If the request is for a service you don't offer (only {cfg['services']}
   are in scope), say so clearly instead of trying to book it.
7. Judge urgency internally (that's what the is_emergency field is for), but
   NEVER mention, explain, or justify that judgment to the customer in the
   reply text - not even in a parenthetical or aside. Do not compare their
   issue to what would be more serious ("not a total outage," "not a safety
   issue," "nothing urgent," "just a minor issue," etc.) - any phrase that
   downplays or categorizes how serious their problem is does not belong in
   the reply, even briefly. Just state the next step directly.
   Wrong: "Since it's just not cooling well (not a total outage or safety
   issue), you can book a service call here: [link]"
   Right: "Got it - you can book a service call here: [link]"
8. There is no separate "24/7 emergency line" or dedicated hotline - do not
   tell customers to call one, it doesn't exist. When something is urgent
   (is_emergency is true), what actually happens is our on-call tech gets
   paged immediately. Tell the customer that directly instead, e.g. "We've
   alerted our on-call tech and they'll reach out right away" - never
   instruct them to call a number that isn't real.

You must always respond by calling the send_sms_reply tool - never respond
with plain text."""


# Forcing this through tool-calling (rather than asking Claude to format its
# own text response) means the API itself guarantees the shape of every
# response - "is_emergency" and "reply" always come back as real, separate
# fields. The old approach (asking Claude to prefix its reply with
# "EMERGENCY: / REPLY:" as plain text) drifted over longer conversations:
# Claude would sometimes just reply normally and skip the format entirely,
# which silently broke parsing. Tool-calling doesn't have that failure mode.
REPLY_TOOL = {
    "name": "send_sms_reply",
    "description": "Send the next SMS reply to the lead and flag whether this is an emergency.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_emergency": {
                "type": "boolean",
                "description": (
                    "True if there is any safety risk needing immediate attention - gas leaks, "
                    "carbon monoxide, fire, flooding, no heat in freezing weather, or anything a "
                    "reasonable person would consider urgent - even if it doesn't match an obvious "
                    "keyword. When unsure, set true rather than false; missing a real emergency is "
                    "worse than a false alarm."
                ),
            },
            "reply": {
                "type": "string",
                "description": "The exact SMS text to send next.",
            },
        },
        "required": ["is_emergency", "reply"],
    },
}


def call_claude(cfg: dict, history: list) -> tuple[str, bool]:
    """
    Send conversation history to Claude and return (reply_text, claude_flagged_emergency).

    Uses tool-calling (forced via tool_choice) instead of asking Claude to
    format its own text response - see REPLY_TOOL above for why.
    """
    response = claude.messages.create(
        model="claude-sonnet-5",
        max_tokens=500,
        system=build_system_prompt(cfg),
        messages=history,
        tools=[REPLY_TOOL],
        tool_choice={"type": "tool", "name": "send_sms_reply"},
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "send_sms_reply":
            reply_text = str(block.input.get("reply", "")).strip()
            is_emergency_flag = bool(block.input.get("is_emergency", False))
            if reply_text:
                return reply_text, is_emergency_flag
            break

    # Forcing tool_choice should make this unreachable in normal operation,
    # but keep a safe fallback rather than crashing the request if the API
    # ever returns something unexpected (e.g. hit max_tokens before the tool
    # call finished). Log it so it's debuggable, same as before.
    print(f"[PARSE FAILED] No usable send_sms_reply tool call in Claude's response: {response.content!r}")
    return (
        "Thanks for reaching out — we got your message and someone from "
        "our team will follow up with you shortly."
    ), False


def is_emergency(cfg: dict, text: str) -> bool:
    """Plain keyword check — fast, cheap, but only catches phrasings you listed."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in cfg["emergency_keywords"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def send_sms(to_number: str, body: str):
    twilio_client.messages.create(
        to=to_number,
        from_=TWILIO_FROM_NUMBER,
        body=body,
    )


def escalate_to_owner(cfg: dict, lead_number: str, reason: str):
    """Text the business owner directly for emergencies or stuck conversations."""
    send_sms(
        cfg["owner_phone"],
        f"URGENT LEAD: {lead_number} — {reason}. Call them back now.",
    )


def start_or_continue_conversation(phone_number: str, inbound_text: str):
    append_message(phone_number, "user", inbound_text)
    history = get_history(phone_number)

    reply, claude_flagged_emergency = call_claude(BUSINESS_CONFIG, history)
    append_message(phone_number, "assistant", reply)

    keyword_flagged_emergency = is_emergency(BUSINESS_CONFIG, inbound_text)
    if keyword_flagged_emergency or claude_flagged_emergency:
        try:
            escalate_to_owner(BUSINESS_CONFIG, phone_number, inbound_text)
        except Exception as e:
            # A failed escalation (bad owner number, Twilio hiccup, etc.)
            # should NEVER block the customer's actual reply - that's worse
            # than a missed page. Log it loudly instead so it's not silently
            # lost, but don't let it crash this request.
            print(f"[ESCALATION FAILED] Could not text owner_phone about {phone_number}: {e}")

    return reply


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------
@app.route("/webhook/voice-incoming", methods=["POST"])
def voice_incoming():
    """
    Point this at the Twilio number's Voice "A call comes in" webhook.

    This assumes the CLIENT's real phone forwards to this Twilio number
    only when THEY don't answer (standard carrier/PBX "forward on no
    answer/busy" feature, set up on the client's own phone - see README).
    Because of that, every call that reaches this endpoint is, by
    definition, already a missed call - there's no need to Dial anyone or
    check a status; we text the caller back immediately and give them a
    short, graceful message before ending the call.

    NOTE: this is different from Twilio's "status callback" pattern (where
    Twilio itself places an outbound Dial and reports back no-answer/busy).
    That pattern would be used instead if you have Twilio calling the
    client directly rather than relying on the client's own forwarding -
    see the README for which setup applies to a given client.
    """
    caller_number = request.form.get("From")

    opening = "Hi! Sorry we missed your call — this is " + BUSINESS_CONFIG["name"] + ". What can we help with today?"
    append_message(caller_number, "assistant", opening)
    send_sms(caller_number, opening)

    twiml = VoiceResponse()
    twiml.say("Thanks for calling. We're texting you right now so we can help you faster.")
    twiml.hangup()
    return Response(str(twiml), mimetype="application/xml")


@app.route("/webhook/webform", methods=["POST"])
def web_form_lead():
    """Point your website's contact form (or a Zapier/Make step) at this endpoint."""
    data = request.get_json(force=True)
    phone_number = data["phone"]
    message = data.get("message", "New website inquiry, no details provided.")

    reply = start_or_continue_conversation(phone_number, message)
    send_sms(phone_number, reply)
    return {"status": "sent"}, 200


@app.route("/webhook/sms-reply", methods=["POST"])
def sms_reply():
    """Twilio inbound SMS webhook — continues the qualifying conversation."""
    from_number = request.form.get("From")
    body = request.form.get("Body", "")

    reply = start_or_continue_conversation(from_number, body)

    twiml = MessagingResponse()
    twiml.message(reply)
    return Response(str(twiml), mimetype="application/xml")


if __name__ == "__main__":
    app.run(port=5000, debug=True)
