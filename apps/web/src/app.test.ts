import { describe, expect, it } from "vitest";

import { looksLikeUrl } from "./api";
import { href, parseRoute, sharedText } from "./app";

describe("routes", () => {
  it("parses feed, search, trash, item and token routes", () => {
    expect(parseRoute("")).toEqual({ name: "feed", query: "", trashed: false });
    expect(parseRoute("#/?q=Tasche%20OR%20Keller&trashed=1")).toEqual({
      name: "feed",
      query: "Tasche OR Keller",
      trashed: true,
    });
    expect(parseRoute("#/items/01a0de51-0fcc-7089-ae37-1a0e48153325")).toEqual({
      name: "item",
      id: "01a0de51-0fcc-7089-ae37-1a0e48153325",
    });
    expect(parseRoute("#/items/../etc")).toEqual({ name: "feed", query: "", trashed: false });
    expect(parseRoute("#/tokens")).toEqual({ name: "tokens" });
  });

  it("round-trips through href", () => {
    for (const route of [
      { name: "feed", query: "", trashed: false } as const,
      { name: "feed", query: "a b&c", trashed: true } as const,
      { name: "item", id: "01a0de51-0fcc-7089-ae37-1a0e48153325" } as const,
      { name: "tokens" } as const,
    ]) {
      expect(parseRoute(href(route))).toEqual(route);
    }
  });
});

describe("share target", () => {
  it("prefers the URL and falls back to title and text", () => {
    expect(sharedText("/", "?text=x")).toBeNull();
    expect(sharedText("/share", "?url=https%3A%2F%2Fexample.org%2Fa&text=ignored")).toBe(
      "https://example.org/a",
    );
    expect(sharedText("/share", "?text=https%3A%2F%2Fexample.org%2Fb")).toBe("https://example.org/b");
    expect(sharedText("/share", "?title=Titel&text=Ein%20Gedanke")).toBe("Titel\nEin Gedanke");
    expect(sharedText("/share", "")).toBeNull();
  });

  it("recognizes bare URLs for link capture", () => {
    expect(looksLikeUrl(" https://example.org/x?y=1 ")).toBe(true);
    expect(looksLikeUrl("https://example.org/x\nmore")).toBe(false);
    expect(looksLikeUrl("see https://example.org")).toBe(false);
    expect(looksLikeUrl("ftp://example.org")).toBe(false);
  });
});
