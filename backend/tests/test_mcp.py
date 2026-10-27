"""The MCP server other agents call: protocol shape and real writes.

Loaded by path because mcp/ is not a Python package the backend imports - it
is a standalone entry point, which is exactly what an MCP client launches.
"""

import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

MCP_PATH = Path(__file__).resolve().parent.parent.parent / "mcp" / "vaani_mcp.py"
IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    """A fresh server process per test, pointed at its own database."""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'mcp.db').as_posix()}"
    monkeypatch.setenv("VAANI_DATABASE_URL", url)
    # The demo clinic is seeded once per process; each test has a new database.
    from services import call_logger
    monkeypatch.setattr(call_logger, "_demo_ready", False)
    spec = importlib.util.spec_from_file_location("vaani_mcp_under_test", MCP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.DATABASE_URL = url
    module.BUSINESS_ID = ""
    return module


def tool_result(response):
    return json.loads(response["result"]["content"][0]["text"])


def bookable_day(offset=2):
    d = datetime.now(IST).date() + timedelta(days=offset)
    while d.weekday() == 6:
        d += timedelta(days=1)
    return d.isoformat()


def test_initialize_and_tool_list(mcp):
    init = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["serverInfo"]["name"] == "vaani"
    assert init["result"]["capabilities"]["tools"] == {"listChanged": False}

    listing = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in listing["result"]["tools"]}
    assert {"vaani_check_availability", "vaani_book_appointment",
            "vaani_daily_digest", "vaani_get_transcript"} <= names

    assert mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "notifications/initialized"}) is None
    unknown = mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})
    assert unknown["error"]["code"] == -32601


def test_tool_errors_are_data_not_crashes(mcp):
    out = tool_result(mcp.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                  "params": {"name": "vaani_book_appointment",
                                             "arguments": {"service": "nothing"}}}))
    assert out["ok"] is False and "error" in out


def test_book_then_see_it_and_refuse_the_duplicate(mcp):
    day = bookable_day()
    first = tool_result(mcp.handle({
        "jsonrpc": "2.0", "id": 6, "method": "tools/call",
        "params": {"name": "vaani_book_appointment", "arguments": {
            "customer_name": "Alan Agent", "customer_phone": "9812345670",
            "service": "check-up", "date": day, "time": "10:00",
        }},
    }))
    assert first["ok"] is True and first["reference"]
    assert first["phone"] == "+919812345670"

    # The same slot is now closed - same conflict check as a phone call.
    second = tool_result(mcp.handle({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "vaani_book_appointment", "arguments": {
            "customer_name": "Someone Else", "customer_phone": "9812345671",
            "service": "check-up", "date": day, "time": "10:00",
        }},
    }))
    assert second["ok"] is False

    upcoming = tool_result(mcp.handle({
        "jsonrpc": "2.0", "id": 8, "method": "tools/call",
        "params": {"name": "vaani_upcoming_appointments",
                   "arguments": {"customer_phone": "9812345670"}},
    }))
    assert [a["reference"] for a in upcoming["appointments"]] == [first["reference"]]

    cancelled = tool_result(mcp.handle({
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "vaani_cancel_appointment",
                   "arguments": {"appointment_id": first["appointment_id"],
                                 "customer_phone": "9812345670"}},
    }))
    assert cancelled["ok"] is True and len(cancelled["cancelled"]) == 1


def test_availability_hides_a_booked_slot(mcp):
    day = bookable_day()
    tool_result(mcp.handle({
        "jsonrpc": "2.0", "id": 10, "method": "tools/call",
        "params": {"name": "vaani_book_appointment", "arguments": {
            "customer_name": "Meera", "customer_phone": "9812345672",
            "service": "cleaning", "date": day, "time": "11:00",
        }},
    }))
    available = tool_result(mcp.handle({
        "jsonrpc": "2.0", "id": 11, "method": "tools/call",
        "params": {"name": "vaani_check_availability",
                   "arguments": {"date": day, "service": "cleaning"}},
    }))
    assert "11:00" not in available["slots"]
    assert available["slots"][0] == "09:00"

