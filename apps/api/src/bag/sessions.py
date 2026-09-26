import logging
import secrets
from datetime import datetime, timedelta
from uuid import UUID

import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import HashingError, InvalidHashError, VerificationError

from bag.auth import Identity, token_hash
from bag.config import Settings
from bag.db import connection
from bag.ids import uuid7
from bag.tokens import TokenAdminError, resolve_owner

logger = logging.getLogger("bag.sessions")
HASHER = PasswordHasher()  # argon2id with the library's current defaults
# Verified against on unknown usernames so timing does not reveal which names exist.
DUMMY_HASH = HASHER.hash(secrets.token_urlsafe(16))
MIN_PASSWORD = 10
MAX_PASSWORD = 1024
COOKIE_NAME = "bag_session"


class LoginError(Exception):
    pass


def valid_username(value: str) -> str:
    value = value.strip()
    if not 1 <= len(value) <= 100 or any(
        c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value
    ):
        raise TokenAdminError("Username must be 1–100 characters without spaces or controls")
    value.encode("utf-8")
    return value


def valid_password(value: str) -> str:
    if not MIN_PASSWORD <= len(value) <= MAX_PASSWORD:
        raise TokenAdminError(f"Password must be {MIN_PASSWORD}–{MAX_PASSWORD} characters")
    if "\x00" in value:
        raise TokenAdminError("Password must not contain NUL")
    return value


def set_password(settings: Settings, username: str, password: str, owner_id: UUID | None) -> UUID:
    """Set the web login of one user and sign out its existing sessions."""
    username = valid_username(username)
    password_hash = HASHER.hash(valid_password(password))
    with connection(settings) as conn:
        owner = resolve_owner(conn, owner_id)
        try:
            conn.execute(
                'UPDATE "user" SET username = %s, password_hash = %s WHERE id = %s',
                (username, password_hash, owner),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise TokenAdminError("Username already belongs to another user") from exc
        conn.execute(
            "UPDATE session SET revoked_at = now() WHERE owner_id = %s AND revoked_at IS NULL",
            (owner,),
        )
    logger.info("password_set", extra={"owner_id": owner})
    return owner


def login(settings: Settings, username: str, password: str) -> tuple[str, datetime]:
    """Verify the password and open a session; the token is returned once."""
    with connection(settings) as conn:
        row = conn.execute(
            'SELECT id, password_hash FROM "user" WHERE username = %s', (username.strip(),)
        ).fetchone()
        stored = row["password_hash"] if row is not None else None
        try:
            HASHER.verify(stored or DUMMY_HASH, password)
        except (VerificationError, InvalidHashError, HashingError) as exc:
            logger.warning("login_failed")
            raise LoginError("Invalid username or password") from exc
        if row is None or stored is None:
            logger.warning("login_failed")
            raise LoginError("Invalid username or password")
        if HASHER.check_needs_rehash(stored):
            conn.execute(
                'UPDATE "user" SET password_hash = %s WHERE id = %s',
                (HASHER.hash(password), row["id"]),
            )
        token = secrets.token_urlsafe(32)
        expires = conn.execute(
            "INSERT INTO session (id, owner_id, token_hash, expires_at) "
            "VALUES (%s, %s, %s, now() + %s) RETURNING expires_at",
            (uuid7(), row["id"], token_hash(token), timedelta(days=settings.session_days)),
        ).fetchone()
        assert expires is not None
    logger.info("session_created", extra={"owner_id": row["id"]})
    return token, expires["expires_at"]


def authenticate_session(settings: Settings, token: str) -> Identity | None:
    if not token or len(token) > 512:
        return None
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT id, owner_id FROM session WHERE token_hash = %s AND revoked_at IS NULL "
            "AND expires_at > now()",
            (token_hash(token),),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE session SET last_seen_at = now() WHERE owner_id = %s AND id = %s",
            (row["owner_id"], row["id"]),
        )
    return Identity(owner_id=row["owner_id"], via="session")


def logout(settings: Settings, token: str) -> None:
    with connection(settings) as conn:
        conn.execute(
            "UPDATE session SET revoked_at = now() WHERE token_hash = %s AND revoked_at IS NULL",
            (token_hash(token),),
        )
