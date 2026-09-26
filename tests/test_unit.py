import json
import logging
import time
from uuid import RFC_4122

import pytest
from bag.api import create_app
from bag.auth import token_hash
from bag.config import Settings
from bag.ids import uuid7
from bag.logging import JsonFormatter
from bag.schemas import TextCapture
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError


def test_uuid7_layout_and_uniqueness() -> None:
    before = time.time_ns() // 1_000_000
    values = [uuid7() for _ in range(1000)]
    after = time.time_ns() // 1_000_000
    assert len(set(values)) == len(values)
    for value in values:
        assert value.version == 7 and value.variant == RFC_4122
        assert before <= value.int >> 80 <= after


@pytest.mark.parametrize("content", ["", "NUL\x00", "\ud800"])
def test_invalid_text(content: str) -> None:
    with pytest.raises(ValidationError):
        TextCapture(content=content)


def test_original_text_and_aware_capture_time() -> None:
    content = "  Überlegung 💼\n日本語\r\n<script>alert(1)</script>  "
    assert TextCapture(content=content).content == content
    with pytest.raises(ValidationError):
        TextCapture.model_validate({"content": "hi", "captured_at": "2026-09-26T12:00:00"})


def test_logs_only_allowlisted_fields() -> None:
    record = logging.LogRecord("bag", logging.INFO, "", 0, "capture_stored", (), None)
    record.item_id = "item-id"
    record.content = "private"
    record.token = "secret"
    value = json.loads(JsonFormatter().format(record))
    assert value == {"level": "INFO", "event": "capture_stored", "item_id": "item-id"}
    assert token_hash("secret") != "secret"


def test_health_readiness_body_limit_and_worker() -> None:
    settings = Settings(
        database_url=SecretStr("postgresql://invalid:invalid@127.0.0.1:1/unavailable"),
        max_request_bytes=1024,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503
        assert client.post("/api/v1/capture/text", content=b"x" * 1025).status_code == 413
        assert client.post("/api/v1/capture/text", json={"content": "a"}).status_code == 401
        schema = client.get("/openapi.json").json()
        assert "HTTPBearer" in schema["components"]["securitySchemes"]
    with TestClient(create_app(settings, worker=True)) as worker:
        assert worker.get("/health").status_code == 200
        assert worker.get("/ready").status_code == 503
        assert worker.post("/api/v1/capture/text", json={"content": "a"}).status_code == 404
