import { describe, expect, it } from "vitest";

import type { ItemSummary } from "./api";
import { feedText } from "./feed-text";

const item = (fields: Partial<ItemSummary> & { snippet?: string }): ItemSummary & { snippet?: string } => ({
  id: "1",
  kind: "text",
  source: "web",
  title: null,
  user_note: null,
  mime_type: null,
  original_filename: null,
  source_url: null,
  excerpt: null,
  language: null,
  tags: [],
  collections: [],
  processing_status: "ready",
  created_at: "2026-10-31T10:00:00Z",
  captured_at: "2026-10-31T10:00:00Z",
  updated_at: "2026-10-31T10:00:00Z",
  deleted_at: null,
  ...fields,
});

describe("text items in the feed", () => {
  it("shows the stored text instead of the kind when there is no title or note", () => {
    expect(feedText(item({ excerpt: "Eine Idee für den Keller" }))).toEqual({
      headline: "Eine Idee für den Keller",
      details: [],
    });
  });

  it("keeps the note below the text when there is no title", () => {
    expect(feedText(item({ excerpt: "Kabelführung", user_note: "Schreibtisch" }))).toEqual({
      headline: "Kabelführung",
      details: ["Schreibtisch"],
    });
  });

  it("puts the title first and keeps both note and text", () => {
    expect(feedText(item({ title: "Setup", excerpt: "Kabelführung", user_note: "Schreibtisch" }))).toEqual({
      headline: "Setup",
      details: ["Schreibtisch", "Kabelführung"],
    });
    expect(feedText(item({ title: "Setup", excerpt: "Kabelführung" })).details).toEqual(["Kabelführung"]);
    expect(feedText(item({ title: "Setup", user_note: "Notiz" })).details).toEqual(["Notiz"]);
  });

  it("does not repeat identical or whitespace-only different lines", () => {
    expect(feedText(item({ title: "Idee", excerpt: "Idee" })).details).toEqual([]);
    expect(feedText(item({ title: "Idee", user_note: "Idee\nzwei", excerpt: "Idee zwei" })).details).toEqual([
      "Idee\nzwei",
    ]);
  });

  it("falls back to the note and then the kind, as before", () => {
    expect(feedText(item({ user_note: "nur Notiz" }))).toEqual({ headline: "nur Notiz", details: [] });
    expect(feedText(item({}))).toEqual({ headline: "text", details: [] });
  });

  it("renders markup-like text as the plain string it is", () => {
    const hostile = '<img src=x onerror="alert(1)">';
    expect(feedText(item({ excerpt: hostile })).headline).toBe(hostile);
  });
});

describe("other item kinds and search results", () => {
  it("keeps the old order for links and files", () => {
    expect(feedText(item({ kind: "url", source_url: "https://example.org/", title: "Seite" }))).toEqual({
      headline: "Seite",
      details: [],
    });
    expect(feedText(item({ kind: "url", source_url: "https://example.org/" })).headline).toBe("https://example.org/");
    expect(feedText(item({ kind: "document", original_filename: "beleg.pdf", user_note: "Miete" }))).toEqual({
      headline: "beleg.pdf",
      details: ["Miete"],
    });
    expect(feedText(item({ kind: "file" })).headline).toBe("file");
  });

  it("ignores an excerpt on a non-text item", () => {
    expect(feedText(item({ kind: "url", source_url: "https://example.org/", excerpt: "x" })).headline).toBe(
      "https://example.org/",
    );
  });

  it("shows the match snippet for search results and keeps the stored text as headline", () => {
    expect(feedText(item({ excerpt: "Die Tasche ist voll", snippet: "Die «Tasche» ist voll" }))).toEqual({
      headline: "Die Tasche ist voll",
      details: ["Die «Tasche» ist voll"],
    });
    expect(feedText(item({ title: "T", user_note: "N", snippet: "…«Tasche»…" })).details).toEqual(["…«Tasche»…"]);
  });
});
