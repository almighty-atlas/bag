import logging
from typing import Annotated
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from bag.auth import Identity, authenticate
from bag.capture import capture_text, get_item
from bag.config import Settings
from bag.db import ready
from bag.logging import configure_logging
from bag.schemas import CaptureResponse, ItemResponse, TextCapture


class RequestSizeLimit:
    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.limit:
                await JSONResponse({"detail": "Request body too large"}, 413)(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, bounded_receive, send)


def create_app(settings: Settings | None = None, *, worker: bool = False) -> FastAPI:
    settings = settings or Settings()
    configure_logging()
    app = FastAPI(title="Bag of Holding worker" if worker else "Bag of Holding", version="0.1.0")
    app.add_middleware(RequestSizeLimit, limit=settings.max_request_bytes)

    @app.exception_handler(psycopg.Error)
    async def database_error(request: Request, exc: psycopg.Error) -> JSONResponse:
        logging.getLogger("bag.api").error("database_unavailable")
        return JSONResponse({"detail": "Database unavailable; retry with the same capture ID"}, 503)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "Invalid request"}, 422)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def readiness() -> JSONResponse:
        is_ready = ready(settings)
        return JSONResponse(
            {"status": "ready" if is_ready else "not_ready"},
            200 if is_ready else 503,
        )

    if worker:
        return app

    bearer = HTTPBearer(auto_error=False)

    def identity(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Identity:
        value = f"{credentials.scheme} {credentials.credentials}" if credentials else None
        return authenticate(settings, value)

    @app.post("/api/v1/capture/text", response_model=CaptureResponse, status_code=201)
    def post_text(
        payload: TextCapture,
        actor: Annotated[Identity, Depends(identity)],
        idempotency_key: Annotated[UUID | None, Header()] = None,
    ) -> CaptureResponse:
        if idempotency_key is not None:
            if payload.client_capture_id not in (None, idempotency_key):
                raise HTTPException(422, "Conflicting idempotency keys")
            payload = payload.model_copy(update={"client_capture_id": idempotency_key})
        return capture_text(settings, actor.owner_id, payload)

    @app.get("/api/v1/items/{item_id}", response_model=ItemResponse)
    def item(item_id: UUID, actor: Annotated[Identity, Depends(identity)]) -> ItemResponse:
        return get_item(settings, actor.owner_id, item_id)

    return app
