import { describe, expect, it } from "vitest";

import swSource from "../public/sw.js?raw";
import { looksLikeUrl } from "./api";
import { href, parseRoute, sharedRoute } from "./app";
import { shareDraft, type SharePayload } from "./share";

const ORIGIN = "https://bag.test";

class TestResponse extends Response {
  // Browsers resolve a relative redirect against the worker's origin; Node needs it absolute.
  static override redirect(url: string | URL, status?: number): Response {
    return Response.redirect(new URL(url, ORIGIN).href, status);
  }
}

/** Runs the real service worker on a multipart POST /share and returns its redirect and cache. */
async function postShare(fields: [string, string | File][]) {
  const listeners: Record<string, (event: unknown) => void> = {};
  const store = new Map<string, Response>();
  const caches = {
    open: async () => ({
      put: async (key: string, value: Response) => void store.set(key, value),
      match: async (key: string) => store.get(key),
      delete: async (key: string) => store.delete(key),
    }),
  };
  const self = {
    addEventListener: (name: string, listener: (event: unknown) => void) => (listeners[name] = listener),
  };
  new Function("self", "caches", "location", "Response", swSource)(self, caches, new URL(ORIGIN), TestResponse);
  const form = new FormData();
  for (const [name, value] of fields) form.append(name, value);
  let responded: Promise<Response> | undefined;
  listeners["fetch"]?.({
    request: new Request(`${ORIGIN}/share`, { method: "POST", body: form }),
    respondWith: (value: Promise<Response>) => (responded = value),
  });
  if (responded === undefined) throw new Error("service worker did not handle POST /share");
  const response = await responded;
  const location = new URL(response.headers.get("Location") ?? "");
  return { status: response.status, path: location.pathname, hash: location.hash, store };
}

/** What the capture form would send after a POST share: endpoint, content and note. */
async function captureAfterShare(fields: [string, string | File][]) {
  const { hash } = await postShare(fields);
  const route = parseRoute(hash);
  if (route.name !== "share") throw new Error(`unexpected route ${route.name}`);
  const draft = shareDraft(route.payload);
  return { endpoint: looksLikeUrl(draft.content) ? "url" : "text", ...draft, files: route.files };
}

const payload = (fields: Partial<SharePayload>): SharePayload => ({ url: [], title: [], text: [], ...fields });

describe("POST share target through the service worker", () => {
  it("captures a URL in text plus a title as a link and keeps the title", async () => {
    // The combination the review flagged: previously joined into a two-line text item.
    expect(
      await captureAfterShare([
        ["title", "Der Name des Windes"],
        ["text", "https://example.org/book"],
      ]),
    ).toEqual({ endpoint: "url", content: "https://example.org/book", note: "Der Name des Windes", files: 0 });
  });

  it("captures the url field as a link and keeps title and accompanying text", async () => {
    expect(
      await captureAfterShare([
        ["url", "https://example.org/a"],
        ["title", "Artikel"],
        ["text", "Lesenswert"],
      ]),
    ).toEqual({ endpoint: "url", content: "https://example.org/a", note: "Artikel\nLesenswert", files: 0 });
  });

  it("keeps plain text shares as text", async () => {
    expect(
      await captureAfterShare([
        ["title", "Einkauf"],
        ["text", "Milch\nBrot"],
      ]),
      // multipart/form-data normalizes line breaks inside values to CRLF, in browsers too.
    ).toEqual({ endpoint: "text", content: "Einkauf\nMilch\r\nBrot", note: "", files: 0 });
  });

  it("forwards repeated fields and Unicode without dropping any value", async () => {
    const { hash } = await postShare([
      ["text", "erste Zeile"],
      ["text", "zweite 💼 & Zeile"],
      ["title", "   "],
    ]);
    expect(parseRoute(hash)).toEqual({
      name: "share",
      payload: payload({ text: ["erste Zeile", "zweite 💼 & Zeile"] }),
      files: 0,
    });
  });

  it("stashes files and redirects to the share route", async () => {
    const result = await postShare([
      ["files", new File(["%PDF-1.7"], "Beleg.pdf", { type: "application/pdf" })],
      ["text", "https://example.org/x"],
    ]);
    expect(result.status).toBe(303);
    expect(result.path).toBe("/");
    expect(await result.store.get("/shared/count")?.text()).toBe("1");
    expect(result.store.get("/shared/0")?.headers.get("X-Bag-Filename")).toBe("Beleg.pdf");
    expect(parseRoute(result.hash)).toMatchObject({ name: "share", files: 1 });
  });

  it("redirects an empty share to an empty capture form", async () => {
    const { hash } = await postShare([]);
    expect(shareDraft((parseRoute(hash) as { payload: SharePayload }).payload)).toEqual({ content: "", note: "" });
  });
});

