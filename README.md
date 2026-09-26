# Bag of Holding

> A self-hosted, capture-first personal memory system. Drop anything in. Organize later. Retrieve by meaning.

**Status:** pre-implementation. The repository currently contains the specification and working rules; no code exists yet.

## What it is

Bag of Holding lets you put files, images, documents, URLs, text and code into one place without choosing folders, tags or types first. Originals are always preserved. Enrichment such as text extraction and search indexing runs afterwards, and AI features are optional and replaceable. The server is the memory; the Linux drop target, web UI and mobile sharing are thin clients of one API.

## Documents

| File | Purpose |
|---|---|
| [`docs/spec.md`](docs/spec.md) | Product vision, architecture, domain model, API, security, roadmap |
| [`AGENTS.md`](AGENTS.md) | Working rules, priorities, conventions and definition of done for coding agents |
| [`TODO.md`](TODO.md) | Actionable outstanding work, starting with Phase 0 |
| `docs/architecture.md` | The architecture as built (created in Phase 0) |
| `docs/decisions/` | Architecture decision records |

## Quickstart

Not available yet. Phase 0 adds the Compose environment and this section will contain the exact commands to start it and verify the first text-capture flow.

## Planned stack

Python 3.12+ with FastAPI, PostgreSQL 17 for data, full-text search and the job queue, content-addressed object storage (filesystem or S3-compatible), a small TypeScript PWA, and a GTK4 Linux client. Details and reasons are in the spec.

## License

MIT, see [`LICENSE`](LICENSE).
