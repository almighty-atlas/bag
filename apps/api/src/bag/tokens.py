import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg
from pydantic import SecretStr

from bag.auth import token_hash
from bag.config import Settings
from bag.db import Row, connection
from bag.ids import uuid7


class TokenAdminError(Exception):
    pass


@dataclass(frozen=True)
class TokenInfo:
    id: UUID
    owner_id: UUID
    name: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


def resolve_owner(conn: psycopg.Connection[Row], owner_id: UUID | None) -> UUID:
    if owner_id is not None:
        row = conn.execute('SELECT id FROM "user" WHERE id = %s', (owner_id,)).fetchone()
        if row is None:
            raise TokenAdminError("Owner not found")
        return owner_id
    # Server administration may discover the sole user; never guess among multiple owners.
    rows = conn.execute('SELECT id FROM "user" ORDER BY id LIMIT 2').fetchall()
    if not rows:
        raise TokenAdminError("Not initialized; run bag init first")
    if len(rows) != 1:
        raise TokenAdminError("Multiple owners; specify --owner UUID")
    return UUID(str(rows[0]["id"]))


def create_token(
    settings: Settings,
    name: str,
    owner_id: UUID | None = None,
) -> tuple[TokenInfo, SecretStr]:
    if not name.strip() or len(name) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise TokenAdminError("Token name must be 1–200 characters without control characters")
    try:
        name.encode("utf-8")
    except UnicodeError as exc:
        raise TokenAdminError("Invalid token name") from exc
    secret = SecretStr(secrets.token_urlsafe(32))
    with connection(settings) as conn:
        owner_id = resolve_owner(conn, owner_id)
        row = conn.execute(
            "INSERT INTO api_token (id, owner_id, name, token_hash) VALUES (%s, %s, %s, %s) "
            "RETURNING id, owner_id, name, created_at, last_used_at, revoked_at",
            (uuid7(), owner_id, name, token_hash(secret.get_secret_value())),
        ).fetchone()
        assert row is not None
        info = TokenInfo(**row)
    return info, secret


def list_tokens(settings: Settings, owner_id: UUID | None = None) -> list[TokenInfo]:
    with connection(settings) as conn:
        owner_id = resolve_owner(conn, owner_id)
        rows = conn.execute(
            "SELECT id, owner_id, name, created_at, last_used_at, revoked_at FROM api_token "
            "WHERE owner_id = %s ORDER BY created_at, id",
            (owner_id,),
        ).fetchall()
    return [TokenInfo(**row) for row in rows]


def revoke_token(settings: Settings, token_id: UUID, owner_id: UUID | None = None) -> None:
    with connection(settings) as conn:
        owner_id = resolve_owner(conn, owner_id)
        row = conn.execute(
            "UPDATE api_token SET revoked_at = coalesce(revoked_at, now()) "
            "WHERE owner_id = %s AND id = %s RETURNING id",
            (owner_id, token_id),
        ).fetchone()
        if row is None:
            raise TokenAdminError("Token not found for this owner")
