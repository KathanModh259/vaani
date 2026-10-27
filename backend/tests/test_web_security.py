"""Browser sessions in httpOnly cookies, CSRF defence, and the nonce CSP."""

import re

PW = "Str0ngPass!2026"
XRW = {"X-Requested-With": "fetch"}


async def test_login_sets_httponly_cookies(browser):
    r = await browser.post("/api/auth/register", json={"email": "c1@x.in", "password": PW, "full_name": "C"})
    cookies = r.headers.get_list("set-cookie")
    access = next(c for c in cookies if c.startswith("vaani_at="))
    refresh = next(c for c in cookies if c.startswith("vaani_rt="))
    for c in (access, refresh):
        assert "HttpOnly" in c and "SameSite=lax" in c
    assert "Path=/api/auth" in refresh          # the long-lived token goes nowhere else


async def test_cookie_session_works_and_writes_need_the_csrf_header(browser):
    await browser.post("/api/auth/register", json={"email": "c2@x.in", "password": PW, "full_name": "C"})
    assert (await browser.get("/api/auth/me")).status_code == 200          # reads: cookie alone

    blocked = await browser.put("/api/business", json={"about": "x"})
    assert blocked.status_code == 403                                     # write without header
    assert (await browser.put("/api/business", json={"about": "x"}, headers=XRW)).status_code == 200


async def test_bearer_clients_need_no_csrf_header(client):
    r = await client.post("/api/auth/register", json={"email": "c3@x.in", "password": PW, "full_name": "C"})
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert (await client.put("/api/business", json={"about": "x"}, headers=auth)).status_code == 200


async def test_refresh_by_cookie_and_logout(browser):
    await browser.post("/api/auth/register", json={"email": "c4@x.in", "password": PW, "full_name": "C"})
    browser.cookies.delete("vaani_at")                                    # access cookie expired
    assert (await browser.get("/api/auth/me")).status_code == 401
    assert (await browser.post("/api/auth/refresh", headers=XRW)).status_code == 200
    assert (await browser.get("/api/auth/me")).status_code == 200

    assert (await browser.post("/api/auth/logout", headers=XRW)).status_code == 204
    assert (await browser.get("/api/auth/me")).status_code == 401


async def test_chat_uses_the_cookie_session(browser):
    await browser.post("/api/auth/register", json={
        "email": "c5@x.in", "password": PW, "full_name": "C", "business_name": "Cookie Clinic"})
    r = await browser.post("/api/chat", json={"message": "hello"}, headers=XRW)
    assert "Cookie Clinic" in r.json()["response"]


async def test_pages_get_a_fresh_nonce_on_every_script(client):
    for path in ("/", "/app", "/dashboard", "/legal", "/reset-password"):
        r = await client.get(path)
        assert r.status_code == 200, path
        csp = r.headers["content-security-policy"]
        assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0], path
        nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
        tags = re.findall(r"<script\b[^>]*>", r.text, re.I)
        assert tags and all(f'nonce="{nonce}"' in t for t in tags), path
    a, b = await client.get("/"), await client.get("/")
    assert a.headers["content-security-policy"] != b.headers["content-security-policy"]


async def test_static_html_is_stamped_too(client):
    r = await client.get("/legal.html")
    nonce = re.search(r"'nonce-([^']+)'", r.headers["content-security-policy"]).group(1)
    assert f'<script nonce="{nonce}"' in r.text


async def test_legal_placeholders_filled_from_settings(client, monkeypatch):
    from scripts.config import settings
    monkeypatch.setattr(settings, "LEGAL_ENTITY_NAME", "Vaani Technologies <Pvt> Ltd")
    r = await client.get("/privacy")
    assert "Vaani Technologies &lt;Pvt&gt; Ltd" in r.text and "[LEGAL ENTITY NAME]" not in r.text
