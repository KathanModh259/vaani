"""Fill in the secrets a deployment needs, in the right files.

    python -m scripts.init_env              # report what is missing or weak
    python -m scripts.init_env --write      # generate the missing values
    python -m scripts.init_env --write --force   # rotate the JWT secret

Two files matter, and they must agree:

    backend/.env   the app's own settings (read by the app and by env_file)
    .env           compose interpolation (POSTGRES_PASSWORD, DOMAIN, ...)

Compose's `environment:` block overrides env_file, so if the JWT secret in the
root .env differs from the one in backend/.env, the deploy silently uses the
root one. Writing both from one generated value is what keeps them in sync.

Writing never replaces ENCRYPTION_KEY or POSTGRES_PASSWORD once they are set:
replacing either destroys data (unreadable transcripts, an unreachable
database). It only replaces a *weak JWT secret*, which costs a session, not a
row. --force rotates the JWT secret too.

Both files are git-ignored. The generated values are secrets.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
APP_ENV = BACKEND / ".env"
APP_EXAMPLE = BACKEND / ".env.example"
COMPOSE_ENV = REPO / ".env"
COMPOSE_EXAMPLE = REPO / "deploy" / "compose.env.example"

# key -> (generator, minimum length, note, may_be_rotated)
SECRETS = {
    "JWT_SECRET_KEY": (lambda: secrets.token_urlsafe(48), 32,
                       "signs session tokens; rotating it logs everyone out", True),
    "ENCRYPTION_KEY": (lambda: secrets.token_urlsafe(32), 16,
                       "encrypts call transcripts; losing it loses every transcript", False),
    "POSTGRES_PASSWORD": (lambda: secrets.token_urlsafe(24), 16,
                          "the database password; the app's URL must carry the same value", False),
}

REQUIRED = ("JWT_SECRET_KEY", "ENCRYPTION_KEY", "POSTGRES_PASSWORD")
PLACEHOLDERS = {"change-me-in-production", "changeme", "secret", "password", "test"}


def read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def set_values(path: Path, values: dict[str, str]) -> None:
    """Update keys in place, or append them. Everything else is preserved."""
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    for key, value in values.items():
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(f"{key}={value}", text)
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            text += f"{key}={value}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)          # secrets file, owner-only where supported
    except OSError:
        pass                           # Windows and some volumes cannot


def assess(app: dict[str, str]) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    """(missing, problems) for the app's own env."""
    missing, problems = [], []
    for key, (_, minimum, note, _) in SECRETS.items():
        value = app.get(key, "")
        if not value:
            missing.append((key, note))
        elif value.lower() in PLACEHOLDERS:
            problems.append((key, note, "placeholder value"))
        elif len(value) < minimum:
            problems.append((key, note, f"{len(value)} chars, want {minimum}+"))

    url = app.get("DATABASE_URL", "")
    password = app.get("POSTGRES_PASSWORD", "")
    if url.startswith(("postgresql", "postgres://")) and password and password not in url:
        problems.append(("DATABASE_URL", "the app would connect with a different password",
                         "does not contain POSTGRES_PASSWORD"))
    return missing, problems


def report(env: dict[str, str]) -> int:
    missing, problems = assess(env)
    compose = read_env(COMPOSE_ENV)
    compose_keys = ("JWT_SECRET_KEY", "ENCRYPTION_KEY", "POSTGRES_PASSWORD")
    # The root file only exists for compose. If it exists, it must carry the
    # secrets compose interpolates - an empty value there is a failed deploy,
    # not a missing nicety.
    out_of_sync = [
        key for key in compose_keys
        if COMPOSE_ENV.is_file() and not compose.get(key)
    ]
    out_of_sync += [
        key for key in compose_keys
        if env.get(key) and compose.get(key) and env[key] != compose[key]
    ]

    if missing:
        print("Missing:")
        for key, note in missing:
            print(f"  {key:<20} {note}")
    if problems:
        print("Weak or inconsistent:")
        for key, note, why in problems:
            print(f"  {key:<20} {why} - {note}")
    if out_of_sync:
        print("Out of sync between backend/.env and ./.env (compose would win):")
        for key in out_of_sync:
            print(f"  {key}")

    if not (missing or problems or out_of_sync):
        print("Both env files look deployable.")
        return 0
    print("\nRun: python -m scripts.init_env --write")
    return 1


