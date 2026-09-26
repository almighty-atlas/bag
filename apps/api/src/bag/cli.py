import argparse
import getpass
import json
import secrets
import sys
import threading
from dataclasses import asdict
from datetime import timedelta
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
from bag.export import ExportError, export_bag
from bag.ids import uuid7
from bag.importer import import_bag
from bag.jobs import Scope, reprocess
from bag.maintenance import MaintenanceLoop, collect_garbage, purge_items
from bag.sessions import set_password
from bag.storage import FileSystemStorage, StorageError
from bag.tokens import TokenAdminError, create_token, list_tokens, resolve_owner, revoke_token
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


def reprocess_items(settings: Settings, scope: Scope, owner_id: UUID | None) -> dict[str, int]:
    """Schedule processing per live item in separate short transactions."""
    with connection(settings) as conn:
        owner_id = resolve_owner(conn, owner_id)
        ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM item WHERE owner_id = %s AND deleted_at IS NULL ORDER BY id",
                (owner_id,),
            ).fetchall()
        ]
    items = jobs = 0
    for item_id in ids:
        with connection(settings) as conn:
            scheduled = reprocess(conn, settings, owner_id, item_id, scope) or []
        if scheduled:
            items += 1
            jobs += len(scheduled)
    return {"items": items, "jobs": jobs}


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
    reprocess_parser = commands.add_parser("reprocess", help="Schedule processing again")
    reprocess_parser.add_argument("--owner", type=UUID, help="Required if multiple owners exist")
    reprocess_parser.add_argument(
        "--scope",
        choices=("missing", "failed", "all"),
        default="missing",
        help="Processors without a run (default), failed runs, or every processor",
    )
    purge_parser = commands.add_parser("purge", help="Hard-delete expired trashed items")
    purge_parser.add_argument("--owner", type=UUID, help="Limit to one owner")
    purge_parser.add_argument(
        "--retention-days", type=int, help="Override BAG_TRASH_RETENTION_DAYS"
    )
    purge_parser.add_argument("--dry-run", action="store_true")
    gc_parser = commands.add_parser("gc", help="Remove unreferenced storage objects")
    gc_parser.add_argument("--min-age-hours", type=float, default=1.0)
    gc_parser.add_argument("--dry-run", action="store_true")
    password_parser = commands.add_parser("password", help="Web login administration")
    password_actions = password_parser.add_subparsers(dest="action", required=True)
    set_parser = password_actions.add_parser("set", help="Set username and password")
    set_parser.add_argument("--username", required=True)
    set_parser.add_argument("--owner", type=UUID, help="Required if multiple owners exist")
    set_parser.add_argument(
        "--stdin", action="store_true", help="Read the password from standard input"
    )
    export_parser = commands.add_parser("export", help="Write originals and JSONL metadata")
    export_parser.add_argument("directory", type=Path, help="Empty or missing target directory")
    export_parser.add_argument("--owner", type=UUID, help="Required if multiple owners exist")
    import_parser = commands.add_parser("import", help="Read an export directory back in")
    import_parser.add_argument("directory", type=Path, help="Directory written by bag export")
    import_parser.add_argument("--owner", type=UUID, help="Required if multiple owners exist")
    args = parser.parse_args()
    if args.command == "password":
        try:
            if args.stdin:
                password = sys.stdin.readline().rstrip("\r\n")
            elif not sys.stdin.isatty():
                parser.exit(
                    1,
                    "No terminal to hide the password; run this in an interactive terminal "
                    "or pipe one line into `bag password set --stdin`.\n",
                )
            else:
                password = getpass.getpass("New password: ")
                if password != getpass.getpass("Repeat password: "):
                    parser.exit(1, "Passwords do not match.\n")
            set_password(Settings(), args.username, password, args.owner)
            print("Password set; existing web sessions were signed out.")
        except TokenAdminError as exc:
            parser.exit(1, f"{exc}\n")
        except psycopg.Error:
            parser.exit(1, "Database operation failed; the password was not changed.\n")
    elif args.command in ("export", "import"):
        try:
            settings = Settings()
            transfer = export_bag if args.command == "export" else import_bag
            result = transfer(
                settings, FileSystemStorage(settings.storage_path), args.directory, args.owner
            )
            print(json.dumps(result))
        except (ExportError, TokenAdminError) as exc:
            parser.exit(1, f"{exc}\n")
        except StorageError:
            parser.exit(1, f"An object failed verification; {args.command} aborted.\n")
        except psycopg.Error:
            parser.exit(1, f"Database operation failed; {args.command} aborted.\n")
    elif args.command in ("purge", "gc"):
        try:
            settings = Settings()
            if args.command == "purge":
                if args.retention_days is not None and args.retention_days < 0:
                    parser.exit(1, "Retention must not be negative.\n")
                retention = (
                    None if args.retention_days is None else timedelta(days=args.retention_days)
                )
                result = purge_items(
                    settings, retention=retention, owner_id=args.owner, dry_run=args.dry_run
                )
            else:
                if args.min_age_hours < 0:
                    parser.exit(1, "Minimum age must not be negative.\n")
                result = collect_garbage(
                    settings,
                    FileSystemStorage(settings.storage_path),
                    min_age=timedelta(hours=args.min_age_hours),
                    dry_run=args.dry_run,
                )
            print(json.dumps(result))
        except StorageError:
            parser.exit(1, "Storage operation failed; rerun after checking the volume.\n")
        except psycopg.Error:
            parser.exit(1, "Database operation failed; maintenance stopped.\n")
    elif args.command == "reprocess":
        try:
            print(json.dumps(reprocess_items(Settings(), args.scope, args.owner)))
        except TokenAdminError as exc:
            parser.exit(1, f"{exc}\n")
        except psycopg.Error:
            parser.exit(1, "Database operation failed; scheduling stopped.\n")
    elif args.command == "token":
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
    storage = FileSystemStorage(settings.storage_path)
    worker = Worker(settings, storage)
    threads = [threading.Thread(target=worker.run_forever, args=(stop,), daemon=True)]
    if settings.maintenance_interval_hours > 0:
        loop = MaintenanceLoop(settings, storage)
        threads.append(threading.Thread(target=loop.run_forever, args=(stop,), daemon=True))
    for thread in threads:
        thread.start()
    try:
        # Readiness reports the loops; the health server handles shutdown signals.
        uvicorn.run(
            create_app(
                settings,
                worker=True,
                worker_alive=lambda: all(thread.is_alive() for thread in threads),
            ),
            host="0.0.0.0",
            port=settings.worker_port,
            access_log=False,
        )
    finally:
        stop.set()
        # Let a running job record its result; the lease recovers anything cut off here.
        for thread in threads:
            thread.join(timeout=SHUTDOWN_GRACE_SECONDS)
