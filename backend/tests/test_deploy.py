"""The deployment helpers: secret handling and the post-deploy checker.

These decide whether a deploy boots and whether "it is live" is true, so they
are tested like application code, not like scripts.
"""

import json

import pytest

from scripts import init_env, preflight


# ------------------------------------------------------------------ init_env

@pytest.fixture
def env_files(tmp_path, monkeypatch):
    """Point the helper at throwaway files instead of the real .env."""
    app = tmp_path / "backend.env"
    compose = tmp_path / "compose.env"
    example = tmp_path / "app.example"
    compose_example = tmp_path / "compose.example"
    example.write_text("JWT_SECRET_KEY=\nENCRYPTION_KEY=\nPOSTGRES_PASSWORD=\n", encoding="utf-8")
    compose_example.write_text("PUBLIC_BASE_URL=https://x\nJWT_SECRET_KEY=\n", encoding="utf-8")
    monkeypatch.setattr(init_env, "APP_ENV", app)
    monkeypatch.setattr(init_env, "COMPOSE_ENV", compose)
    monkeypatch.setattr(init_env, "APP_EXAMPLE", example)
    monkeypatch.setattr(init_env, "COMPOSE_EXAMPLE", compose_example)
    return app, compose


def test_read_env_ignores_comments_and_blank_lines(tmp_path):
    path = tmp_path / "x.env"
    path.write_text("# hi\n\nA=1\nB = 2 \nnot a pair\n", encoding="utf-8")
    assert init_env.read_env(path) == {"A": "1", "B": "2"}


def test_set_values_updates_in_place_and_appends(tmp_path):
    path = tmp_path / "x.env"
    path.write_text("A=old\nB=keep\n", encoding="utf-8")
    init_env.set_values(path, {"A": "new", "C": "added"})
    assert init_env.read_env(path) == {"A": "new", "B": "keep", "C": "added"}


def test_missing_secrets_are_reported(env_files):
    app, _ = env_files
    code = init_env.report(init_env.read_env(app))
    assert code == 1


def test_weak_jwt_is_reported_but_never_used_as_is(env_files, monkeypatch):
    app, _ = env_files
    app.write_text("JWT_SECRET_KEY=short\nENCRYPTION_KEY=", encoding="utf-8")
    assert init_env.report(init_env.read_env(app)) == 1


def test_write_generates_secrets_and_syncs_both_files(env_files):
    app, compose = env_files
    assert init_env.generate(force=False) == 0

    values = init_env.read_env(app)
    assert len(values["JWT_SECRET_KEY"]) >= 32
    assert len(values["ENCRYPTION_KEY"]) >= 16
    assert len(values["POSTGRES_PASSWORD"]) >= 16

    # Compose must see the same values, or it would interpolate different ones.
    compose_values = init_env.read_env(compose)
    for key in ("JWT_SECRET_KEY", "ENCRYPTION_KEY", "POSTGRES_PASSWORD"):
        assert compose_values[key] == values[key]

    # And the report is happy afterwards.
    assert init_env.report(init_env.read_env(app)) == 0


def test_write_is_idempotent_and_keeps_existing_secrets(env_files):
    app, compose = env_files
    init_env.generate(force=False)
    first = init_env.read_env(app)
    init_env.generate(force=False)
    assert init_env.read_env(app) == first

    # A second run must not touch a key that is already strong.
    init_env.generate(force=False)
    assert init_env.read_env(compose)["ENCRYPTION_KEY"] == first["ENCRYPTION_KEY"]


def test_a_weak_jwt_secret_is_rotated_but_the_encryption_key_is_not(env_files):
    app, compose = env_files
    app.write_text(
        "JWT_SECRET_KEY=weak\nENCRYPTION_KEY=keep-this-key-32-characters-long\n"
        "POSTGRES_PASSWORD=keep-this-password\n",
        encoding="utf-8",
    )
    init_env.generate(force=False)
    values = init_env.read_env(app)
    assert values["JWT_SECRET_KEY"] != "weak" and len(values["JWT_SECRET_KEY"]) >= 32
    # Rotating either of these would destroy data, so they must survive.
    assert values["ENCRYPTION_KEY"] == "keep-this-key-32-characters-long"
    assert values["POSTGRES_PASSWORD"] == "keep-this-password"


