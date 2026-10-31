import { afterEach, describe, expect, it, vi } from "vitest";

import { looksLikeUrl } from "./api";
import { href, parseRoute, sharedRoute } from "./app";
import { shareDraft, type SharePayload } from "./share";
import { SHARE_CACHE, batchDraft, listBatches, loadBatch } from "./share-store";
import { FakeCaches, startWorker } from "./test-support";

const payload = (fields: Partial<SharePayload>): SharePayload => ({ url: [], title: [], text: [], ...fields });

function setup() {
  const caches = new FakeCaches();
  vi.stubGlobal("caches", caches);
  return { caches, worker: startWorker(caches) };
}

afterEach(() => vi.unstubAllGlobals());

/** What the capture form would send after a POST share: the stored batch, classified by the app. */
async function captureAfterShare(fields: [string, string | File][]) {
  const { worker } = setup();
  const { hash } = await worker.post(fields);
  const route = parseRoute(hash);
  if (route.name !== "share" || route.batch === null) throw new Error(`unexpected route ${hash}`);
  const batch = await loadBatch(route.batch);
  if (batch === null) throw new Error("batch was not stored");
  const draft = batchDraft(batch);
  return { endpoint: looksLikeUrl(draft.content) ? "url" : "text", ...draft, files: batch.files.length };
}

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
    ).toEqual({ endpoint: "text", content: "Einkauf\nMilch\r\nBrot", note: "", files: 0 });
  });

  it("stores repeated fields and Unicode without dropping any value, skipping blank ones", async () => {
    const { worker } = setup();
    const { hash } = await worker.post([
      ["text", "erste Zeile"],
      ["text", "zweite 💼 & Zeile"],
      ["title", "   "],
    ]);
    const route = parseRoute(hash);
    const batch = await loadBatch(route.name === "share" && route.batch ? route.batch : "");
    expect(batch).toMatchObject({ url: [], title: [], text: ["erste Zeile", "zweite 💼 & Zeile"], files: [] });
  });

  it("stores files with the share in one batch with file IDs, and redirects to it", async () => {
    const { worker, caches } = setup();
    const result = await worker.post([
      ["files", new File(["%PDF-1.7"], "Beleg.pdf", { type: "application/pdf" })],
      ["files", new File(["zwei"], "roh.bin")],
      ["url", "https://example.org/x"],
      ["title", "Beleg"],
    ]);
    expect(result.status).toBe(303);
    expect(result.path).toBe("/");
    const route = parseRoute(result.hash);
    expect(route).toMatchObject({ name: "share", batch: expect.stringMatching(/^[0-9a-f-]{36}$/) });
    const batch = (await listBatches())[0]!;
    expect(batch).toMatchObject({ url: ["https://example.org/x"], title: ["Beleg"], text: [] });
    expect(batch.files).toEqual([
      { id: expect.stringMatching(/^[0-9a-f-]{36}$/), name: "Beleg.pdf", type: "application/pdf", size: 8 },
      { id: expect.stringMatching(/^[0-9a-f-]{36}$/), name: "roh.bin", type: "application/octet-stream", size: 4 },
    ]);
    expect(caches.paths(SHARE_CACHE)).toEqual(
      [`/shared/${batch.id}/meta`, ...batch.files.map((file) => `/shared/${batch.id}/${file.id}`)].sort(),
    );
  });

  it("redirects an empty share to the feed and stores nothing", async () => {
    const { worker, caches } = setup();
    const { hash } = await worker.post([["text", "  "]]);
    expect(hash).toBe("#/");
    expect(caches.paths(SHARE_CACHE)).toEqual([]);
  });

  it("gives every received file and every share its own identity, even for identical files", async () => {
    const { worker } = setup();
    const lookAlike = () => new File(["same"], "foto.jpg", { type: "image/jpeg" });
    await worker.post([
      ["files", lookAlike()],
      ["files", lookAlike()],
    ]);
    await worker.post([["files", lookAlike()]]);
    const batches = await listBatches();
    expect(new Set(batches.map((batch) => batch.id)).size).toBe(2);
    const fileIds = batches.flatMap((batch) => batch.files.map((file) => file.id));
    expect(fileIds).toHaveLength(3);
    expect(new Set(fileIds).size).toBe(3);
  });

  it("does not let a second share overwrite the files or text of the first", async () => {
    const { worker, caches } = setup();
    const first = await worker.post([
      ["files", new File(["erste Datei"], "a.txt", { type: "text/plain" })],
      ["text", "https://example.org/eins"],
      ["title", "Erster"],
    ]);
    const second = await worker.post([
      ["files", new File(["zweite Datei"], "a.txt", { type: "text/plain" })],
      ["text", "https://example.org/zwei"],
      ["title", "Zweiter"],
    ]);
    expect(first.hash).not.toBe(second.hash);
    const batches = await listBatches();
    expect(batches).toHaveLength(2);
    const bodies = new Map<string, string>();
    for (const batch of batches) {
      const cache = await caches.open(SHARE_CACHE);
      const stored = await cache.match(`/shared/${batch.id}/${batch.files[0]!.id}`);
      bodies.set(batch.text[0]!, await stored!.text());
    }
    expect(bodies).toEqual(
      new Map([
        ["https://example.org/eins", "erste Datei"],
        ["https://example.org/zwei", "zweite Datei"],
      ]),
    );
    expect(batches.map((batch) => batch.title[0]).sort()).toEqual(["Erster", "Zweiter"]);
  });

  it("keeps all batches when many shares are written concurrently", async () => {
    const { worker } = setup();
    const results = await Promise.all(
      Array.from({ length: 8 }, (_, index) =>
        worker.post([
          ["files", new File([`inhalt ${index}`], "scan.pdf", { type: "application/pdf" })],
          ["text", `Notiz ${index}`],
        ]),
      ),
    );
    expect(new Set(results.map((result) => result.hash)).size).toBe(8);
    const batches = await listBatches();
    expect(batches).toHaveLength(8);
    expect(new Set(batches.flatMap((batch) => batch.files.map((file) => file.id))).size).toBe(8);
    expect(batches.map((batch) => batch.text[0]).sort()).toEqual(Array.from({ length: 8 }, (_, i) => `Notiz ${i}`));
  });

  it("removes a half-written batch and reports the failure instead of acknowledging it", async () => {
    const { worker, caches } = setup();
    await worker.post([["text", "bleibt"]]);
    const before = caches.paths(SHARE_CACHE);
    caches.failPut = (_, key) => key.endsWith("/meta");
    await expect(worker.post([["files", new File(["x"], "x.txt")], ["text", "neu"]])).rejects.toThrow();
    caches.failPut = null;
    expect(caches.paths(SHARE_CACHE)).toEqual(before);
    expect(await listBatches()).toHaveLength(1);
  });

  it("ignores batches whose meta entry was never written", async () => {
    const { caches } = setup();
    const cache = await caches.open(SHARE_CACHE);
    await cache.put("/shared/11111111-1111-4111-8111-111111111111/22222222-2222-4222-8222-222222222222", new Response("x"));
    expect(await listBatches()).toEqual([]);
  });
});

