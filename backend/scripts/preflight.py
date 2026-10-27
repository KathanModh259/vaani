"""Post-deploy verification: is this deployment actually a working Vaani?

Run it against the public URL right after a deploy, and again before a demo:

    python -m scripts.preflight https://vaani.example.com
    python -m scripts.preflight https://vaani.example.com --no-book

It checks what a browser, Twilio and an Alan agent would experience from
outside: TLS and the certificate, the proxy's security headers, health and
readiness, which model answers, whether Twilio is configured, whether the
pages load, and - unless --no-book - whether a booking really commits.

The booking check is deliberately indirect: it books a slot ~45 days out, then
tries to book the *same* slot again. The second attempt can only be refused
if the first row reached the database, because the conflict check reads it
back. That proves persistence without needing an authenticated dashboard call.
The test booking is left in place and named so it is easy to spot; the
reference is printed so it can be cancelled in the dashboard.

Standard library only, so it runs from any machine with Python 3.10+.
Exit code 0 means every check passed.
"""

from __future__ import annotations

import argparse
import json
import ssl
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

TIMEOUT = 20
SLOW_TURN_MS = 6000


class Result:
    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []
        self.failures = 0

    def add(self, ok: bool, name: str, detail: str = "") -> bool:
        self.rows.append((ok, name, detail))
        if not ok:
            self.failures += 1
        mark = "PASS" if ok else "FAIL"
        print(f"  {mark}  {name:<44} {detail}")
        return ok

    def skip(self, name: str, why: str) -> None:
        self.rows.append((True, name, why))
        print(f"  SKIP  {name:<44} {why}")

    def warn(self, ok: bool, name: str, detail: str = "") -> None:
        """Not a failure: the demo works without a phone line or a model.

        Same signature as add() so the two can be chosen by configuration.
        """
        self.rows.append((True, name, detail))
        print(f"  WARN  {name:<44} {detail}")


def request(url: str, method: str = "GET", body: dict | None = None,
            headers: dict | None = None) -> tuple[int, dict, bytes, float]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", "vaani-preflight/1.0")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            elapsed = (time.perf_counter() - started) * 1000
            return response.status, dict(response.headers), response.read(), elapsed
    except urllib.error.HTTPError as error:
        elapsed = (time.perf_counter() - started) * 1000
        return error.code, dict(error.headers), error.read(), elapsed


def certificate_days_left(host: str) -> int | None:
    """Days until the TLS certificate expires, or None when it cannot be read."""
    context = ssl.create_default_context()
    try:
        with context.wrap_socket(ssl.socket.create_connection((host, 443), TIMEOUT),
                                 server_hostname=host) as tls:
            cert = tls.getpeercert()
        expiry = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
        return (expiry - datetime.utcnow()).days
    except Exception:
        return None


def _booking_day(offset: int = 45) -> str:
    d = date.today() + timedelta(days=offset)
    while d.weekday() == 6:                 # default business week: closed Sunday
        d += timedelta(days=1)
    return d.isoformat()