def test_a_database_url_is_repaired_when_the_password_changes(env_files):
    app, compose = env_files
    app.write_text(
        "DATABASE_URL=postgresql+asyncpg://vaani:old@db:5432/vaani\n"
        "ENCRYPTION_KEY=keep-this-key-32-characters-long\n",
        encoding="utf-8",
    )
    init_env.generate(force=False)
    values = init_env.read_env(app)
    password = values["POSTGRES_PASSWORD"]
    assert f":{password}@" in values["DATABASE_URL"]
    assert password in init_env.read_env(compose)["DATABASE_URL"]


def test_report_flags_an_empty_secret_in_the_compose_file(env_files):
    app, compose = env_files
    init_env.generate(force=False)
    # Someone emptied one value in the root file; compose would fail to start.
    text = compose.read_text(encoding="utf-8").replace(
        next(line for line in compose.read_text(encoding="utf-8").splitlines()
             if line.startswith("POSTGRES_PASSWORD=")),
        "POSTGRES_PASSWORD=",
    )
    compose.write_text(text, encoding="utf-8")
    assert init_env.report(init_env.read_env(app)) == 1


# ---------------------------------------------------------------- preflight

class Recorder(preflight.Result):
    def __init__(self):
        super().__init__()
        self.lines = []

    def add(self, ok, name, detail=""):
        self.lines.append(("PASS" if ok else "FAIL", name))
        return super().add(ok, name, detail)

    def warn(self, ok, name, detail=""):
        self.lines.append(("WARN", name))
        return super().warn(ok, name, detail)


def test_a_warning_does_not_fail_the_run():
    r = Recorder()
    r.add(True, "health")
    r.warn(False, "twilio", "not configured")
    assert r.failures == 0
    assert ("WARN", "twilio") in r.lines


def test_a_failure_is_counted():
    r = Recorder()
    r.add(False, "health")
    assert r.failures == 1


def test_booking_day_skips_sunday():
    day = preflight._booking_day(45)
    from datetime import date
    assert date.fromisoformat(day).weekday() != 6
    assert day > date.today().isoformat()


