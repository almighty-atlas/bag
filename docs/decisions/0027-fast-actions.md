# ADR-0027: Fast actions: clipboard paste, item actions and readable processing states

**Status:** accepted
**Date:** 2026-11-21

## Context

Phase 3B of TODO.md asks for "clipboard paste for text and supported images; context actions
to open/copy/share/download; user-friendly processing status instead of raw processor names".
The web feed only accepted typed text, picked files and drag and drop. Copying, sharing or
downloading an item meant opening its detail view, and the feed and detail showed server
values such as `queued`, `partial`, `url_fetch` or `skipped`.

Browsers differ here. Reading the clipboard needs a user gesture and sometimes a permission
prompt, and Firefox only has `navigator.clipboard.read` from version 125. Safari only writes
to the clipboard inside the click, unless it gets a pending `ClipboardItem`. The share sheet
(`navigator.share`) is mostly available on phones and in Safari, and only some of them accept
files. Priorities: capture reliability and no false success (AGENTS.md), then low friction.

## Decision

**Paste.**
- A paste of files or images into the capture text field saves them at once, like a drop.
  Images arrive as `image.png` or without a name and are renamed to `Zwischenablage
  YYYY-MM-DD HH-MM-SS.<ext>` (local time); real file names stay.
- A paste outside any text field (`Strg+V`/`⌘V` on the feed) also saves files. Pasted text is
  only put into the capture field, appended on a new line, and focused, with the warning
  "Eingefügt, noch nicht gespeichert". A stray paste must never become an item.
- Pastes into other fields (search, note, filters) are left to the browser.
- Where the browser can read the clipboard on a button press, the form shows a secondary
  "Einfügen" button. It reads PNG, JPEG, GIF or WebP images first, else plain text, with the
  same rules. A refusal explains the keyboard or long-press paste.
- Every upload goes through the existing `uploadFiles` path: the same retry IDs, the same
  warning when a save is unconfirmed and the same success only after the server confirmed it.

**Item actions** (`actions.tsx`) in the detail view and in a "⋯" menu at each feed row:
- "Link öffnen" for a saved URL (new tab, `noopener noreferrer nofollow`) and "Original
  herunterladen" for files, which keeps the existing attachment download.
- Copy puts the text of a text item, the URL of a link item, or the item's own Bag link for
  files on the clipboard. Copying an image's bytes is left out: browsers only accept PNG there,
  and the original may be anything.
- "Teilen…" uses the system share sheet when it exists: text and links directly, files as the
  downloaded original only where `navigator.canShare({files})` agrees. Closing the sheet is not
  an error.
- The feed menu is a `<details>` element beside the row link, not inside it, so the row stays
  one tap target and the menu works by keyboard (Escape closes it) and by touch (a tap outside
  closes it). Its content is only rendered while it is open, so a closed feed carries no extra
  links. Trashed rows have no menu.
- A pin action ("Merkliste") is not part of this ADR. It needs its own data decision (TODO.md).

**Readable states** (`labels.ts`): item kinds (Text, Link, Bild, Dokument, Datei), item states
("wartet auf Verarbeitung", "wird verarbeitet", "fertig", "teilweise verarbeitet",
"Verarbeitung fehlgeschlagen"), run states and processor names ("Webseite abrufen",
"Vorschaubild erstellen", …) are shown in German. Unknown values from a newer server are shown
as sent. The API is unchanged.

## Consequences

Saving from the clipboard is one keystroke or one tap, without a file dialog, and repeated
actions no longer need the detail view. No API, schema or service worker change was needed.

Behavior on real devices is unverified. Vitest component tests (happy-dom, stubbed clipboard
and share APIs) and one headless Chromium layout pass (320 to 1280 px, no horizontal overflow,
44 px menu target) cover it. Still open, and tracked in TODO.md: paste on Android and iOS
(long press into the text field), the clipboard permission prompts of Safari and Firefox,
sharing files from the PWA, and a screen reader pass of the menu.