describe("shareDraft", () => {
  it("prefers a valid url field over links in the text", () => {
    expect(shareDraft(payload({ url: ["https://a.example/"], text: ["siehe https://b.example/"] }))).toEqual({
      content: "https://a.example/",
      note: "siehe https://b.example/",
    });
  });

  it("removes the repeated link from the accompanying text", () => {
    expect(
      shareDraft(payload({ url: ["https://a.example/x"], text: ["Schau mal https://a.example/x heute"] })),
    ).toEqual({ content: "https://a.example/x", note: "Schau mal heute" });
  });

  it("extracts a single link from text without trailing punctuation or brackets", () => {
    expect(shareDraft(payload({ text: ["Gefunden (https://example.org/a_(b)), wirklich gut."] }))).toEqual({
      content: "https://example.org/a_(b)",
      note: "Gefunden ( ), wirklich gut.",
    });
    expect(shareDraft(payload({ text: ["„https://example.org/z”"] })).content).toBe("https://example.org/z");
  });

  it("treats a title that is the link like any other field", () => {
    expect(shareDraft(payload({ title: ["https://example.org/t"], text: ["https://example.org/t"] }))).toEqual({
      content: "https://example.org/t",
      note: "",
    });
  });

  it("keeps text with several different links as text instead of guessing", () => {
    expect(shareDraft(payload({ text: ["https://a.example/ und https://b.example/"] }))).toEqual({
      content: "https://a.example/ und https://b.example/",
      note: "",
    });
  });

  it("keeps a second url field value in the note", () => {
    expect(shareDraft(payload({ url: ["https://a.example/", "https://b.example/"] }))).toEqual({
      content: "https://a.example/",
      note: "https://b.example/",
    });
  });

  it("does not turn non-http or malformed url fields into a link", () => {
    expect(shareDraft(payload({ url: ["ftp://example.org/f"], title: ["Datei"] }))).toEqual({
      content: "ftp://example.org/f\nDatei",
      note: "",
    });
    expect(shareDraft(payload({ text: ["C:\\temp https://example.org\\x"] })).content).toBe("https://example.org");
  });

  it("drops duplicates and whitespace-only values but keeps distinct text", () => {
    expect(shareDraft(payload({ title: ["Titel", "Titel"], text: [" ", "Titel", "Mehr"] }))).toEqual({
      content: "Titel\nMehr",
      note: "",
    });
  });

  it("still understands the combined text of the previous worker version", () => {
    expect(shareDraft(payload({ text: ["https://example.org/a\nArtikel\nLesenswert"] }))).toEqual({
      content: "https://example.org/a",
      note: "Artikel\nLesenswert",
    });
  });
});

describe("GET share fallback", () => {
  it("maps /share query fields onto the same share route", () => {
    expect(sharedRoute("/", "?text=x")).toBeNull();
    const route = sharedRoute("/share", "?title=Titel&text=https%3A%2F%2Fexample.org%2Fb&text=noch");
    expect(route).toBe(
      href({ name: "share", payload: payload({ title: ["Titel"], text: ["https://example.org/b", "noch"] }), files: 0 }),
    );
    expect(shareDraft((parseRoute(route ?? "") as { payload: SharePayload }).payload)).toEqual({
      content: "https://example.org/b",
      note: "Titel\nnoch",
    });
  });
});
