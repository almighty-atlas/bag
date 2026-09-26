import logging
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from tempfile import SpooledTemporaryFile
from typing import Annotated, Any
from uuid import UUID

import psycopg
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from bag.auth import Identity, authenticate, unauthorized
from bag.capture import (
    capture_file,
    capture_text,
    capture_url,
    get_item,
    get_processing,
    reprocess_item,
    restore_item,
    trash_item,
    update_item,
)
from bag.config import Settings
from bag.db import connection, ready
from bag.download import download
from bag.logging import configure_logging
from bag.organize import Kind, create_named, list_named
from bag.ratelimit import FailureLimiter
from bag.schemas import (
    CaptureResponse,
    FileCapture,
    ItemPage,
    ItemResponse,
    ItemUpdate,
    LoginRequest,
    NameCreate,
    NamedResponse,
    ProcessingRunResponse,
    SearchPage,
    SessionResponse,
    TextCapture,
    TokenCreate,
    TokenCreated,
    TokenResponse,
    UrlCapture,
)
from bag.search import list_items, search_items
from bag.sessions import COOKIE_NAME, LoginError, authenticate_session, login, logout
from bag.storage import CHUNK_SIZE, FileSystemStorage, StorageError, UploadTooLarge
from bag.tokens import TokenAdminError, create_token, list_tokens, revoke_token


class RequestSizeLimit:
    def __init__(self, app: ASGIApp, limit: int, upload_limit: int) -> None:
        self.app = app
        self.limit = limit
        self.upload_limit = upload_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit
        if scope["path"] == "/api/v1/capture/file":
            limit += self.upload_limit
        # Bound memory even before multipart parsing, including requests without Content-Length.
        with SpooledTemporaryFile(max_size=CHUNK_SIZE) as body:
            size = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                data = message.get("body", b"")
                size += len(data)
                if size > limit:
                    await JSONResponse({"detail": "Request body too large"}, 413)(
                        scope, receive, send
                    )
                    return
                await run_in_threadpool(body.write, data)
                if not message.get("more_body", False):
                    break
            await run_in_threadpool(body.seek, 0)
            delivered = 0

            async def bounded_receive() -> Message:
                nonlocal delivered
                if delivered > size:
                    return await receive()
                data = await run_in_threadpool(body.read, CHUNK_SIZE)
                delivered += len(data)
                more = delivered < size
                if not more:
                    delivered = size + 1
                return {"type": "http.request", "body": data, "more_body": more}

            await self.app(scope, bounded_receive, send)


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "x-bag-csrf"


def whoami(settings: Settings, actor: Identity | None, expires: datetime | None) -> SessionResponse:
    assert actor is not None
    with connection(settings) as conn:
        row = conn.execute(
            'SELECT username, display_name FROM "user" WHERE id = %s', (actor.owner_id,)
        ).fetchone()
    assert row is not None
    return SessionResponse(
        owner_id=actor.owner_id,
        username=row["username"],
        display_name=row["display_name"],
        via=actor.via,
        expires_at=expires,
    )


