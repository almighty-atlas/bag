import { describe, expect, it } from "vitest";

import { looksLikeUrl } from "./api";
import { EMPTY_FILTERS, FEED, href, parseRoute, sharedText } from "./app";
import { filterParams } from "./feed";

describe("routes", () => {
  it("parses feed, search, trash, item and token routes", () => {
    expect(parseRoute("")).toEqual(FEED);
    expect(parseRoute("#/?q=Tasche%20OR%20Keller&trashed=1&kind=url&tag=todo&from=2026-09-01&to=bad")).toEqual({
      name: "feed",
      query: "Tasche OR Keller",
      trashed: true,
      filters: { ...EMPTY_FILTERS, kind: "url", tag: "todo", from: "2026-09-01" },
    });
    expect(parseRoute("#/organize")).toEqual({ name: "organize" });
    expect(parseRoute("#/share?text=hi&files=2")).toEqual({ name: "share", text: "hi", files: 2 });
    expect(parseRoute("#/share?files=-3")).toEqual({ name: "share", text: "", files: 0 });
    expect(parseRoute("#/items/01a0de51-0fcc-7089-ae37-1a0e48153325")).toEqual({
      name: "item",
      id: "01a0de51-0fcc-7089-ae37-1a0e48153325",
    });
    expect(parseRoute("#/items/../etc")).toEqual(FEED);
    expect(parseRoute("#/tokens")).toEqual({ name: "tokens" });
  });

  it("round-trips through href", () => {
    for (const route of [
      FEED,
      { ...FEED, query: "a b&c", trashed: true },
      { ...FEED, filters: { kind: "image", tag: "Wohnung & Keller", collection: "Umzug 2026", from: "2026-01-01", to: "2026-12-31" } },
      { name: "item", id: "01a0de51-0fcc-7089-ae37-1a0e48153325" } as const,
      { name: "organize" } as const,
      { name: "tokens" } as const,
      { name: "share", text: "a&b", files: 1 } as const,
    ]) {
      expect(parseRoute(href(route))).toEqual(route);
    }
  });

  it("turns date filters into timezone-aware day boundaries", () => {
    const params = filterParams({ ...EMPTY_FILTERS, kind: "text", from: "2026-09-26", to: "2026-09-26" });
    expect(params["kind"]).toBe("text");
    expect(new Date(params["to"] ?? "").getTime() - new Date(params["from"] ?? "").getTime()).toBe(86_400_000);
    expect(filterParams(EMPTY_FILTERS)).toEqual({});
  });
});

describe("snapshot preview", () => {
  it("injects a no-network policy before the page's own head content", async () => {
    const { sandboxedDocument } = await import("./item");
    const doc = sandboxedDocument("<html><head><script src=x></script></head><body>hi</body></html>");
    expect(doc.indexOf("Content-Security-Policy")).toBeLessThan(doc.indexOf("<script"));
    expect(doc).toContain("default-src 'none'");
    expect(sandboxedDocument("no head").startsWith("<meta http-equiv")).toBe(true);
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
