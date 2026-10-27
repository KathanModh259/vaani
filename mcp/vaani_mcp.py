#!/usr/bin/env python3
"""Vaani MCP server - the receptionist's calendar as tools any agent can call.

Alan agents (and Claude, Cursor, or any MCP client) get the same booking engine
the phone line uses: read availability, book a real slot, cancel one, read the
day's calls. Nothing is simulated. Every write goes through
`services.booking`, so the agent is bound by the same conflict check, the same
opening hours and the same row lock as a phone caller.

Dependency-free on purpose: MCP over stdio is newline-delimited JSON-RPC, so
this file needs only the standard library plus the backend's existing deps.

    python mcp/vaani_mcp.py

Point an MCP client at it. Example `.mcp.json`:

    {"mcpServers": {"vaani": {"command": "python",
      "args": ["mcp/vaani_mcp.py"],
      "env": {"VAANI_DATABASE_URL": "sqlite+aiosqlite:///phone_receptionist.db"}}}}
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))
# The backend loads .env from the working directory, so run from there.
os.chdir(BACKEND)


def _database_url() -> str:
    """Resolve the DB URL, making a relative SQLite file absolute.

    A relative path would otherwise be interpreted against the backend
    directory (the process cwd), which surprises anyone passing a path from
    somewhere else.
    """
    raw = os.getenv("VAANI_DATABASE_URL")
    if not raw:
        return f"sqlite+aiosqlite:///{(BACKEND / 'phone_receptionist.db').as_posix()}"
    if raw.startswith("sqlite") and ":///" in raw:
        prefix, _, target = raw.partition(":///")
        if target and target != ":memory:" and not Path(target).is_absolute():
            return f"{prefix}:///{(BACKEND / target).as_posix()}"
    return raw


DATABASE_URL = _database_url()
BUSINESS_ID = os.getenv("VAANI_BUSINESS_ID", "")

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "vaani", "version": "1.0.0"}

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "vaani_check_availability",
        "description": (
            "List free appointment start times for a date. Always call this before "
            "offering a time to anyone. Accepts 'today' and 'tomorrow'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD, 'today' or 'tomorrow'"},
                "service": {"type": "string", "description": "Service name; optional when the business has one"},
            },
            "required": ["date"],
        },
    },
    {
        "name": "vaani_book_appointment",
        "description": (
            "Book a real appointment. Returns the stored row, or a refusal with the "
            "reason and the nearest alternatives. Never tell a caller it is booked "
            "unless this returns ok=true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string"},
                "customer_phone": {"type": "string", "description": "Any format; stored as E.164"},
                "service": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "time": {"type": "string", "description": "24-hour HH:MM"},
                "notes": {"type": "string"},
            },
            "required": ["customer_name", "customer_phone", "service", "date", "time"],
        },
    },
    {
        "name": "vaani_upcoming_appointments",
        "description": "A caller's future appointments, newest first. Use before cancelling or rescheduling.",
        "inputSchema": {
            "type": "object",
            "properties": {"customer_phone": {"type": "string"}},
            "required": ["customer_phone"],
        },
    },
    {
        "name": "vaani_reschedule_appointment",
        "description": (
            "Move one appointment to a new date and time. The new slot is booked "
            "and checked first; if it is taken, the original appointment is left "
            "untouched and alternatives are returned. This is what makes an "
            "agent useful on a bad day: when a doctor is out, move everyone."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "appointment_id": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "time": {"type": "string", "description": "24-hour HH:MM"},
                "customer_phone": {"type": "string", "description": "Owner of the appointment"},
            },
            "required": ["appointment_id", "date", "time"],
        },
    },
    {
        "name": "vaani_cancel_appointment",
        "description": "Cancel one appointment by id, or every future one for a phone number.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "appointment_id": {"type": "string"},
                "customer_phone": {"type": "string"},
                "reason": {"type": "string"},
            },
        },
    },
    {
        "name": "vaani_daily_digest",
        "description": (
            "One call's worth of context for the owner: how many calls came in, how many "
            "booked, who escalated, and what is on the calendar. Accepts a date or "
            "'today'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD or 'today'"}},
        },
    },
    {
        "name": "vaani_list_calls",
        "description": "Recent calls with outcome, duration and a preview of what the caller said.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
        },
    },
    {
        "name": "vaani_get_transcript",
        "description": "Full transcript for one call, by call SID or id.",
        "inputSchema": {
            "type": "object",
            "properties": {"call_id": {"type": "string"}},
            "required": ["call_id"],
        },
    },
    {
        "name": "vaani_business_info",
        "description": "Hours, address, contact details and the service catalog.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_SCHEMA_READY = False


def _prepare_schema() -> None:
    """Run the same migrations the app runs, once per process."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    from db.migrate import upgrade_to_head

    upgrade_to_head(DATABASE_URL)
    _SCHEMA_READY = True