def create_app(
    settings: Settings | None = None,
    *,
    worker: bool = False,
    worker_alive: Callable[[], bool] | None = None,
) -> FastAPI:
    settings = settings or Settings()
    configure_logging()
    app = FastAPI(title="Bag of Holding worker" if worker else "Bag of Holding", version="0.1.0")
    app.add_middleware(
        RequestSizeLimit,
        limit=settings.max_request_bytes,
        upload_limit=settings.max_upload_bytes,
    )
    storage = FileSystemStorage(settings.storage_path)

    @app.exception_handler(StorageError)
    async def storage_error(request: Request, exc: StorageError) -> JSONResponse:
        logging.getLogger("bag.api").error("storage_unavailable")
        return JSONResponse({"detail": "Original storage unavailable; retry with the same ID"}, 503)

    @app.exception_handler(UploadTooLarge)
    async def upload_too_large(request: Request, exc: UploadTooLarge) -> JSONResponse:
        return JSONResponse({"detail": "File too large"}, 413)

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
        is_ready = ready(settings) and (worker_alive is None or worker_alive())
        return JSONResponse(
            {"status": "ready" if is_ready else "not_ready"},
            200 if is_ready else 503,
        )

    if worker:
        return app

    bearer = HTTPBearer(auto_error=False)

    def csrf_guard(request: Request) -> None:
        # Cookies are sent cross-site by forms; a custom header cannot be, so require one.
        if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(403, f"Missing {CSRF_HEADER} header")

    def identity(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Identity:
        if credentials is not None:
            return authenticate(settings, f"{credentials.scheme} {credentials.credentials}")
        cookie = request.cookies.get(COOKIE_NAME)
        if cookie:
            actor = authenticate_session(settings, cookie)
            if actor is not None:
                csrf_guard(request)
                return actor
        raise unauthorized()

    def session_only(actor: Annotated[Identity, Depends(identity)]) -> Identity:
        if actor.via != "session":
            raise HTTPException(403, "Sign in with a password to manage tokens")
        return actor

    limiter = FailureLimiter(settings.login_max_failures, settings.login_window_seconds)

    @app.post("/api/v1/session", response_model=SessionResponse)
    def create_session(
        payload: LoginRequest, request: Request, response: Response
    ) -> SessionResponse:
        csrf_guard(request)
        client = request.client.host if request.client else "unknown"
        keys = (f"user:{payload.username.strip()}", f"addr:{client}")
        waits = [wait for wait in (limiter.retry_after(key) for key in keys) if wait is not None]
        if waits:
            raise HTTPException(
                429,
                "Too many failed logins; try again later",
                headers={"Retry-After": str(int(max(waits)) + 1)},
            )
        try:
            token, expires = login(settings, payload.username, payload.password)
        except LoginError as exc:
            for key in keys:
                limiter.record_failure(key)
            raise HTTPException(401, "Invalid username or password") from exc
        for key in keys:
            limiter.reset(key)
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=settings.session_days * 86_400,
            path="/api",
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )
        return whoami(settings, authenticate_session(settings, token), expires)

    @app.get("/api/v1/session", response_model=SessionResponse)
    def read_session(actor: Annotated[Identity, Depends(identity)]) -> SessionResponse:
        return whoami(settings, actor, None)

    @app.delete("/api/v1/session", status_code=204)
    def delete_session(
        request: Request, actor: Annotated[Identity, Depends(session_only)]
    ) -> Response:
        logout(settings, request.cookies.get(COOKIE_NAME, ""))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE_NAME, path="/api")
        return response

    @app.get("/api/v1/tokens", response_model=list[TokenResponse])
    def tokens(actor: Annotated[Identity, Depends(session_only)]) -> list[TokenResponse]:
        return [TokenResponse(**asdict(row)) for row in list_tokens(settings, actor.owner_id)]

    @app.post("/api/v1/tokens", response_model=TokenCreated, status_code=201)
    def create_api_token(
        payload: TokenCreate, actor: Annotated[Identity, Depends(session_only)]
    ) -> TokenCreated:
        info, secret = create_token(settings, payload.name, actor.owner_id)
        return TokenCreated(**asdict(info), token=secret.get_secret_value())

    @app.delete("/api/v1/tokens/{token_id}", status_code=204)
    def revoke_api_token(
        token_id: UUID, actor: Annotated[Identity, Depends(session_only)]
    ) -> Response:
        try:
            revoke_token(settings, token_id, actor.owner_id)
        except TokenAdminError as exc:
            raise HTTPException(404, "Token not found") from exc
        return Response(status_code=204)

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

    @app.post("/api/v1/capture/url", response_model=CaptureResponse, status_code=201)
    def post_url(
        payload: UrlCapture,
        actor: Annotated[Identity, Depends(identity)],
        idempotency_key: Annotated[UUID | None, Header()] = None,
    ) -> CaptureResponse:
        if idempotency_key is not None:
            if payload.client_capture_id not in (None, idempotency_key):
                raise HTTPException(422, "Conflicting idempotency keys")
            payload = payload.model_copy(update={"client_capture_id": idempotency_key})
        return capture_url(settings, actor.owner_id, payload)

    Filters = Annotated[str | None, Query(min_length=1, max_length=100, pattern=r"^[a-z_-]+$")]
    Limit = Annotated[int, Query(ge=1, le=100)]

    Name = Annotated[str | None, Query(min_length=1, max_length=100)]

    def filter_params(
        kind: Filters = None,
        status: Filters = None,
        captured_from: Annotated[AwareDatetime | None, Query(alias="from")] = None,
        captured_to: Annotated[AwareDatetime | None, Query(alias="to")] = None,
        trashed: bool = False,
        tag: Name = None,
        collection: Name = None,
    ) -> dict[str, Any]:
        for value in (tag, collection):
            if value is not None and "\x00" in value:
                raise HTTPException(422, "Invalid request")
        return {
            "kind": kind,
            "status": status,
            "captured_from": captured_from,
            "captured_to": captured_to,
            "trashed": trashed,
            "tag": tag,
            "collection": collection,
        }

    def named_routes(kind: Kind) -> None:
        @app.get(f"/api/v1/{kind}s", response_model=list[NamedResponse])
        def listing(actor: Annotated[Identity, Depends(identity)]) -> list[NamedResponse]:
            return list_named(settings, kind, actor.owner_id)

        @app.post(f"/api/v1/{kind}s", response_model=NamedResponse, status_code=201)
        def create(
            payload: NameCreate, actor: Annotated[Identity, Depends(identity)], response: Response
        ) -> NamedResponse:
            named, created = create_named(settings, kind, actor.owner_id, payload.name)
            if not created:
                response.status_code = 200
            return named

    named_routes("tag")
    named_routes("collection")

    @app.get("/api/v1/items", response_model=ItemPage)
    def items(
        actor: Annotated[Identity, Depends(identity)],
        filters: Annotated[dict[str, Any], Depends(filter_params)],
        limit: Limit = 20,
        cursor: UUID | None = None,
    ) -> ItemPage:
        return list_items(settings, actor.owner_id, limit=limit, cursor=cursor, **filters)

    @app.get("/api/v1/search", response_model=SearchPage)
    def search(
        actor: Annotated[Identity, Depends(identity)],
        filters: Annotated[dict[str, Any], Depends(filter_params)],
        q: Annotated[str, Query(min_length=1, max_length=500)],
        limit: Limit = 20,
        offset: Annotated[int, Query(ge=0, le=1000)] = 0,
    ) -> SearchPage:
        if "\x00" in q:
            raise HTTPException(422, "Invalid query")
        return search_items(settings, actor.owner_id, q=q, limit=limit, offset=offset, **filters)

    @app.get("/api/v1/items/{item_id}", response_model=ItemResponse)
    def item(item_id: UUID, actor: Annotated[Identity, Depends(identity)]) -> ItemResponse:
        return get_item(settings, actor.owner_id, item_id)

    @app.patch("/api/v1/items/{item_id}", response_model=ItemResponse)
    def update(
        item_id: UUID, payload: ItemUpdate, actor: Annotated[Identity, Depends(identity)]
    ) -> ItemResponse:
        return update_item(settings, actor.owner_id, item_id, payload)

    @app.delete("/api/v1/items/{item_id}", status_code=204)
    def trash(item_id: UUID, actor: Annotated[Identity, Depends(identity)]) -> Response:
        trash_item(settings, actor.owner_id, item_id)
        return Response(status_code=204)

    @app.post("/api/v1/items/{item_id}/restore", response_model=ItemResponse)
    def restore(item_id: UUID, actor: Annotated[Identity, Depends(identity)]) -> ItemResponse:
        return restore_item(settings, actor.owner_id, item_id)

    @app.get("/api/v1/items/{item_id}/processing", response_model=list[ProcessingRunResponse])
    def processing(
        item_id: UUID, actor: Annotated[Identity, Depends(identity)]
    ) -> list[ProcessingRunResponse]:
        return get_processing(settings, actor.owner_id, item_id)

    @app.post(
        "/api/v1/items/{item_id}/reprocess",
        response_model=list[ProcessingRunResponse],
        status_code=202,
    )
    def reprocess(
        item_id: UUID, actor: Annotated[Identity, Depends(identity)]
    ) -> list[ProcessingRunResponse]:
        return reprocess_item(settings, actor.owner_id, item_id)

    @app.post("/api/v1/capture/file", response_model=CaptureResponse, status_code=201)
    def post_file(
        actor: Annotated[Identity, Depends(identity)],
        file: Annotated[UploadFile, File()],
        metadata: Annotated[str, Form()] = "{}",
        idempotency_key: Annotated[UUID | None, Header()] = None,
    ) -> CaptureResponse:
        if len(metadata.encode("utf-8")) > settings.max_request_bytes:
            raise HTTPException(413, "Metadata too large")
        try:
            payload = FileCapture.model_validate_json(metadata)
        except ValidationError as exc:
            raise HTTPException(422, "Invalid metadata") from exc
        if idempotency_key is not None:
            if payload.client_capture_id not in (None, idempotency_key):
                raise HTTPException(422, "Conflicting idempotency keys")
            payload = payload.model_copy(update={"client_capture_id": idempotency_key})
        if file.size is not None and file.size > settings.max_upload_bytes:
            raise UploadTooLarge
        return capture_file(
            settings,
            actor.owner_id,
            payload,
            file.file,
            file.filename or "download",
            storage,
        )

    @app.get("/api/v1/items/{item_id}/content")
    def original(item_id: UUID, actor: Annotated[Identity, Depends(identity)]) -> StreamingResponse:
        return download(settings, actor.owner_id, item_id, storage)

    @app.get("/api/v1/items/{item_id}/snapshot")
    def snapshot(item_id: UUID, actor: Annotated[Identity, Depends(identity)]) -> StreamingResponse:
        return download(settings, actor.owner_id, item_id, storage, "snapshot")

    return app
