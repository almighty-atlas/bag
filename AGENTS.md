# AGENTS.md — Working rules for Bag of Holding

Read this before changing anything. The product and architecture specification is in [`docs/spec.md`](docs/spec.md). Outstanding work is in [`TODO.md`](TODO.md).

## Project in one paragraph

Bag of Holding is a self-hosted, capture-first personal memory system. Anything can be dropped in without choosing folders, tags or types. Originals are preserved, enrichment runs asynchronously, and the server behind the Bag API is the only canonical store. AI is optional and never on the capture path.

## Priorities when requirements conflict

In this order:

1. **data safety**
2. **capture reliability**
3. **low-friction UX**
4. **privacy and self-hostability**
5. **simple, maintainable architecture**
6. **future extensibility**
7. visual polish

## Architecture boundaries

- Clients (web, Linux, mobile, scripts) talk to the API only. Never to the database or storage.
- The API returns a capture response only after the original is durably persisted.
- Enrichment happens in workers through the PostgreSQL-backed job queue. Jobs are idempotent and retryable.
- Storage is content-addressed. Filenames never influence storage paths.
- Every owned entity carries `owner_id` and every query is scoped by it, even while there is one user.
- Deletion is soft. Purge and storage garbage collection are explicit background jobs.
- No Redis, no separate vector database, no microservices in the MVP.

## How to work

1. Inspect the existing repository and documentation before implementing.
2. Preserve working behavior.
3. Implement the smallest coherent vertical slice.
4. Run the relevant tests, lint and build checks.
5. Fix failures before moving on.
6. Update documentation to reflect reality (see below).
7. Commit logically separable changes with clear messages.

Do not implement the entire roadmap in one pass. Do not start the AI roadmap (Phase 6) before Phases 0 to 5 are stable. Do not start the Linux GUI, AI functionality or complex extractors until `TODO.md` says so.

When the spec leaves a choice open, decide, write a short ADR in `docs/decisions/` and move on. When a decision would contradict the spec, stop and ask.

## Commands

Fill this section in during Phase 0 and keep it current. Every command below must work from a fresh clone.

```text
# development environment
<compose up>
<migrate>

# quality gates
<lint>
<type check>
<tests>
<build>
```

## Conventions

- Python 3.12+, FastAPI, type hints everywhere, `ruff` for lint and format, `mypy` or `pyright` in strict mode.
- TypeScript strict mode in the web app. Node 22 LTS.
- PostgreSQL 17. Schema changes only through migrations. Migrations run explicitly, never on container start.
- UUIDv7 for all IDs, generated server-side. `timestamptz` in UTC for all timestamps.
- Structured logging with stable IDs. Never log item content, notes or tokens.
- Configuration through environment variables documented in `.env.example`. No secrets in the repository.
- Field names: `kind` for the item type, `source` for the capturing client. Do not introduce `type` or `source_type`.
- TODOs live in `TODO.md`, not in code comments.

## Security requirements

- MIME type is verified from content, never trusted from the client alone.
- Uploaded content is never executed or rendered unsanitized.
- URL fetching blocks private and link-local addresses, resolves DNS before connecting, limits redirects, size and time.
- Bearer tokens are stored hashed. Session cookies are `HttpOnly`, `Secure`, `SameSite=Lax`, with CSRF protection on state-changing requests.
- Originals are preserved but treated as untrusted input.

## Documentation the agent maintains

| File | Content |
|---|---|
| `README.md` | Status, setup, exact commands, basic usage, screenshots when available |
| `docs/architecture.md` | The architecture as actually built, not as planned |
| `docs/data-model.md` | Entities, important fields, migration considerations |
| `docs/capture-pipeline.md` | How every supported input becomes an item |
| `docs/decisions/` | Short ADRs for meaningful decisions, using the template in that folder |
| `TODO.md` | Actionable outstanding work only, no vague ideas |

`docs/spec.md` is the target and is not rewritten to match reality. Deviations are recorded as ADRs.

## Definition of done

A change is complete only when all relevant items hold:

- implementation works end to end
- tests added or updated, and passing
- lint and type checks pass
- migrations included when the schema changed
- failure behavior considered and tested where data safety is involved
- security implications considered
- documentation updated when behavior or architecture changed
- no unrelated regressions
- remaining work is listed in `TODO.md`, not hidden in comments