async def _business(session) -> str:
    """Which business this server speaks for."""
    from services.call_logger import get_or_create_demo_business

    if BUSINESS_ID:
        return BUSINESS_ID
    return await get_or_create_demo_business(session)


async def _run(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    from services import booking
    from services.business_profile import load_profile

    from db import DatabaseManager

    manager = DatabaseManager(DATABASE_URL)
    await manager.initialize()
    session = await manager.get_session()
    try:
        business_id = await _business(session)
        profile = await load_profile(session, business_id)
        if profile is None:
            return {"ok": False, "error": f"Unknown business {business_id}"}

        if name == "vaani_check_availability":
            service = profile.find_service(args.get("service")) or (
                profile.services[0] if profile.services else None
            )
            if service is None:
                return {"ok": False, "error": "This business has no bookable services"}
            day = _parse_day(args.get("date"), profile)
            slots = await booking.free_slots(session, profile, service, day, limit=12)
            return {
                "ok": True,
                "date": day.isoformat(),
                "weekday": day.strftime("%A"),
                "service": service.name,
                "slots": [s.strftime("%H:%M") for s in slots],
                "next_available": [] if slots else [
                    s.strftime("%Y-%m-%d %H:%M")
                    for s in await booking.next_available(session, profile, service, day)
                ],
            }

        if name == "vaani_book_appointment":
            service = profile.find_service(args.get("service"))
            if service is None:
                return {"ok": False, "error": "unknown_service",
                        "services": [s.name for s in profile.services]}
            day = _parse_day(args.get("date"), profile)
            slot = str(args.get("time", "")).strip()
            start = booking.parse_local(profile, day.isoformat(), slot)
            appt = await booking.book(
                session, profile, service, start,
                customer_name=str(args.get("customer_name", "")),
                customer_phone=str(args.get("customer_phone", "")),
                source="mcp",
                notes=args.get("notes"),
            )
            return {
                "ok": True,
                "appointment_id": appt.id,
                "reference": appt.id[:6].upper(),
                "service": appt.service.name,
                "when": appt.start.strftime("%A %d %B at %I:%M %p").replace(" 0", " "),
                "customer": appt.customer_name,
                "phone": appt.customer_phone,
            }

        if name == "vaani_upcoming_appointments":
            rows = await booking.upcoming_for_phone(session, profile, args.get("customer_phone"))
            names = {s.id: s.name for s in profile.services}
            return {"ok": True, "appointments": [
                {
                    "appointment_id": r.id,
                    "reference": r.id[:6].upper(),
                    "service": names.get(r.service_id, "appointment"),
                    "when": _aware(r.start_time).astimezone(profile.tz)
                    .strftime("%A %d %B at %I:%M %p").replace(" 0", " "),
                    "status": r.status,
                }
                for r in rows
            ]}

        if name == "vaani_reschedule_appointment":
            try:
                day = _parse_day(args.get("date"), profile)
                start = booking.parse_local(profile, day.isoformat(), str(args.get("time", "")))
                moved = await booking.reschedule(
                    session, profile, str(args.get("appointment_id", "")),
                    args.get("customer_phone"), start,
                )
            except booking.BookingError as exc:
                return {
                    "ok": False, "error": exc.code, "message": exc.message,
                    # The old appointment is untouched: reschedule books first.
                    "alternatives": [
                        d.strftime("%Y-%m-%d %H:%M") for d in (exc.alternatives or [])
                    ],
                    "note": "The original appointment is still in place.",
                }
            return {
                "ok": True,
                "appointment_id": moved.id,
                "reference": moved.id[:6].upper(),
                "service": moved.service.name,
                "customer": moved.customer_name,
                "when": moved.start.strftime("%A %d %B at %I:%M %p").replace(" 0", " "),
            }

        if name == "vaani_cancel_appointment":
            cancelled = []
            if args.get("appointment_id"):
                appt = await booking.cancel(
                    session, profile, str(args["appointment_id"]),
                    args.get("customer_phone"), reason=args.get("reason"),
                )
                cancelled.append(appt)
            else:
                phone = args.get("customer_phone")
                if not phone:
                    return {"ok": False, "error": "Give an appointment_id or a customer_phone"}
                for row in await booking.upcoming_for_phone(session, profile, phone):
                    cancelled.append(await booking.cancel(session, profile, row.id, phone,
                                                          reason=args.get("reason")))
            return {"ok": True, "cancelled": [
                {"appointment_id": a.id, "reference": a.id[:6].upper(), "customer": a.customer_name}
                for a in cancelled
            ]}

        if name in ("vaani_list_calls", "vaani_get_transcript", "vaani_daily_digest"):
            return await _calls(session, profile, name, args)

        if name == "vaani_business_info":
            return {
                "ok": True,
                "business": profile.name,
                "phone": profile.phone,
                "email": profile.email,
                "time_zone": profile.time_zone,
                "hours": profile.hours_summary(),
                "about": profile.about or "",
                "booking_enabled": profile.booking_enabled,
                "services": [
                    {
                        "name": s.name,
                        "duration_minutes": s.duration_minutes,
                        "price": profile.format_price(s.price) if s.price else None,
                    }
                    for s in profile.services
                ],
                "faqs": [{"question": q, "answer": a} for q, a in profile.faqs],
            }

        return {"ok": False, "error": f"Unknown tool {name}"}

    except booking.BookingError as exc:
        return {"ok": False, "error": exc.code, "message": exc.message,
                "alternatives": [d.strftime("%Y-%m-%d %H:%M") for d in (exc.alternatives or [])]}
    except Exception as exc:                       # never take the connection down
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        await session.close()
        await manager.close()


async def _calls(session, profile, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    from sqlalchemy import select

    from db import AppointmentSQL, CallLogSQL
    from services.call_logger import transcript_of

    if name == "vaani_get_transcript":
        call_id = str(args.get("call_id", ""))
        row = (await session.execute(
            select(CallLogSQL).where(
                (CallLogSQL.call_sid == call_id) | (CallLogSQL.id == call_id)
            )
        )).scalar_one_or_none()
        if row is None:
            return {"ok": False, "error": "Call not found"}
        # Transcripts are encrypted at rest; transcript_of decrypts.
        return {"ok": True, "call_id": row.call_sid or row.id, "outcome": row.outcome,
                "transcript": transcript_of(row), "metadata": row.call_metadata or {}}

    if name == "vaani_list_calls":
        limit = max(1, min(int(args.get("limit") or 10), 100))
        rows = (await session.execute(
            select(CallLogSQL).where(CallLogSQL.business_id == profile.id)
            .order_by(CallLogSQL.start_time.desc()).limit(limit)
        )).scalars().all()
        return {"ok": True, "calls": [_call_summary(row, transcript_of) for row in rows]}

    # vaani_daily_digest
    day = _parse_day(args.get("date") or "today", profile)
    lo = datetime.combine(day, datetime.min.time(), tzinfo=profile.tz).astimezone(timezone.utc)
    hi = lo + timedelta(days=1)
    calls = (await session.execute(
        select(CallLogSQL).where(
            CallLogSQL.business_id == profile.id,
            CallLogSQL.start_time >= lo,
            CallLogSQL.start_time < hi,
        ).order_by(CallLogSQL.start_time.desc())
    )).scalars().all()
    appts = (await session.execute(
        select(AppointmentSQL).where(
            AppointmentSQL.business_id == profile.id,
            AppointmentSQL.start_time >= lo,
            AppointmentSQL.start_time < hi,
        ).order_by(AppointmentSQL.start_time)
    )).scalars().all()
    from collections import Counter
    outcomes = Counter(c.outcome or "unknown" for c in calls)
    return {
        "ok": True,
        "date": day.isoformat(),
        "business": profile.name,
        "calls": len(calls),
        "outcomes": dict(outcomes),
        "escalations": sum(1 for c in calls if c.escalation_requested),
        "appointments": [
            {
                "reference": a.id[:6].upper(),
                "customer": a.customer_name,
                "at": _aware(a.start_time).astimezone(profile.tz).strftime("%H:%M"),
                "status": a.status,
            }
            for a in appts
        ],
        "needs_attention": [
            _call_summary(c, transcript_of) for c in calls if c.escalation_requested
        ],
    }


def _call_summary(row, transcript_of) -> Dict[str, Any]:
    turns = transcript_of(row)
    return {
        "call_id": row.call_sid or row.id,
        "outcome": row.outcome,
        "duration_seconds": row.duration_seconds,
        "escalated": bool(row.escalation_requested),
        "started_at": row.start_time.isoformat() if row.start_time else None,
        "preview": next(
            (t.get("text", "") for t in turns if t.get("speaker") == "caller"), ""
        )[:160],
    }


def _aware(value: Optional[datetime]) -> datetime:
    """Treat a naive column value as UTC, which is how it was written."""
    if value is None:
        return datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_day(value: Any, profile) -> date:
    """'today', 'tomorrow' or YYYY-MM-DD, in the business's time zone."""
    text = str(value or "today").strip().lower()
    today = profile.now().date()
    if text in ("today", "now", ""):
        return today
    if text in ("tomorrow", "tmrw", "tom"):
        return today + timedelta(days=1)
    return date.fromisoformat(text)


def call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        # Alembic's env.py runs its own asyncio.run(), so migrate outside the
        # loop this handler opens.
        _prepare_schema()
        result = asyncio.run(_run(name, args))
    except Exception as exc:                       # tool bugs are data, not crashes
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}]}


