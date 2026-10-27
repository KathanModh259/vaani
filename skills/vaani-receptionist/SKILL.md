---
name: vaani-receptionist
description: Operate Vaani, the open-source AI phone receptionist, from an agent. Use when a user asks to book, move or cancel an appointment by phone, check a clinic's or salon's availability, read call transcripts, or summarise a day of calls — and Vaani's MCP server is available. Handles multi-tenant businesses, refuses double bookings, and never confirms a slot the calendar did not commit.
---

# Vaani receptionist

Vaani answers a business's phone in Hindi, Gujarati and English, books real
appointments, and files the transcript. This skill is how an agent uses its
calendar directly, without placing a call.

Everything is a real write to the same database the phone line uses. There is
no sandbox mode: a booking you make is a booking the business will see.

## Connect

Add the server to the MCP client configuration:

```json
{
  "mcpServers": {
    "vaani": {
      "command": "python",
      "args": ["mcp/vaani_mcp.py"],
      "env": {
        "VAANI_DATABASE_URL": "sqlite+aiosqlite:///phone_receptionist.db",
        "VAANI_BUSINESS_ID": ""
      }
    }
  }
}
```

- `VAANI_DATABASE_URL` — the same database the API uses. Relative SQLite paths
  resolve inside `backend/`.
- `VAANI_BUSINESS_ID` — leave empty for the built-in demo clinic, otherwise
  the business id from `GET /api/business` (or the `businesses` table).

Before using the tools, verify the server:

```bash
python mcp/vaani_mcp.py --check     # prints READY only if a real booking worked
python mcp/vaani_mcp.py --print     # copy this into the client's mcpServers
```

## Decide what to call

| The user asks | Call |
|---|---|
| "Do you have anything tomorrow?" | `vaani_check_availability` |
| "Book me in" | `vaani_check_availability` → `vaani_book_appointment` |
| "What have I got booked?" | `vaani_upcoming_appointments` |
| "Move it to Friday" | `vaani_upcoming_appointments` → `vaani_reschedule_appointment` |
| "Cancel it" | `vaani_upcoming_appointments` → `vaani_cancel_appointment` |
| "How did today go?" | `vaani_daily_digest` |
| "What did that caller say?" | `vaani_list_calls` → `vaani_get_transcript` |
| "What do you charge?" | `vaani_business_info` |

## Booking, in the only order that is safe

1. **Look before you speak.** Call `vaani_check_availability` for the date and
   service. Never offer a time that is not in the returned `slots`.
2. **Collect four fields.** Service, date (`YYYY-MM-DD`), time (`HH:MM`,
   24-hour, business-local), customer name and phone. A missing phone is a
   failed booking — `book` refuses it rather than storing a hollow row.
3. **Book.** `vaani_book_appointment` returns `ok: true` with a
   `reference`, or `ok: false` with an `error` and `alternatives`.
4. **Read back what was written.** Confirm using the returned `when` and
   `reference`. Do not invent a nicer time than the tool returned.

If the tool refuses:

| `error` | What it means | What to do |
|---|---|---|
| `taken` | Someone booked that slot first | Offer the `alternatives` |
| `closed` / `outside_hours` | The business is not open then | Use `next_available` or `alternatives` |
| `too_soon` | Inside the 30-minute notice window | Offer a later slot the same day |
| `too_far` | Beyond the booking horizon | Choose a nearer date |
| `missing_name` / `missing_phone` | Incomplete request | Ask the user, then retry |
| `disabled` | The owner takes bookings by hand | Offer a message instead |

`alternatives` are real start times, already conflict-checked.

## Rescheduling

Never cancel-then-book: a crash between the two leaves the patient with
nothing. `vaani_reschedule_appointment` books the new slot first and cancels
the old one only after the new row commits. If the new slot is taken it
returns `alternatives` and states that the original is still in place.

Check availability for the new date before offering times, exactly as when
booking. When several appointments must move — a doctor is out, a room is
closed — use the `clear-the-day` prompt and work one appointment at a time,
naming the customer whenever a step fails.

## Cancelling

Never cancel from memory. Run `vaani_upcoming_appointments` for the phone
number, then pass the exact `appointment_id` to `vaani_cancel_appointment`.
Cancelling by phone alone cancels *every* future appointment for that number —
only do that when the user said so explicitly.

## Reading calls

- `vaani_list_calls` returns outcome, duration, whether the caller asked for a
  human, and the first thing they said.
- `vaani_get_transcript` decrypts and returns the full turn-by-turn
  conversation. Transcripts contain personal data: quote only what the user
  needs, and never paste a transcript into a public channel.

Transcripts are encrypted at rest with `ENCRYPTION_KEY`; a different key
returns unreadable text rather than failing loudly.

## Reporting style

The people using this are small-business owners, not engineers. Say "Meera
Patel is booked for a cleaning on Tuesday at 11 AM, reference DD3E02", not
"appointment_id DD3E02...status=scheduled". For a day summary, lead with the
numbers that matter: calls answered, appointments booked, anyone still waiting
for a callback.

## Verify the demo works

```bash
python mcp/vaani_mcp.py        # then send initialize + tools/list over stdin
cd backend && python -m pytest tests/test_mcp.py -q
```

A booking made through the skill shows up in `GET /api/dashboard` and in the
`appointments` table immediately — that is the point of the whole project.