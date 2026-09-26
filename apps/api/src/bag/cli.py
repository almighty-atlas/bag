import argparse
import json
import secrets
import threading
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

import psycopg
import uvicorn
from alembic import command
from alembic.config import Config

from bag.api import create_app
from bag.auth import token_hash
from bag.config import Settings
from bag.db import connection
from bag.ids import uuid7
from bag.storage import FileSystemStorage
from bag.tokens import TokenAdminError, create_token, list_tokens, revoke_token
from bag.worker import Worker

SHUTDOWN_GRACE_SECONDS = 30


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
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("migrate", "init", "worker"):
        commands.add_parser(name)
    token_parser = commands.add_parser("token", help="Server-side token administration")
    actions = token_parser.add_subparsers(dest="action", required=True)
    for action in ("list", "create", "recover", "revoke"):
        action_parser = actions.add_parser(action)
        action_parser.add_argument("--owner", type=UUID, help="Required if multiple owners exist")
        if action == "create":
            action_parser.add_argument("--name", required=True)
        elif action == "recover":
            action_parser.add_argument("--name", default="recovery")
        elif action == "revoke":
            action_parser.add_argument("token_id", type=UUID, help="Token ID from bag token list")
    args = parser.parse_args()
    if args.command == "token":
        try:
            settings = Settings()
            if args.action == "list":
                print(
                    json.dumps(
                        [asdict(row) for row in list_tokens(settings, args.owner)],
                        default=str,
                        indent=2,
                    )
                )
            elif args.action == "revoke":
                revoke_token(settings, args.token_id, args.owner)
                print("Token revoked.")
            else:
                info, secret = create_token(settings, args.name, args.owner)
                print(json.dumps({**asdict(info), "token": secret.get_secret_value()}, default=str))
        except TokenAdminError as exc:
            parser.exit(1, f"{exc}\n")
        except psycopg.Error:
            parser.exit(1, "Database operation failed; no token was displayed.\n")
    elif args.command == "migrate":
        command.upgrade(migration_config(), "head")
    elif args.command == "init":
        token = initialize(Settings())
        print(
            f"API token (shown once): {token}"
            if token
            else "Already initialized; no token changed."
        )
    else:
        run_worker(Settings())


def run_worker(settings: Settings) -> None:
    stop = threading.Event()
    worker = Worker(settings, FileSystemStorage(settings.storage_path))
    thread = threading.Thread(target=worker.run_forever, args=(stop,), daemon=True)
    thread.start()
    try:
        # Readiness reports the job loop; the health server handles shutdown signals.
        uvicorn.run(
            create_app(settings, worker=True, worker_alive=thread.is_alive),
            host="0.0.0.0",
            port=settings.worker_port,
            access_log=False,
        )
    finally:
        stop.set()
        # Let a running job record its result; the lease recovers anything cut off here.
        thread.join(timeout=SHUTDOWN_GRACE_SECONDS)