def handle(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = message.get("method")
    msg_id = message.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": SERVER_INFO,
            },
        }

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = message.get("params") or {}
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": call_tool(params.get("name", ""), params.get("arguments") or {}),
        }

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"prompts": PROMPTS}}

    if method == "prompts/get":
        params = message.get("params") or {}
        prompt = _prompt(params.get("name"), params.get("arguments") or {})
        if prompt is None:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32602, "message": f"Unknown prompt: {params.get('name')}"},
            }
        return {"jsonrpc": "2.0", "id": msg_id, "result": prompt}

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


# ------------------------------------------------------------------ prompts
#
# A prompt is how an MCP server teaches an agent the procedure, instead of
# hoping the agent infers it from tool descriptions. These are the two the
# receptionist actually needs: book, and survive a bad day.

PROMPTS: List[Dict[str, Any]] = [
    {
        "name": "book-appointment",
        "description": "Book a real appointment for a caller, checking availability first.",
        "arguments": [
            {"name": "request", "description": "What the caller wants, in their words", "required": True},
        ],
    },
    {
        "name": "clear-the-day",
        "description": "Move every appointment off a date (a doctor is out, a room is closed).",
        "arguments": [
            {"name": "from_date", "description": "YYYY-MM-DD to empty", "required": True},
            {"name": "to_date", "description": "YYYY-MM-DD to move them to", "required": True},
        ],
    },
]


