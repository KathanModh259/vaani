#!/usr/bin/env python3
"""Synthetic check: behave like a caller, fail loudly if anything is off.

/health only proves the process is up. This exercises the real path - the
database, the agent, the model, the call log - by holding a short
conversation with the public demo receptionist, and fails if any step errors
or is slower than a caller will tolerate.

    python -m scripts.synthetic_check https://vaani.yourdomain.com

Exit 0 = healthy, 1 = degraded. Run it hourly (the scheduled GitHub workflow
does) and wire the failure to whoever is on call. Standard library only, so
it runs anywhere with Python.
"""

import json
import sys
import time
import urllib.error
import urllib.request

SLOW_TURN_SECONDS = 6.0   # longer than this and callers start saying "hello?"


def call(base, path, body=None, timeout=20):
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "User-Agent": "VaaniSynthetic/1.0"},
        method="POST" if body is not None else "GET",
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read() or b"{}"), time.perf_counter() - t0


def main(base: str) -> int:
    report, problems = {"target": base, "checks": []}, []

    def record(name, ok, detail, seconds=None):
        report["checks"].append({"check": name, "ok": ok, "detail": detail,
                                 "seconds": round(seconds, 2) if seconds else None})
        if not ok:
            problems.append(f"{name}: {detail}")

    for path in ("/health/ready", "/api/voice/status"):
        try:
            status, body, secs = call(base, path)
            record(path, status == 200, body, secs)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            record(path, False, str(e))

    session = None
    for line in ("Hello", "What are your opening hours?"):
        try:
            status, body, secs = call(base, "/api/chat", {"message": line, "session_id": session})
            session = body.get("session_id", session)
            reply = (body.get("response") or "").strip()
            ok = status == 200 and bool(reply) and secs <= SLOW_TURN_SECONDS
            detail = {"reply": reply[:120], "source": body.get("source")}
            if secs > SLOW_TURN_SECONDS:
                detail["slow"] = True
            record(f"chat: {line}", ok, detail, secs)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            record(f"chat: {line}", False, str(e))
            break

    # Answered, but by the deterministic path while a model was supposed to be
    # serving: the provider is down or out of credit. A deployment with no key
    # configured is expected to answer this way and must not be called broken.
    def _model_expected() -> bool:
        try:
            _, status, _ = call(base, "/api/llm/status")
            return bool(status.get("available"))
        except (urllib.error.URLError, TimeoutError, ValueError):
            return False

    if _model_expected() and any(
        c["detail"].get("source") == "rules" for c in report["checks"]
        if isinstance(c["detail"], dict) and c["check"].startswith("chat")
    ):
        problems.append("replies came from the rules fallback while a model is configured "
                        "- the LLM provider is failing")

    report["healthy"] = not problems
    print(json.dumps(report, indent=2, ensure_ascii=False))
    for p in problems:
        print("PROBLEM:", p, file=sys.stderr)
    return 0 if not problems else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
