# ADR-0024: Mobile-first web layout, navigation and status messages

**Status:** accepted
**Date:** 2026-11-01

## Context

Tests on a real iPhone (PWA) and a CachyOS desktop showed: the top navigation wrapped
badly, the native file control was clipped, search and filter fields were cramped, the
date fields had no visible labels, forms made the page scroll sideways, and save
failures or unconfirmed saves were shown in the same green as a success. Capture and
search matter most on a phone and must stay near the top.

## Decision

- **Navigation.** Below 900 px the links sit behind one labelled "Menü" button that
  discloses them (`aria-expanded`, `aria-controls`; closes on Escape, on a page change
  and on a tap outside). From 900 px they are inline: with a long user name they need
  about 840 px, so 720 to 899 px (tablets, small windows) keep the menu. The user name
  in "Abmelden (…)" is cut at 14 characters with an ellipsis in the inline bar; the full
  name stays in the button text and its tooltip, and is shown in full in the menu. All entries stay in the DOM in both
  modes: Feed, Tags & Sammlungen (formerly "Tags"), Papierkorb, Token and Abmelden. The
  current page carries `aria-current`.
- **Status messages.** One `Notice` component with three tones. Success is only a save the
  server confirmed. An unanswered request ("Speichern nicht bestätigt"), unconfirmed
  files and a share that is no longer open are warnings. A refused save, lost share files
  and load errors are errors. Tones differ in color, glyph (✓ ⚠ ✕) and a spoken label;
  errors use `role="alert"`, the rest `role="status"`. Retry and discard stay unchanged.
  A server failure (HTTP 5xx) or timeout (408) can happen after the item was stored, so it
  is a warning "Speichern nicht bestätigt (HTTP …)", not an error; a retry reuses the same
  capture ID. Clear refusals (for example 400, 413, 422) stay errors "Nicht gespeichert: …".
- **Capture and search.** Order on phones: capture (text, thought, then "Dateien
  hochladen" and "In die Tasche"), search, then a collapsed "Filter" section. Filters are
  open on wide screens (720 px) and whenever one is set, also when it is set later (tag
  link, other route); collapsing by hand stays possible until the filters change again.
  The section shows the number of active filters.
  Every filter has a visible label; the two dates read "Erfasst von" and "Erfasst bis"
  (the filter applies to the capture time). The native file input is visually hidden but
  focusable; its label is the visible, touch-sized button. A picked file still uploads at
  once. The search syntax tip moved from the placeholder into the filter section.
- **Overflow.** Card grids use `minmax(0, 1fr)`, form controls are at most as wide as their
  container, long names wrap, controls are 16 px (no iOS zoom on focus) and at least 44 px
  tall on coarse pointers, and the viewport uses `viewport-fit=cover` with safe-area
  padding. Tables stack on narrow screens; explicit ARIA roles keep them tables.
- **Image cards.** The thumbnail stays beside the text on every width: 96 px square on
  phones, 168 × 126 px from 560 px, always `object-fit: contain`. This replaces the 180 px
  tall frame stacked above the text, which made image rows about three times as tall as
  text rows. The thumbnail pipeline, endpoint and fallbacks are unchanged.

## Consequences

No API, storage or schema change. The layout is verified in headless Chromium against a
mocked API from 320 to 1280 px (including 720, 768 and 900 px with user names up to 63
characters) and by component tests for the semantics; it has not yet been checked on real
devices. The browser check must not use Chromium's mobile emulation (`isMobile`), which
widens the viewport to fit overflowing content and hides the overflow.
