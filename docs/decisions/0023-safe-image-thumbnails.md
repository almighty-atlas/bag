# ADR-0023: Re-encoded WebP thumbnails from an isolated Pillow child process

**Status:** accepted
**Date:** 2026-10-31
**Supersedes in part:** ADR-0018 (no image decoder in the worker)

## Context

Phase 3B needs image cards. Originals are untrusted and are only ever delivered
as attachments (sandboxed, `application/octet-stream`). Showing them inline for a
cosmetic preview would make every stored SVG, polyglot or malformed image part of
the application's origin. A preview therefore has to be a different object, built
by decoding and re-encoding, and decoding itself is the risky step: bombs, crashing
decoders and metadata (GPS, camera, ICC, XMP) all arrive through it.

## Decision

- **Decoder and encoder:** Pillow (`pillow>=12,<14`, wheels bundle libjpeg, libwebp
  and zlib). It decodes PNG, JPEG, WebP and GIF only (`Image.open(formats=...)`);
  SVG, HEIC, AVIF, TIFF, BMP and the rest are skipped with `unsupported_format`.
  The output is always a lossy-or-alpha WebP, at most 512 px per side, never
  enlarged, first frame only for animations, EXIF orientation applied to the
  pixels. The writer is given no EXIF, ICC or XMP, and the result must consist
  solely of `VP8 `, `VP8L`, `VP8X` and `ALPH` chunks (`is_clean_webp`).
- **Limits:** 32 MiB input, 40 megapixels, 16 384 px per side, checked from the
  header before any pixel is decoded; JPEG uses libjpeg draft scaling. The output
  must fit 256 KiB (quality 80, 60, 40, then `output_too_large`).
- **Isolation:** the worker renders in `python -I -m bag.imaging`, a child with an
  empty environment (no database URL), `RLIMIT_AS` 1 GiB, 20 s CPU, no core dump,
  an output file-size limit and a 30 s wall clock. Original bytes go in on stdin;
  the outcome is the exit code. A decoder crash, a hang or a memory blow-up is a
  permanent, per-item `failed` run and never reaches the worker. If the child
  cannot be started the run is retryable.
- **Processor:** a seventh processor, `thumbnail`, runs for every capture like the
  others and is `skipped` for non-images. Outcomes: `succeeded`; `skipped` with
  `reason` `unsupported_format`, `too_large` or `output_too_large`; `failed`
  (permanent) for corrupt images; retryable failure when the original is missing
  or fails its integrity check or storage is down. Failures never affect the
  capture, which is acknowledged before any thumbnail work.
- **Storage:** a derived `blob` row with role `thumbnail`, written through the
  existing content-addressed store, one row per item, replaced on rerun (as for
  `snapshot`). Processors may write only `snapshot` and `thumbnail` roles. Equal
  thumbnails share one object, and `bag gc` keeps it until the last row is purged.
- **Delivery:** `GET /api/v1/items/{id}/thumbnail`, authenticated like every item
  route, scoped by `owner_id`, `404` for trashed, foreign, missing or unavailable
  thumbnails. The bytes come only from the `thumbnail` blob, are integrity-checked
  and re-validated with `is_clean_webp` on every response, so an edited import or
  tampered object is never served inline. Headers: `image/webp`, `nosniff`, a
  `default-src 'none'; sandbox` CSP, `Cross-Origin-Resource-Policy: same-origin`,
  `Cache-Control: private, no-cache` with a content-hash `ETag` (`304` on
  `If-None-Match`), and `no-store` on errors. `/content` and `/snapshot` are
  unchanged.

## Consequences

No schema migration: `blob.role` is free text and the processor and job tables are
keyed by processor name. Deploying needs a rebuilt image (new dependency) and a
worker restart; existing images get thumbnails through `bag reprocess` (default
scope, processors that never ran). Export carries the thumbnail row and object;
import verifies hashes only, and the endpoint's own validation covers edited
exports. Items with a stale or invalid thumbnail show none until `bag reprocess
--scope all`.

Known limits: the child's memory cap is address space, not resident memory, so
huge-but-allowed images still need roughly 0.3 to 0.7 GiB of headroom per render
(the worker runs one job at a time); a container memory limit below that fails the
run as `corrupt`. Colour management is not applied (ICC profiles are dropped, which
can shift wide-gamut colours). Animations show their first frame. HEIC/AVIF and
SVG previews are not supported. Pillow decoder CVEs remain possible: keep the
dependency current, and the child isolation bounds their effect rather than
removing it.