def _prompt(name: str, args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if name == "book-appointment":
        request = str(args.get("request") or "an appointment").strip()
        text = f"""You are booking a real appointment through the Vaani MCP server.

The caller said: "{request}"

Do this, in this order:
1. vaani_business_info - learn the services and opening hours.
2. vaani_check_availability for the date and service. Only offer times that
   appear in "slots". If the caller gave no date, ask for one.
3. Collect service, date (YYYY-MM-DD), time (HH:MM, 24-hour), full name and
   phone number. Ask for whatever is missing in one short question.
4. vaani_book_appointment. It either returns ok=true with a reference, or
   ok=false with an error and alternatives.
5. If the slot went, offer the alternatives and book again only after the
   caller agrees. Never confirm a booking unless the tool returned ok=true.
6. Read back the returned "when" and "reference" exactly.

Never invent a price, a policy or a free slot. The tool result is the truth."""
        return {
            "description": "Book an appointment through Vaani",
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        }

    if name == "clear-the-day":
        from_date = str(args.get("from_date") or "today")
        to_date = str(args.get("to_date") or "tomorrow")
        text = f"""Every appointment on {from_date} has to move to {to_date}.

1. vaani_daily_digest for {from_date} lists the appointments and their
   references, but not the customer phones. For each one, ask for the phone
   if you do not already have it, then vaani_upcoming_appointments to get the
   appointment_id.
2. For each appointment, vaani_check_availability on {to_date} for the same
   service, and pick a time as close as possible to the original hour.
3. vaani_reschedule_appointment with the appointment_id, the new date and the
   new time. It books the new slot first; if the slot is taken it returns
   alternatives and leaves the original appointment untouched.
4. Report a table: customer, old time, new time, or "needs a human" when no
   slot existed. Do not claim a move that did not return ok=true.

One appointment at a time. Do not batch silently: if a step fails, say which
customer it failed for and stop moving that one."""
        return {
            "description": "Move a day of appointments through Vaani",
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        }

    return None


# ---------------------------------------------------------------- installer

CLIENT_CONFIGS = {
    "claude": Path.home() / ".claude" / "mcp.json",
    "claude-desktop": (
        Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        / "Claude" / "claude_desktop_config.json"
        if os.name == "nt" else
        Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    ),
    "cursor": Path.home() / ".cursor" / "mcp.json",
}


def server_entry() -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "command": sys.executable,
        "args": [str(Path(__file__).resolve())],
        "env": {"VAANI_DATABASE_URL": DATABASE_URL},
    }
    if BUSINESS_ID:
        entry["env"]["VAANI_BUSINESS_ID"] = BUSINESS_ID
    return entry


