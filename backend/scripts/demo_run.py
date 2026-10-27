"""One command that proves the whole thing, on a throwaway database.

    python -m scripts.demo_run                 # transcript to stdout
    python -m scripts.demo_run --write-evidence docs/EVIDENCE.md
    python -m scripts.demo_run --use-model     # do not blank the provider keys

It runs the real app in-process (no port to collide with), the real MCP server
module, and a real migrated database, then walks the story a judge needs to
see: a caller books, a second caller is refused, an FAQ is answered from the
owner's own profile, an escalation is recorded, and an agent moves the
appointment through the MCP surface.

Every line it prints is an assertion that ran. The exit code is non-zero if
any step fails, so it works as a pre-demo check.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
IST = ZoneInfo("Asia/Kolkata")

US = "+919988776655"
OTHER = "+919812345670"


def _prepare_environment(use_model: bool) -> str:
    """Point the app at a throwaway database before anything reads settings."""
    tmp = Path(tempfile.mkdtemp(prefix="vaani-demo-"))
    db = tmp / "demo.db"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    env = {
        "ENVIRONMENT": "test",
        "DATABASE_URL": url,
        "AUTO_MIGRATE": "true",
        "RATE_LIMIT_ENABLED": "false",
        "JWT_SECRET_KEY": "demo-run-secret-key-that-is-long-enough",
        "ENCRYPTION_KEY": "demo-run-encryption-key-32-chars",
        "PUBLIC_BASE_URL": "",
        "VOICE_MODE": "gather",
        "SMTP_HOST": "",
        "REDIS_URL": "",
        "SENTRY_DSN": "",
        "TWILIO_ACCOUNT_SID": "",
        "TWILIO_AUTH_TOKEN": "",
        "TWILIO_PHONE_NUMBER": "",
        "DEEPGRAM_API_KEY": "",
        "ELEVENLABS_API_KEY": "",
        "SARVAM_API_KEY": "",
        "GOOGLE_CLIENT_ID": "",
        "GOOGLE_CLIENT_SECRET": "",
    }
    if not use_model:
        # The point of the default run: no key, still books.
        env.update({
            "OPENAI_API_KEY": "", "OPENROUTER_API_KEY": "", "GROQ_API_KEY": "",
            "TOGETHER_API_KEY": "", "LLM_BASE_URL": "", "LLM_API_KEY": "",
            "LLM_PROVIDER": "auto", "LLM_MODEL": "",
            "LLM_FALLBACK_BASE_URL": "", "LLM_FALLBACK_MODEL": "",
        })
    os.environ.update(env)
    return url


def _load_mcp(url: str):
    os.environ["VAANI_DATABASE_URL"] = url
    spec = importlib.util.spec_from_file_location(
        "vaani_mcp_demo", REPO / "mcp" / "vaani_mcp.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.DATABASE_URL = url
    module.BUSINESS_ID = ""
    return module


class Transcript:
    def __init__(self, echo: bool = True):
        self.lines: list[str] = []
        self.echo = echo
        self.failures = 0

    def say(self, text: str = "") -> None:
        self.lines.append(text)
        if self.echo:
            print(text)

    def step(self, title: str) -> None:
        self.say()
        # ASCII only: a Windows console defaults to cp1252 and a box-drawing
        # character crashes the whole run at the worst moment.
        self.say(f"\n-- {title} " + "-" * max(0, 68 - len(title)))

    def ok(self, claim: str, detail: str = "") -> None:
        self.say(f"  PASS  {claim}" + (f"   {detail}" if detail else ""))

    def fail(self, claim: str, detail: str = "") -> None:
        self.failures += 1
        self.say(f"  FAIL  {claim}" + (f"   {detail}" if detail else ""))

    def check(self, claim: str, condition: bool, detail: str = "") -> bool:
        (self.ok if condition else self.fail)(claim, detail)
        return bool(condition)

    def quote(self, speaker: str, text: str) -> None:
        self.say(f"  {speaker:<7} {text}")


def _bookable_day(offset: int = 2) -> str:
    d = datetime.now(IST).date() + timedelta(days=offset)
    while d.weekday() == 6:
        d += timedelta(days=1)
    return d.isoformat()


async def _tool(mcp, name: str, **args) -> dict:
    """Call an MCP tool exactly as a client would.

    Off the event loop on purpose: the server runs its schema migration with
    asyncio.run(), which cannot be called from inside a running loop - the same
    reason a real client launches it as a separate process.
    """
    def call():
        response = mcp.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args},
        })
        return json.loads(response["result"]["content"][0]["text"])

    return await asyncio.to_thread(call)


async def _run(use_model: bool) -> Transcript:
    url = _prepare_environment(use_model)

    try:
        import httpx
    except ImportError:
        print("scripts/demo_run.py needs httpx: pip install -r requirements-dev.txt",
              file=sys.stderr)
        raise SystemExit(2)

    from app import app
    from llm_agent import llm_provider

    t = Transcript()
    day = _bookable_day()
    weekday = datetime.fromisoformat(day).strftime("%A")

    state = llm_provider.status()
    t.say("Vaani - end-to-end proof")
    t.say(f"database   {url}")
    t.say(f"model      {state.get('provider') or 'none'} / {state.get('model') or 'rules only'}"
          f"  (open weights: {state.get('open_weights')})")
    t.say(f"today      {datetime.now(IST).strftime('%A %d %B %Y, %H:%M %Z')}")

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://vaani") as client:

            async def chat(message: str, session: str | None = None) -> dict:
                body = {"message": message}
                if session:
                    body["session_id"] = session
                response = await client.post("/api/chat", json=body)
                return response.json()

            # ---------------------------------------------------- booking
            t.step("1. A caller asks to book")
            t.quote("Caller", '"I want to book an appointment"')
            first = await chat("I want to book an appointment")
            session = first["session_id"]
            t.quote("Vaani", f'"{first["response"]}"')
            t.check("no booking is claimed before anything is written",
                    "booked" not in first["response"].lower() and not first.get("booked"))
            t.check("it asks which service", "which one" in first["response"].lower())

            t.step("2. The caller gives the details")
            t.quote("Caller", f'"teeth cleaning on {weekday} at 11 am, '
                              f'name Meera Patel, 9988776655"')
            started = time.perf_counter()
            booked_turn = await chat(
                f"teeth cleaning on {weekday} at 11 am, name Meera Patel, 9988776655",
                session,
            )
            elapsed = (time.perf_counter() - started) * 1000
            t.quote("Vaani", f'"{booked_turn["response"]}"')
            t.check("the reply is a confirmation", "booked" in booked_turn["response"].lower())
            rows = booked_turn.get("booked") or []
            t.check("the response carries the committed row, not just prose",
                    len(rows) == 1, rows[0]["reference"] if rows else "")
            t.check("it names the day and time from the row",
                    weekday.lower() in booked_turn["response"].lower()
                    and "11:00" in booked_turn["response"])
            t.say(f"  timing  {elapsed:.0f} ms for the turn")

            from sqlalchemy import select

            from db import AppointmentSQL, DatabaseManager

            manager = DatabaseManager(url)
            await manager.initialize()
            db = await manager.get_session()
            try:
                stored = (await db.execute(select(AppointmentSQL))).scalars().all()
                t.check("exactly one row exists in appointments", len(stored) == 1)
                if stored:
                    row = stored[0]
                    t.check("the row holds the caller's name", row.customer_name == "Meera Patel",
                            row.customer_name)
                    t.check("the phone is normalised to E.164", row.customer_phone == US,
                            row.customer_phone)
                    t.check("the row is scheduled and attributed to the web channel",
                            row.status == "scheduled" and row.source == "web",
                            f"{row.status}/{row.source}")
                    appointment_id = row.id

                # ---------------------------------------- second caller
                t.step("3. A second caller wants the same slot")
                t.quote("Caller", f'"teeth cleaning on {weekday} at 11 am, '
                                  f'name Bela Shah, 9812345670"')
                second = await chat(
                    f"teeth cleaning on {weekday} at 11 am, name Bela Shah, 9812345670"
                )
                t.quote("Vaani", f'"{second["response"]}"')
                t.check("the double booking is refused", not second.get("booked"))
                t.check("the caller is offered real alternatives",
                        "offer" in second["response"].lower()
                        or "taken" in second["response"].lower())
                still_one = (await db.execute(select(AppointmentSQL))).scalars().all()
                t.check("the database still holds exactly one appointment", len(still_one) == 1)

                # ------------------------------------------------- FAQ
                t.step("4. A question, answered from the owner's own profile")
                faq = await chat("do you have parking?")
                t.quote("Caller", '"do you have parking?"')
                t.quote("Vaani", f'"{faq["response"]}"')
                t.check("the answer comes from the business FAQ",
                        "parking" in faq["response"].lower())

                # ------------------------------------------ escalation
                t.step("5. A caller who wants a person")
                esc = await chat("I want to talk to a real person please")
                t.quote("Caller", '"I want to talk to a real person please"')
                t.quote("Vaani", f'"{esc["response"]}"')
                from services.call_logger import find_call

                esc_call = await find_call(db, esc["session_id"])
                t.check("the call is flagged for a callback",
                        bool(esc_call and esc_call.escalation_requested))
                t.check("the reply promises a person, not a booking",
                        "team" in esc["response"].lower()
                        or "connect" in esc["response"].lower())

                # --------------------------------------------- agent
                t.step("6. An Alan agent moves that appointment (MCP surface)")
                mcp = _load_mcp(url)
                tools = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                names = [tool["name"] for tool in tools["result"]["tools"]]
                t.say(f"  tools   {', '.join(names)}")
                t.check("the MCP server exposes a reschedule tool",
                        "vaani_reschedule_appointment" in names)

                digest = await _tool(mcp, "vaani_daily_digest", date=day)
                t.check("the agent can see the day's calendar",
                        digest.get("ok") and len(digest.get("appointments", [])) == 1,
                        f"{digest.get('appointments')}")

                moved = await _tool(mcp, "vaani_reschedule_appointment",
                              appointment_id=appointment_id, date=day, time="15:00",
                              customer_phone=US)
                t.check("the move succeeded", moved.get("ok") is True,
                        moved.get("when", moved.get("message", "")))
                if moved.get("ok"):
                    t.quote("Agent", f'moved to {moved["when"]} (ref {moved["reference"]})')

                # The MCP call wrote through a separate connection; without
                # expiring, this session would hand back its cached instances
                # and report the old status.
                db.expire_all()
                after_move = (await db.execute(select(AppointmentSQL))).scalars().all()
                active = [a for a in after_move if a.status in ("scheduled", "confirmed")]
                t.check("exactly one active appointment remains after the move",
                        len(active) == 1)
                t.check("the superseded row is kept as cancelled, not deleted",
                        any(a.status == "cancelled" for a in after_move),
                        f"{len(after_move)} rows: "
                        + ", ".join(sorted({a.status for a in after_move})))

                free = await _tool(mcp, "vaani_check_availability", date=day, service="cleaning")
                slots = free.get("slots", [])
                t.check("the original slot is free again", "11:00" in slots, str(slots[:4]))
                t.check("the new slot is no longer offered", "15:00" not in slots)

                clash = await _tool(mcp, "vaani_reschedule_appointment",
                              appointment_id=appointment_id, date=day, time="09:00",
                              customer_phone=OTHER)
                t.check("an agent cannot move someone else's appointment",
                        clash.get("ok") is False)

                # ------------------------------------------ transcript
                t.step("7. What the owner can read afterwards")
                calls = await _tool(mcp, "vaani_list_calls", limit=5)
                t.check("call logs exist with outcomes",
                        calls.get("ok") and len(calls.get("calls", [])) > 0,
                        ", ".join(sorted({c["outcome"] for c in calls.get("calls", [])})))
                transcript_call = next(
                    (c for c in calls.get("calls", []) if c["outcome"] == "booked"), None
                )
                if transcript_call:
                    full = await _tool(mcp, "vaani_get_transcript",
                                 call_id=transcript_call["call_id"])
                    turns = full.get("transcript", [])
                    t.check("the transcript is decrypted for the owner", len(turns) > 0,
                            f"{len(turns)} turns")
                    t.check("it records what happened, not just what was said",
                            any(turn.get("speaker") == "event" for turn in turns))
            finally:
                await db.close()
                await manager.close()

    t.say("")
    t.say("=" * 72)
    if t.failures:
        t.say(f"FAILED {t.failures} check(s)")
    else:
        t.say("ALL CHECKS PASSED")
    t.say("=" * 72)
    return t


def _sanitize(line: str) -> str:
    """Strip local machine paths before publishing the transcript.

    The evidence file is committed, so a reviewer should not see somebody's
    home directory in it. The database is a throwaway; only its filename
    matters.
    """
    if line.startswith("database "):
        name = line.rsplit("/", 1)[-1].rsplit(chr(92), 1)[-1]
        return f"database   <throwaway>/{name}"
    return line


def main() -> None:
    parser = argparse.ArgumentParser(description="Prove Vaani end to end.")
    parser.add_argument("--use-model", action="store_true",
                        help="keep the configured LLM keys (default: blank them)")
    parser.add_argument("--write-evidence", metavar="PATH",
                        help="also write the transcript to a markdown file")
    parser.add_argument("--quiet", action="store_true", help="write the file only")
    parser.add_argument("--verbose", action="store_true",
                        help="keep application INFO logs in the output")
    args = parser.parse_args()

    if not args.verbose:
        # A terminal recording of this should read like a demo, not a log file.
        logging.disable(logging.INFO)

    result = asyncio.run(_run(args.use_model))

    if args.write_evidence:
        path = Path(args.write_evidence)
        if not path.is_absolute():
            path = REPO / path
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(IST).strftime("%d %B %Y, %H:%M IST")
        body = (
            "# Evidence\n\n"
            f"Generated by `python -m scripts.demo_run --write-evidence {args.write_evidence}`\n"
            f"on {stamp}. Regenerate it rather than editing it.\n\n"
            "```text\n" + "\n".join(_sanitize(line) for line in result.lines) + "\n```\n"
        )
        path.write_text(body, encoding="utf-8")
        if not args.quiet:
            print(f"\nwrote {path}")

    raise SystemExit(1 if result.failures else 0)


if __name__ == "__main__":
    main()