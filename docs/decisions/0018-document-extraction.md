# ADR-0018: Header-only image metadata now, PDF text extraction later with pypdf

**Status:** accepted
**Date:** 2026-09-26

## Context

Spec section 7 lists `metadata`, `image_meta` and later OCR among the processors.
Image libraries (Pillow) and PDF parsers add native or large dependencies and
parse untrusted input with a long history of decoder bugs, while the priorities
put data safety and a simple architecture above enrichment breadth.

## Decision

`image_meta` reads width and height from the first 64 KiB of PNG, GIF, JPEG and
WebP originals with a hand-written header parser (no decoding, no dependency)
and stores them in `metadata.image_meta`; anything else is skipped. Basic file
metadata (size, hash, MIME) already lives on the item and blob rows, so no
separate `metadata` processor is added. PDF text extraction is deferred: when
it is added it will be a `pdf_text` processor using the pure-Python `pypdf`
library, bounded by the same 1 MiB extraction limit and failing per item, never
per worker. OCR stays out of scope until the basic pipeline has run in
production.

## Consequences

Images become filterable by dimensions later without a decoder in the worker.
PDFs remain searchable only by title, note and filename until `pdf_text` exists,
which is visible as a `skipped` `text_extract` run. Adding `pypdf` later needs no
schema change and reaches existing items through `bag reprocess`.