def config_snippet() -> Dict[str, Any]:
    return {"mcpServers": {"vaani": server_entry()}}


def install(client: str) -> int:
    """Merge the Vaani entry into a client's config without touching the rest."""
    path = CLIENT_CONFIGS.get(client)
    if path is None:
        print(f"Unknown client {client!r}. Known: {', '.join(CLIENT_CONFIGS)}", file=sys.stderr)
        return 2
    existing: Dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"{path} is not valid JSON; fix it first.", file=sys.stderr)
            return 2
    servers = existing.setdefault("mcpServers", {})
    if "vaani" in servers and servers["vaani"] == server_entry():
        print(f"Vaani is already configured in {path}")
        return 0
    servers["vaani"] = server_entry()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    print(f"Added Vaani to {path}")
    print("Restart the client to pick it up. Tools are prefixed vaani_*.")
    return 0


def self_check() -> int:
    """Prove the server works end to end, on a throwaway database.

    Protocol, schema and a real booking round trip. Nothing touches the
    configured database, so this is safe to run before a demo.
    """
    import tempfile

    global DATABASE_URL, _SCHEMA_READY
    ok = True

    init = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    print(f"protocol      {init['result']['protocolVersion']}  {init['result']['capabilities']}")
    listing = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = listing["result"]["tools"]
    print(f"tools         {len(tools)}: {', '.join(t['name'] for t in tools)}")
    for tool in tools:
        if tool["inputSchema"].get("type") != "object":
            print(f"  bad schema for {tool['name']}")
            ok = False

    prompts = handle({"jsonrpc": "2.0", "id": 3, "method": "prompts/list"})["result"]["prompts"]
    print(f"prompts       {', '.join(p['name'] for p in prompts)}")

    original_url, original_ready = DATABASE_URL, _SCHEMA_READY
    tmp = Path(tempfile.mkdtemp()) / "self_check.db"
    DATABASE_URL = f"sqlite+aiosqlite:///{tmp.as_posix()}"
    _SCHEMA_READY = False
    try:
        day = (date.today() + timedelta(days=2)).isoformat()
        info = json.loads(call_tool("vaani_business_info", {})["content"][0]["text"])
        service = info.get("services", [{}])[0].get("name", "check-up")
        booked = json.loads(call_tool("vaani_book_appointment", {
            "customer_name": "Self Check", "customer_phone": "9000000000",
            "service": service, "date": day, "time": "10:00",
        })["content"][0]["text"])
        if not booked.get("ok"):
            print(f"booking       FAILED: {booked}")
            return 1
        subsequent = json.loads(call_tool("vaani_check_availability", {
            "date": day, "service": service,
        })["content"][0]["text"])
        if "10:00" in subsequent.get("slots", []):
            print("booking       FAILED: the booked slot is still advertised")
            ok = False
        else:
            print(f"booking       ok  {booked['reference']} {booked['when']} "
                  f"({service}) - slot no longer offered")
    finally:
        DATABASE_URL, _SCHEMA_READY = original_url, original_ready

    print("\nREADY" if ok else "\nNOT READY")
    return 0 if ok else 1


def main() -> None:
    args = sys.argv[1:]
    if "--print" in args:
        print(json.dumps(config_snippet(), indent=2))
        return
    if "--check" in args:
        raise SystemExit(self_check())
    if "--install" in args:
        client = "claude"
        if "--client" in args:
            client = args[args.index("--client") + 1]
        raise SystemExit(install(client))

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()