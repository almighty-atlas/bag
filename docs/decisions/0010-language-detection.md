# ADR-0010: Stopword language detection with a user override marker

**Status:** accepted
**Date:** 2026-09-26

## Context

Spec section 8.1 wants a lightweight detector to set `item.language` so the
German or English text search configuration applies, and allows the user to
correct it. Detection libraries add dependencies and models for a two-language
MVP, and generated metadata must never overwrite user input.

## Decision

A `language` processor counts frequent German and English function words in the
item's own plain text (inline content or a decoded UTF-8 text file, first 20 000
characters). It sets `de` or `en` only with at least three hits and a 1.5× margin;
otherwise it skips and records `metadata.language.detected = null`. URLs and
binaries are skipped. The processor also skips whenever `metadata.language.user`
is true, the marker a future PATCH endpoint sets when the user chooses a language.

## Consequences

No new dependency; detection is deterministic and cheap. Short or mixed texts stay
on the `simple` configuration, which still matches exact tokens. Other languages
are never detected and would need new stopword lists or a library, recorded in a
new ADR. Items processed before this processor existed gain a language through
`bag reprocess`. The user marker protocol must be honored by the upcoming PATCH.
