"""Draft a business profile from its website, so setup takes minutes.

Fetches one page the owner names, reduces it to text, and asks the LLM for
hours, services, prices and FAQs *stated on the page*. The result is a draft
for the owner to review, never applied automatically.

SSRF: this fetches a URL chosen by a user, from inside our network. The
connector's resolver refuses any address that is not publicly routable, so
"http://169.254.169.254/" (cloud metadata), "http://localhost:5432" or a
hostname that resolves to 10.x all fail. Checking at resolve time (not before
the request) also defeats DNS rebinding, where a name resolves to a public IP
for the check and a private one for the fetch.
"""

import ipaddress
import json
import logging
import re
import socket
from html.parser import HTMLParser
from typing import Any, Dict, List
from urllib.parse import urljoin, urlparse

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver

logger = logging.getLogger(__name__)

MAX_BYTES = 1_000_000
MAX_TEXT = 12_000
MAX_REDIRECTS = 3
TIMEOUT = aiohttp.ClientTimeout(total=12)


class ImportError_(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _public(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


class PublicOnlyResolver(AbstractResolver):
    def __init__(self):
        self._inner = DefaultResolver()

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        results = await self._inner.resolve(host, port, family)
        safe = [r for r in results if _public(r["host"])]
        if not safe:
            raise OSError(f"{host} does not resolve to a public address")
        return safe

    async def close(self):
        await self._inner.close()


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__()
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in ("br", "p", "li", "tr", "h1", "h2", "h3", "h4", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.parts))
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()[:MAX_TEXT]


def _normalise_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ImportError_("Enter a website address like example.com")
    if parsed.port not in (None, 80, 443):
        raise ImportError_("Only standard web ports are allowed")
    # aiohttp skips the resolver for IP-literal hosts, so PublicOnlyResolver
    # never sees "http://169.254.169.254/" - check literals here.
    try:
        literal = ipaddress.ip_address(parsed.hostname.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ImportError_("That address is not a public website")
    return url


async def fetch_text(url: str) -> str:
    url = _normalise_url(url)
    connector = aiohttp.TCPConnector(resolver=PublicOnlyResolver(), ttl_dns_cache=0)
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT, connector=connector,
                                         headers={"User-Agent": "VaaniSetupBot/1.0"}) as http:
            for _ in range(MAX_REDIRECTS + 1):
                async with http.get(url, allow_redirects=False) as r:
                    if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                        # Every hop goes back through the same resolver check.
                        url = _normalise_url(urljoin(url, r.headers["Location"]))
                        continue
                    if r.status != 200:
                        raise ImportError_(f"The website answered {r.status}", status=422)
                    if "html" not in r.headers.get("Content-Type", "text/html"):
                        raise ImportError_("That address is not a web page", status=422)
                    raw = await r.content.read(MAX_BYTES)
                    return html_to_text(raw.decode(r.charset or "utf-8", "replace"))
            raise ImportError_("Too many redirects", status=422)
    except ImportError_:
        raise
    except (aiohttp.ClientError, OSError, TimeoutError) as e:
        logger.info("Website import fetch failed for %s: %s", url, e)
        raise ImportError_("Could not open that website. Check the address and try again.", status=422)


EXTRACT_PROMPT = """You extract facts about a business from its website text for a phone receptionist.
Return JSON with exactly these keys:
  "about": one or two sentences describing the business, or "".
  "phone": the main phone number as written, or "".
  "hours": list of {"day_of_week": 0-6 (Monday=0), "is_open": bool, "opens": "HH:MM", "closes": "HH:MM"} - only days the page states.
  "services": list of {"name", "duration_minutes" (int or null), "price_rupees" (number or null), "description"}.
  "faqs": list of {"question", "answer"} - questions callers commonly ask, answered ONLY from the page (payment, parking, location, policies).
Use only what the text says. Never guess a price, duration or hour. Omit anything not stated."""


def _clean(data: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only well-formed items: the model output is untrusted input."""
    hhmm = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
    hours = []
    for h in data.get("hours") or []:
        try:
            d = int(h.get("day_of_week"))
        except (TypeError, ValueError):
            continue
        if not 0 <= d <= 6 or any(x["day_of_week"] == d for x in hours):
            continue
        is_open = bool(h.get("is_open"))
        o, c = str(h.get("opens") or ""), str(h.get("closes") or "")
        if is_open and not (hhmm.match(o) and hhmm.match(c) and o < c):
            continue
        hours.append({"day_of_week": d, "is_open": is_open, "opens": o if is_open else None,
                      "closes": c if is_open else None})
    services = []
    for s in (data.get("services") or [])[:30]:
        name = str(s.get("name") or "").strip()[:120]
        if not name:
            continue
        dur = s.get("duration_minutes")
        price = s.get("price_rupees")
        services.append({
            "name": name,
            "duration_minutes": int(dur) if isinstance(dur, (int, float)) and 5 <= dur <= 480 else None,
            "price": int(round(float(price) * 100)) if isinstance(price, (int, float)) and price >= 0 else None,
            "description": str(s.get("description") or "").strip()[:500] or None,
        })
    faqs = [{"question": str(f.get("question", "")).strip()[:300], "answer": str(f.get("answer", "")).strip()[:1000]}
            for f in (data.get("faqs") or [])[:20] if f.get("question") and f.get("answer")]
    return {
        "about": str(data.get("about") or "").strip()[:2000],
        "phone": str(data.get("phone") or "").strip()[:32],
        "hours": sorted(hours, key=lambda h: h["day_of_week"]),
        "services": services,
        "faqs": faqs,
    }


async def draft_from_website(url: str) -> Dict[str, Any]:
    from llm_agent import llm_provider

    if not llm_provider.is_configured():
        raise ImportError_("Website import needs the AI model configured (OPENAI_API_KEY).", status=503)
    text = await fetch_text(url)
    if len(text) < 40:
        raise ImportError_("That page has almost no text to read. Try your About or Services page.", status=422)
    raw = await llm_provider.complete_json(EXTRACT_PROMPT, f"Website text:\n{text}")
    if raw is None:
        raise ImportError_("The AI model is unavailable right now. Try again shortly.", status=503)
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        raise ImportError_("Could not read the website clearly. Try a different page.", status=422)
    return {"source": url, **_clean(data if isinstance(data, dict) else {})}
