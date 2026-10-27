"""Serving the HTML pages with a strict script policy.

The CSP allows only scripts from our own origin or carrying this request's
nonce. Every <script> tag in our pages is stamped with the nonce on the way
out, so the pages work unchanged while a script injected by an attacker
(a stored XSS in a business name, say) has no nonce and is refused by the
browser. The pages have no inline event handlers or javascript: URLs, which
is what makes this possible without 'unsafe-inline'.

The legal page's [PLACEHOLDERS] are filled from settings (LEGAL_ENTITY_NAME
etc.) so operators edit configuration, not HTML.
"""

import html
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

from fastapi import Request
from fastapi.responses import HTMLResponse
from starlette.staticfiles import StaticFiles

from scripts.config import settings

_cache: Dict[str, Tuple[float, str]] = {}
_SCRIPT_TAG = re.compile(r"<script(?=[\s>])", re.IGNORECASE)


def _read(path: Path) -> str:
    mtime = path.stat().st_mtime
    hit = _cache.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    text = path.read_text(encoding="utf-8")
    _cache[str(path)] = (mtime, text)
    return text


def nonce_of(scope_or_request) -> str:
    state = scope_or_request.scope.get("state", {}) if hasattr(scope_or_request, "scope") else scope_or_request.get("state", {})
    return state.get("csp_nonce", "")


def stamp_nonce(markup: str, nonce: str) -> str:
    return _SCRIPT_TAG.sub(f'<script nonce="{nonce}"', markup) if nonce else markup


def legal_substitutions() -> Dict[str, str]:
    pairs = {
        "[LEGAL ENTITY NAME]": settings.LEGAL_ENTITY_NAME,
        "[REGISTERED ADDRESS]": settings.LEGAL_ADDRESS,
        "[CITY]": settings.LEGAL_CITY,
        "[JURISDICTION]": settings.LEGAL_JURISDICTION,
        "[PRIVACY EMAIL]": settings.PRIVACY_EMAIL,
        "[SUPPORT EMAIL]": settings.SUPPORT_EMAIL,
        "[CLOUD HOST]": settings.CLOUD_HOST,
        "[RETENTION PERIOD]": settings.RETENTION_PERIOD_TEXT,
    }
    return {k: html.escape(v) for k, v in pairs.items() if v}


def render_page(path: Path, request: Request, headers: Optional[Dict[str, str]] = None,
                substitutions: Optional[Dict[str, str]] = None) -> HTMLResponse:
    markup = _read(path)
    for placeholder, value in (substitutions or {}).items():
        markup = markup.replace(placeholder, value)
    return HTMLResponse(stamp_nonce(markup, nonce_of(request)), headers=headers or {})


class NoncedStaticFiles(StaticFiles):
    """StaticFiles that also stamps nonces into any .html it serves."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        full, _ = self.lookup_path(path)
        if full and full.endswith(".html") and response.status_code == 200:
            markup = stamp_nonce(_read(Path(full)), nonce_of(scope))
            return HTMLResponse(markup, headers={"Cache-Control": "no-cache, must-revalidate"})
        return response