def test_prompts_teach_the_booking_procedure(mcp):
    listing = mcp.handle({"jsonrpc": "2.0", "id": 20, "method": "prompts/list"})
    names = {p["name"] for p in listing["result"]["prompts"]}
    assert names == {"book-appointment", "clear-the-day"}

    got = mcp.handle({"jsonrpc": "2.0", "id": 21, "method": "prompts/get",
                      "params": {"name": "book-appointment",
                                 "arguments": {"request": "cleaning tomorrow at 11"}}})
    text = got["result"]["messages"][0]["content"]["text"]
    assert "cleaning tomorrow at 11" in text
    assert "vaani_check_availability" in text
    assert "Never confirm a booking unless the tool returned ok=true" in text

    missing = mcp.handle({"jsonrpc": "2.0", "id": 22, "method": "prompts/get",
                          "params": {"name": "nope"}})
    assert missing["error"]["code"] == -32602


def _book(mcp, name, phone, day, clock, service="check-up", req=30):
    return tool_result(mcp.handle({
        "jsonrpc": "2.0", "id": req, "method": "tools/call",
        "params": {"name": "vaani_book_appointment", "arguments": {
            "customer_name": name, "customer_phone": phone,
            "service": service, "date": day, "time": clock,
        }},
    }))


def _reschedule(mcp, appointment_id, day, clock, phone, req=31):
    return tool_result(mcp.handle({
        "jsonrpc": "2.0", "id": req, "method": "tools/call",
        "params": {"name": "vaani_reschedule_appointment", "arguments": {
            "appointment_id": appointment_id, "date": day, "time": clock,
            "customer_phone": phone,
        }},
    }))


def test_an_agent_can_move_an_appointment(mcp):
    day = bookable_day()
    booked = _book(mcp, "Meera", "9811111111", day, "10:00")
    assert booked["ok"] is True

    moved = _reschedule(mcp, booked["appointment_id"], day, "14:00", "9811111111")
    assert moved["ok"] is True and moved["when"].endswith("2:00 PM")

    # The old slot is free again and the new one is taken.
    free = tool_result(mcp.handle({
        "jsonrpc": "2.0", "id": 32, "method": "tools/call",
        "params": {"name": "vaani_check_availability",
                   "arguments": {"date": day, "service": "check-up"}},
    }))
    assert "10:00" in free["slots"] and "14:00" not in free["slots"]

    upcoming = tool_result(mcp.handle({
        "jsonrpc": "2.0", "id": 33, "method": "tools/call",
        "params": {"name": "vaani_upcoming_appointments",
                   "arguments": {"customer_phone": "9811111111"}},
    }))
    assert len(upcoming["appointments"]) == 1
    assert upcoming["appointments"][0]["appointment_id"] == booked["appointment_id"] or \
        upcoming["appointments"][0]["appointment_id"] == moved["appointment_id"]


def test_a_failed_move_leaves_the_original_appointment_alone(mcp):
    day = bookable_day()
    first = _book(mcp, "Asha", "9822222222", day, "10:00", req=40)
    second = _book(mcp, "Bela", "9833333333", day, "11:00", req=41)
    assert first["ok"] and second["ok"]

    # Bela cannot take Asha's slot; Bela must still be at 11:00.
    refused = _reschedule(mcp, second["appointment_id"], day, "10:00", "9833333333", req=42)
    assert refused["ok"] is False and refused["alternatives"]
    assert "still in place" in refused["note"]

    bela = tool_result(mcp.handle({
        "jsonrpc": "2.0", "id": 43, "method": "tools/call",
        "params": {"name": "vaani_upcoming_appointments",
                   "arguments": {"customer_phone": "9833333333"}},
    }))
    assert len(bela["appointments"]) == 1
    assert "11:00" in bela["appointments"][0]["when"]


def test_self_check_cli_is_honest_about_a_broken_booking(mcp, monkeypatch):
    """--check must fail, not print READY, if the slot is never consumed."""
    monkeypatch.setattr(mcp, "call_tool", lambda name, args: {
        "content": [{"type": "text", "text": json.dumps(
            {"ok": True, "reference": "X", "when": "now", "slots": ["10:00"]}
            if name == "vaani_book_appointment"
            else {"ok": True, "services": [{"name": "check-up"}]}
            if name == "vaani_business_info"
            else {"ok": True, "slots": ["10:00"]})}]
    })
    assert mcp.self_check() == 1
