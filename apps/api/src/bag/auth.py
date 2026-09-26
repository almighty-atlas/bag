import hashlib
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException

from bag.config import Settings
from bag.db import connection


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Identity:
    owner_id: UUID


def authenticate(settings: Settings, authorization: str | None) -> Identity:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 512:
        raise unauthorized()
    with connection(settings) as conn:
        # Credential lookup establishes ownership; all subsequent queries use owner_id.
        row = conn.execute(
            "SELECT id, owner_id FROM api_token WHERE token_hash = %s AND revoked_at IS NULL",
            (token_hash(token),),
        ).fetchone()
        if row is None:
            raise unauthorized()
        conn.execute(
            "UPDATE api_token SET last_used_at = now() WHERE owner_id = %s AND id = %s",
            (row["owner_id"], row["id"]),
        )
    return Identity(owner_id=row["owner_id"])


def unauthorized() -> HTTPException:
    return HTTPException(401, "Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})