describe("service worker activation", () => {
  it("removes stale shell caches but keeps pending shares across an app update", async () => {
    const { caches, worker } = setup();
    await worker.post([
      ["files", new File(["offen"], "offen.txt", { type: "text/plain" })],
      ["text", "Begleittext"],
    ]);
    await (await caches.open("bag-shell-v0")).put("/index.html", new Response("alt"));
    const before = await listBatches();
    const stored = caches.paths(SHARE_CACHE);

    // A new worker version installs and activates over the old caches.
    const updated = startWorker(caches);
    await updated.install();
    await updated.activate();

    expect(await caches.keys()).toEqual(expect.arrayContaining([SHARE_CACHE, "bag-shell-v1"]));
    expect(await caches.keys()).not.toContain("bag-shell-v0");
    expect(caches.paths(SHARE_CACHE)).toEqual(stored);
    expect(await listBatches()).toEqual(before);
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
  it("maps /share query fields onto the inline share route", () => {
    expect(sharedRoute("/", "?text=x")).toBeNull();
    const route = sharedRoute("/share", "?title=Titel&text=https%3A%2F%2Fexample.org%2Fb&text=noch");
    expect(route).toBe(
      href({
        name: "share",
        batch: null,
        payload: payload({ title: ["Titel"], text: ["https://example.org/b", "noch"] }),
      }),
    );
    const parsed = parseRoute(route ?? "");
    expect(parsed).toMatchObject({ name: "share", batch: null });
    expect(shareDraft((parsed as { payload: SharePayload }).payload)).toEqual({
      content: "https://example.org/b",
      note: "Titel\nnoch",
    });
  });
});
