import secrets
import time
from uuid import UUID


def uuid7() -> UUID:
    """RFC 9562 UUIDv7: 48 timestamp bits, version/variant, 74 random bits."""
    timestamp = time.time_ns() // 1_000_000
    return UUID(
        int=(timestamp << 80)
        | (7 << 76)
        | (secrets.randbits(12) << 64)
        | (2 << 62)
        | secrets.randbits(62)
    )
