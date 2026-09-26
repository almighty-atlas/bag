# TODO

Actionable outstanding work only. Keep items concrete; move vague ideas to `docs/spec.md` or drop them.

## Phase 0 — Foundation

- [ ] Initialize repository tooling for `apps/api` (Python 3.12+, FastAPI, ruff, mypy or pyright, pytest).
- [ ] Add `.env.example` with every configuration variable the API needs.
- [ ] Add Docker Compose in `deploy/compose/` with `bag-api`, `bag-worker`, `postgres` (17). Keep `bag-web` and `minio` for later phases.
- [ ] Add health (`/health`) and readiness (`/ready`) endpoints.
- [ ] Add migration tooling and the initial schema: `user`, `api_token`, `item`, `blob`, `processing_run`, `tag`, `item_tag`, `collection`, `item_collection`, `relation`. All owned tables carry `owner_id`.
- [ ] Seed the single MVP user and print a first API token on initial setup (`bag init` or equivalent).
- [ ] Add CI running lint, type check, tests and build.
- [ ] Write `docs/architecture.md`, `docs/data-model.md` and `docs/capture-pipeline.md` describing what exists after this phase.
- [ ] Record ADRs for: ID strategy (UUIDv7), job queue (PostgreSQL-backed), storage layout (content-addressed).
- [ ] Fill in the **Commands** section of `AGENTS.md`.

## Phase 1 — First vertical slice: text capture

- [ ] Bearer-token authentication middleware resolving `owner_id`.
- [ ] `POST /api/v1/capture/text` with `content`, optional `user_note`, `captured_at`, `client_capture_id`.
- [ ] Idempotency: replaying the same `client_capture_id` returns the original item.
- [ ] Response only after the row is committed.
- [ ] `GET /api/v1/items/{id}` returning the item with content intact.
- [ ] Integration test proving `POST text -> persist Item -> return ID -> GET Item -> content intact`, including idempotent replay and Unicode content.
- [ ] Update `README.md` with the exact commands to start the environment and verify the flow.

## Later phases

See the roadmap in `docs/spec.md` section 18. Break the next phase into items here when the current one is done.
