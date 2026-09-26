import logging
from collections.abc import Callable
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

from bag.auth import Identity, authenticate
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
from bag.db import ready
from bag.download import download
from bag.logging import configure_logging
from bag.organize import Kind, create_named, list_named
from bag.schemas import (
    CaptureResponse,
    FileCapture,
    ItemPage,
    ItemResponse,
    ItemUpdate,
    NameCreate,
    NamedResponse,
    ProcessingRunResponse,
    SearchPage,
    TextCapture,
    UrlCapture,
)
from bag.search import list_items, search_items
from bag.storage import CHUNK_SIZE, FileSystemStorage, StorageError, UploadTooLarge


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

    return app
