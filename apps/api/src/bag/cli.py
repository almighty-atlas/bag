import argparse
import secrets
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config

from bag.api import create_app
from bag.auth import token_hash
from bag.config import Settings
from bag.db import connection
from bag.ids import uuid7


def migration_config() -> Config:
    return Config(str(Path(__file__).parent / "migrations" / "alembic.ini"))


def initialize(settings: Settings) -> str | None:
    token = secrets.token_urlsafe(32)
    with connection(settings) as conn:
        # Bootstrap is global administration; serialize first-user creation across processes.
        conn.execute("SELECT pg_advisory_xact_lock(86753109)")
        if conn.execute('SELECT id FROM "user" LIMIT 1').fetchone() is not None:
            return None
        owner_id = uuid7()
        conn.execute(
            'INSERT INTO "user" (id, display_name) VALUES (%s, %s)',
            (owner_id, settings.user_name),
        )
        conn.execute(
            "INSERT INTO api_token (id, owner_id, name, token_hash) VALUES (%s, %s, %s, %s)",
            (uuid7(), owner_id, "initial", token_hash(token)),
        )
    return token


def main() -> None:
    parser = argparse.ArgumentParser(prog="bag")
    parser.add_argument("command", choices=["migrate", "init", "worker"])
    args = parser.parse_args()
    if args.command == "migrate":
        command.upgrade(migration_config(), "head")
    elif args.command == "init":
        token = initialize(Settings())
        print(
            f"API token (shown once): {token}"
            if token
            else "Already initialized; no token changed."
        )
    else:
        settings = Settings()
        uvicorn.run(
            create_app(settings, worker=True),
            host="0.0.0.0",
            port=settings.worker_port,
            access_log=False,
        )