def generate(force: bool) -> int:
    app = read_env(APP_ENV)
    compose = read_env(COMPOSE_ENV)
    merged = {**compose, **app}          # the app's value is the one to keep

    values: dict[str, str] = {}
    for key, (make, minimum, _, rotatable) in SECRETS.items():
        current = merged.get(key, "")
        weak = bool(current) and (len(current) < minimum or current.lower() in PLACEHOLDERS)
        if not current or (weak and (rotatable or force)) or (force and rotatable):
            values[key] = make()

    if not values:
        print("Nothing to generate - every secret is already set and strong.")
    else:
        for key in values:
            print(f"generated {key}")

    # Create the two files from their examples the first time, so every other
    # setting is documented inline instead of missing.
    if not COMPOSE_ENV.is_file() and COMPOSE_EXAMPLE.is_file():
        COMPOSE_ENV.write_text(COMPOSE_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        try:
            shown = COMPOSE_EXAMPLE.relative_to(REPO)
        except ValueError:
            shown = COMPOSE_EXAMPLE
        print(f"created {COMPOSE_ENV} from {shown}")
    if not APP_ENV.is_file() and APP_EXAMPLE.is_file():
        APP_ENV.write_text(APP_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"created {APP_ENV} from {APP_EXAMPLE.name}")

    # The app's file is the source of truth; generate() may have refreshed
    # some of it. Everything compose interpolates is copied across, so the two
    # files cannot disagree.
    app_now = read_env(APP_ENV)
    app_now.update(values)
    compose_values = {
        key: app_now[key]
        for key in ("POSTGRES_PASSWORD", "JWT_SECRET_KEY", "ENCRYPTION_KEY")
        if app_now.get(key)
    }

    if values:
        before = read_env(APP_ENV)
        set_values(APP_ENV, values)                 # app: the full set
        after = read_env(APP_ENV)
        # Keep the app's DATABASE_URL usable with the new password, if it is
        # already a Postgres URL and the password is the only thing that changed.
        if "POSTGRES_PASSWORD" in values:
            url = after.get("DATABASE_URL", "")
            old = before.get("DATABASE_URL", "")
            if url.startswith(("postgresql", "postgres://")) and old:
                match = re.match(r"(postgres(?:ql)?(?:\+asyncpg)?://[^:]+:)([^@]*)(@.*)", url)
                if match:
                    fixed = f"{match.group(1)}{values['POSTGRES_PASSWORD']}{match.group(3)}"
                    set_values(APP_ENV, {"DATABASE_URL": fixed})
                    set_values(COMPOSE_ENV, {"DATABASE_URL": fixed})

    # Always sync, even when nothing was generated: the two files may already
    # disagree because someone edited one of them by hand.
    if compose_values:
        set_values(COMPOSE_ENV, compose_values)
        print(f"synced {len(compose_values)} secret(s) into {COMPOSE_ENV.name} for compose")

    print("\nNext:")
    print("  1. edit PUBLIC_BASE_URL, DOMAIN and CORS_ORIGINS in ./.env")
    print("  2. docker compose up -d --build")
    print("  3. python -m scripts.preflight https://your.domain")
    print("\nBefore a fresh deploy you can run: python -m scripts.init_env --write")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Check or generate deployment secrets.")
    parser.add_argument("--write", action="store_true", help="generate the missing secrets")
    parser.add_argument("--force", action="store_true",
                        help="also rotate the JWT secret (ends existing sessions)")
    args = parser.parse_args()

    if not args.write:
        raise SystemExit(report(read_env(APP_ENV)))
    raise SystemExit(generate(args.force))


if __name__ == "__main__":
    main()