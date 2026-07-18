# Missed-Call / Lead Follow-Up Bot — Setup

MVP sketch for the productized service: instant SMS follow-up + qualification
for home-service business leads, powered by Claude.

## How it works

1. A call is missed, or a lead comes in via web form.
2. Twilio/your webhook hits this app.
3. Claude drafts a warm, qualifying text (what do they need, where are they,
   is it urgent).
4. Non-emergency + qualified → app sends a booking link.
5. Emergency or high-value → app texts the business owner directly so a human
   takes over immediately.

## Requirements

- Python 3.10+
- A Twilio account with one phone number (SMS + Voice enabled)
- An Anthropic API key
- `pip install flask twilio anthropic`

## Environment variables

```
ANTHROPIC_API_KEY=sk-ant-...
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=+1XXXXXXXXXX
```

## Running it locally

```
python app.py
```

Then expose it publicly for Twilio to reach (during dev, use `ngrok http 5000`).

In your Twilio console, point:
- Voice number's "Call Status Changed" webhook → `https://<your-url>/webhook/missed-call`
- The number's "A message comes in" webhook → `https://<your-url>/webhook/sms-reply`

Point the client's website contact form (or a Zapier/Make automation on it) at:
`https://<your-url>/webhook/webform` with JSON body `{"phone": "+1...", "message": "..."}`

## What's stubbed vs. production-ready

This is a sketch to prove the concept and demo to a prospect — not
production code. Before selling this to a real client, you'd want to:

- **Persistent storage.** `CONVERSATIONS` is an in-memory dict — swap for
  Postgres/SQLite/Redis so state survives restarts and scales past one lead.
- **Real calendar booking.** Currently just sends a Calendly link. A tighter
  version would call the Google Calendar API directly to check availability
  and book the slot in the conversation.
- **Multi-tenant config.** `BUSINESS_CONFIG` is hardcoded for one business.
  For multiple clients, load config per Twilio number or per webhook path.
- **Logging/monitoring.** Add basic logging and an alert if Claude or Twilio
  calls fail, so a bad lead doesn't silently vanish.
- **Compliance.** Confirm SMS opt-in language meets TCPA requirements before
  texting leads — Twilio has guidance on this.

## Selling it

This sketch is the demo, not the pitch. Use it to show a prospect a live
example (miss a test call, watch the text arrive in seconds) rather than
describing it abstractly — that's usually what turns a cold intro into a
paid pilot.