def check(base: str, do_book: bool, require_phone: bool = False) -> Result:
    base = base.rstrip("/")
    r = Result()
    https = base.startswith("https://")
    host = base.split("://", 1)[-1].split("/")[0].split(":")[0]

    print(f"\nVaani preflight -> {base}\n")

    print("Edge")
    if https:
        days = certificate_days_left(host)
        if days is None:
            r.add(False, "TLS certificate readable", "could not negotiate TLS")
        else:
            r.add(days > 14, "TLS certificate has >14 days left", f"{days} days")
    else:
        r.skip("TLS certificate", "plain HTTP target")

    try:
        status, headers, _, _ = request(base + "/health")
        r.add(status == 200, "/health answers 200", f"{status}")
        hsts = headers.get("Strict-Transport-Security") or headers.get("strict-transport-security")
        r.add(bool(hsts) or not https, "HSTS header set by the proxy",
              hsts or "absent (ok on http)")
        r.add((headers.get("X-Content-Type-Options") == "nosniff")
              or not https, "X-Content-Type-Options: nosniff")
        # Caddy strips the Server header. Hitting the origin directly shows
        # "uvicorn", which is expected while testing a container on localhost,
        # and a leak worth failing on for a public https deployment.
        leaks = any(v == "uvicorn" for v in headers.values())
        (r.add if https else r.warn)(
            not leaks, "proxy does not advertise the server",
            "Server: uvicorn is visible" if leaks else "")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        r.add(False, "reachable over the network", str(e))
        return r

    print("\nApplication")
    try:
        status, _, raw, _ = request(base + "/health/ready")
        body = json.loads(raw or b"{}")
        r.add(status == 200 and body.get("database") == "ok",
              "/health/ready reports database ok", f"{status} {body}")
    except Exception as e:
        r.add(False, "/health/ready", str(e))

    try:
        _, _, raw, _ = request(base + "/api/llm/status")
        state = json.loads(raw)
        configured = bool(state.get("available"))
        if configured:
            r.add(True, "model provider configured",
                  f"{state.get('provider')} / {state.get('model')}")
            r.add(bool(state.get("open_weights")), "the model's weights are open",
                  f"open_weights={state.get('open_weights')}")
        else:
            r.skip("model provider", "none configured - deterministic booking only")
        r.add("detail" not in state or state.get("detail") is None or not configured,
              "no provider error reported", str(state.get("detail") or ""))
    except Exception as e:
        r.add(False, "/api/llm/status", str(e))

    try:
        _, _, raw, _ = request(base + "/api/voice/status")
        voice = json.loads(raw)
        # The web demo and the MCP surface work with no phone line, so an
        # unconfigured Twilio is a warning - unless the deployment claims to
        # answer calls, in which case it is the whole point and must fail.
        reporter = r.add if require_phone else r.warn
        reporter(voice.get("twilio_configured") is True,
                 "Twilio configured for real calls",
                 "set TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN" if not voice.get("twilio_configured") else "")
        reporter(voice.get("signature_validation") is True,
                 "webhook signatures are enforced",
                 str(voice.get("mode")))
    except Exception as e:
        r.add(False, "/api/voice/status", str(e))

    print("\nPages")
    for path, needle in (("/", "vaani"), ("/app", "api/chat"), ("/dashboard", "vaani")):
        try:
            status, headers, raw, _ = request(base + path)
            text = raw.decode("utf-8", "ignore")
            r.add(status == 200 and needle in text.lower(),
                  f"{path} serves the app", f"{status}, {len(raw)} bytes")
        except Exception as e:
            r.add(False, f"{path}", str(e))

    print("\nBooking path")
    try:
        status, _, raw, ms = request(base + "/api/chat", "POST", {"message": "hello"})
        body = json.loads(raw or b"{}")
        r.add(status == 200 and bool(body.get("response")), "a chat turn answers",
              f"{ms:.0f} ms")
        r.add(ms < SLOW_TURN_MS, "the turn is not slow", f"{ms:.0f} ms < {SLOW_TURN_MS}")
        session = body.get("session_id")
    except Exception as e:
        r.add(False, "chat turn", str(e))
        return r

    if not do_book:
        r.skip("booking commits", "--no-book")
        return r

    stamp = datetime.now().strftime("%H%M")
    # Candidates are tried in order, so a second preflight on the same day
    # does not fail just because the first one booked 09:30, and a fully
    # booked day rolls to the next one.
    candidates = [(d, clock)
                  for d in (_booking_day(45), _booking_day(47))
                  for clock in ("09:30", "10:00", "10:30", "11:00", "14:00", "15:00")]
    booked_reference = ""
    booked_clock = ""
    day = ""
    for day, clock in candidates:
        message = (f"teeth cleaning on {day} at {clock}, "
                   f"name Preflight Check {stamp}, 9000000001")
        _, _, raw, ms = request(base + "/api/chat", "POST",
                                {"message": message, "session_id": session})
        attempt = json.loads(raw or b"{}")
        rows = attempt.get("booked") or []
        if rows:
            booked_reference = rows[0]["reference"]
            booked_clock = clock
            r.add(True, "a booking is written and confirmed",
                  f"{clock} {day}, ref {booked_reference}, {ms:.0f} ms")
            break
        if "taken" not in (attempt.get("response") or "").lower():
            r.add(False, "a booking is written and confirmed",
                  (attempt.get("response") or "")[:80])
            break

    if not booked_reference:
        r.skip("the same slot is refused", "no free candidate slot to test with")
        return r

    print(f"        test appointment uses {booked_clock} on {day}; "
          f"cancel ref {booked_reference} when done")

    # The same slot again. A refusal can only happen if the first row
    # exists, because book() re-checks the calendar under a lock.
    _, _, raw, _ = request(base + "/api/chat", "POST", {
        "message": f"teeth cleaning on {day} at {booked_clock}, "
                   f"name Preflight Duplicate, 9000000002",
    })
    second = json.loads(raw or b"{}")
    r.add(not second.get("booked"),
          "the same slot is refused (the row persisted)",
          (second.get("response") or "")[:60])
    return r


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify a deployed Vaani from outside.")
    parser.add_argument("base_url", help="e.g. https://vaani.example.com")
    parser.add_argument("--no-book", action="store_true",
                        help="skip the booking round trip (leaves no test appointment)")
    parser.add_argument("--require-phone", action="store_true",
                        help="treat an unconfigured Twilio line as a failure")
    args = parser.parse_args()

    result = check(args.base_url, not args.no_book, args.require_phone)
    total = len(result.rows)
    print(f"\n{'ALL CHECKS PASSED' if not result.failures else f'{result.failures} FAILED'}"
          f"  ({total - result.failures}/{total})")
    raise SystemExit(1 if result.failures else 0)


if __name__ == "__main__":
    main()