def test_check_reports_network_failure_instead_of_raising(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(preflight, "request", boom)
    monkeypatch.setattr(preflight, "certificate_days_left", lambda host: None)
    result = preflight.check("https://example.invalid", do_book=False)
    assert result.failures >= 1


def test_check_passes_through_a_healthy_deployment(monkeypatch):
    """The happy path, with the HTTP layer stubbed: every branch must agree."""
    def fake_request(url, method="GET", body=None, headers=None):
        if url.endswith("/api/llm/status"):
            return 200, {}, json.dumps({"available": True, "provider": "openrouter",
                                        "model": "qwen/qwen3-30b-a3b-instruct-2507",
                                        "open_weights": True, "detail": None}).encode(), 5
        if url.endswith("/api/voice/status"):
            return 200, {}, json.dumps({"twilio_configured": True,
                                        "signature_validation": True,
                                        "mode": "gather"}).encode(), 5
        if url.endswith("/health/ready"):
            return 200, {}, json.dumps({"status": "ready", "database": "ok"}).encode(), 5
        if url.endswith("/health"):
            return 200, {"Strict-Transport-Security": "max-age=1",
                         "X-Content-Type-Options": "nosniff"}, b"{}", 5
        if "/api/chat" in url:
            if body and "Preflight Check" in body.get("message", ""):
                return 200, {}, json.dumps({"response": "booked",
                                            "booked": [{"reference": "ABC123"}]}).encode(), 20
            return 200, {}, json.dumps({"response": "booked", "booked": []}).encode(), 20
        return 200, {}, b"<html>vaani api/chat</html>", 5

    monkeypatch.setattr(preflight, "request", fake_request)
    monkeypatch.setattr(preflight, "certificate_days_left", lambda host: 60)
    result = preflight.check("https://vaani.example.com", do_book=True, require_phone=True)
    assert result.failures == 0, [line for line in result.rows if not line[0]]


def test_require_phone_turns_an_unconfigured_line_into_a_failure(monkeypatch):
    def fake_request(url, method="GET", body=None, headers=None):
        if url.endswith("/api/voice/status"):
            return 200, {}, json.dumps({"twilio_configured": False,
                                        "signature_validation": False}).encode(), 5
        if url.endswith("/health"):
            return 200, {"Strict-Transport-Security": "1",
                         "X-Content-Type-Options": "nosniff"}, b"{}", 5
        if url.endswith("/health/ready"):
            return 200, {}, b'{"status":"ready","database":"ok"}', 5
        if url.endswith("/api/llm/status"):
            return 200, {}, b'{"available":false,"provider":null}', 5
        return 200, {}, b"<html>vaani api/chat</html>", 5

    monkeypatch.setattr(preflight, "request", fake_request)
    monkeypatch.setattr(preflight, "certificate_days_left", lambda host: 60)
    warn_only = preflight.check("https://x.example", do_book=False, require_phone=False)
    strict = preflight.check("https://x.example", do_book=False, require_phone=True)
    assert strict.failures > warn_only.failures

# --------------------------------------------------------------- readiness

async def test_readiness_reports_the_schema_revision(client):
    """A deploy that forgot its migration must not be routed to. The probe
    returns the revision so an operator can see what is actually applied."""
    r = await client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["database"] == "ok"
    assert body["schema"], "the applied alembic revision should be reported"


async def test_readiness_fails_when_the_schema_is_missing(tmp_path, monkeypatch):
    """AUTO_MIGRATE=false plus a skipped release step leaves no tables; the
    probe must say so instead of returning ready."""
    import app as app_module
    from db import DatabaseManager

    url = f"sqlite+aiosqlite:///{(tmp_path / 'empty.db').as_posix()}"
    manager = DatabaseManager(url)
    await manager.initialize()

    async def fake_manager():
        return manager

    monkeypatch.setattr(app_module, "get_manager", fake_manager)
    try:
        # Nothing has migrated this file, so alembic_version does not exist.
        response = await app_module.readiness_check()
        assert response.status_code == 503
        data = json.loads(response.body)
        assert data["status"] == "unavailable"
        assert data["detail"] == "schema not migrated"
    finally:
        await manager.close()


# ------------------------------------------------------- production boot gate

def test_production_boot_fails_without_an_encryption_backend(monkeypatch):
    """A key with no cipher behind it looks encrypted and is not. The image
    once lacked `cryptography` and stored every transcript in plaintext."""
    from scripts.config import settings
    from services import crypto
    import app as app_module

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "DEBUG", False)
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "x" * 40)
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", "y" * 32)
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setattr(crypto, "backend_available", lambda: False)
    monkeypatch.setattr(settings, "CORS_ORIGINS", ["https://example.com"])

    with pytest.raises(RuntimeError) as error:
        app_module.check_production_readiness()
    assert "transcript encryption is unavailable" in str(error.value)


def test_production_boot_succeeds_when_everything_is_in_place(monkeypatch):
    from scripts.config import settings
    from services import crypto
    import app as app_module

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "DEBUG", False)
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "x" * 40)
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", "y" * 32)
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setattr(settings, "CORS_ORIGINS", ["https://example.com"])
    monkeypatch.setattr(crypto, "backend_available", lambda: True)
    app_module.check_production_readiness()      # must not raise


def test_crypto_backend_is_importable_in_this_environment():
    """If this fails, the installed environment cannot encrypt transcripts."""
    from services import crypto
    assert crypto.backend_available() is True
