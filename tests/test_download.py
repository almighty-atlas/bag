import asyncio
import io

import pytest
from bag.download import OriginalResponse
from starlette.requests import ClientDisconnect
from starlette.types import Message


def test_download_closes_original_when_client_disconnects() -> None:
    source = io.BytesIO(b"original")
    response = OriginalResponse(source, {})

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            raise OSError("client disconnected")

    with pytest.raises(ClientDisconnect):
        asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    assert source.closed
