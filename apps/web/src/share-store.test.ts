import { afterEach, describe, expect, it, vi } from "vitest";

import {
  BatchGoneError,
  MissingFilesError,
  SHARE_CACHE,
  derivedKey,
  describeBatch,
  discardBatch,
  listBatches,
  loadBatch,
  missingFiles,
  remainingFiles,
  saveBatch,
  type BatchSenders,
  type ShareBatch,
} from "./share-store";
import { FakeCaches, FakeServer, startWorker } from "./test-support";

function setup() {
  const caches = new FakeCaches();
  vi.stubGlobal("caches", caches);
  return { caches, worker: startWorker(caches), server: new FakeServer() };
}

afterEach(() => vi.unstubAllGlobals());

/** Senders that record what the server stored, as [kind, content, note] / [name, content, note]. */
async function senders(server: FakeServer): Promise<BatchSenders> {
  return {
    text: (content, note, key) => server.capture(["text", content, note], key),
    url: (url, note, key) => server.capture(["url", url, note], key),
    file: async (file, note, key) => server.capture([file.name, await file.text(), note], key),
  };
}

async function share(worker: ReturnType<typeof startWorker>, ...fields: [string, string | File][]): Promise<ShareBatch> {
  const { hash } = await worker.post(fields);
  const batch = await loadBatch(new URLSearchParams(hash.split("?")[1]).get("batch") ?? "");
  if (batch === null) throw new Error("batch missing");
  return batch;
}

const file = (name: string, body: string) => ["files", new File([body], name, { type: "text/plain" })] as [string, File];

describe("saving a pending share", () => {
  it("saves content and files once and then removes the batch", async () => {
    const { worker, server, caches } = setup();
    const batch = await share(worker, file("a.txt", "eins"), file("b.txt", "zwei"), ["text", "Notiz"]);
    expect(await saveBatch(batch, "Notiz", "Gedanke", await senders(server))).toEqual({ duplicate: null, saved: 2 });
    expect(server.items).toEqual([
      ["text", "Notiz", "Gedanke"],
      ["a.txt", "eins", "Gedanke"],
      ["b.txt", "zwei", "Gedanke"],
    ]);
    expect(caches.paths(SHARE_CACHE)).toEqual([]);
    expect(await loadBatch(batch.id)).toBeNull();
  });

  it("keeps the batch after a lost response and a retry replays the same keys, creating no duplicate", async () => {
    const { worker, server, caches } = setup();
    const batch = await share(worker, file("a.txt", "eins"), ["url", "https://example.org/x"]);
    const send = await senders(server);
    server.next("ok", "lost"); // link saved; the file is stored but its response is lost
    await expect(saveBatch(batch, "https://example.org/x", null, send)).rejects.toThrow();
    expect(caches.paths(SHARE_CACHE)).toHaveLength(2);
    expect(await remainingFiles(batch)).toHaveLength(1);

    await saveBatch(batch, "https://example.org/x", null, send);
    expect(server.items).toEqual([
      ["url", "https://example.org/x", null],
      ["a.txt", "eins", null],
    ]);
    expect(server.requests).toHaveLength(4);
    expect(server.requests[0]).toBe(server.requests[2]);
    expect(server.requests[1]).toBe(server.requests[3]);
    expect(caches.paths(SHARE_CACHE)).toEqual([]);
  });

  it("retries after a network error with the same keys", async () => {
    const { worker, server } = setup();
    const batch = await share(worker, ["text", "Idee"]);
    server.next("offline");
    await expect(saveBatch(batch, "Idee", null, await senders(server))).rejects.toThrow();
    expect(await loadBatch(batch.id)).not.toBeNull();
    await saveBatch(batch, "Idee", null, await senders(server));
    expect(server.items).toHaveLength(1);
    expect(server.requests[0]).toBe(server.requests[1]);
  });

  it("after a partial failure only resends unconfirmed files, with the same file IDs, also after reopening the page", async () => {
    const { worker, server, caches } = setup();
    const batch = await share(worker, file("a.txt", "eins"), file("b.txt", "zwei"), file("c.txt", "drei"));
    server.next("ok", "lost");
    await expect(saveBatch(batch, "", null, await senders(server))).rejects.toThrow();
    expect(server.items).toHaveLength(2);
    expect((await remainingFiles(batch)).map((entry) => entry.name)).toEqual(["b.txt", "c.txt"]);

    // "Reopen the page": nothing but the cache is left; read the batch back and retry.
    const reopened = (await listBatches())[0]!;
    expect(reopened).toEqual(batch);
    await saveBatch(reopened, "", null, await senders(server));
    expect(server.items.map((item) => (item as string[])[0])).toEqual(["a.txt", "b.txt", "c.txt"]);
    // a once; b twice with one key; c once.
    expect(server.requests).toHaveLength(4);
    expect(server.requests[1]).toBe(server.requests[2]);
    expect(new Set(server.requests).size).toBe(3);
    expect(caches.paths(SHARE_CACHE)).toEqual([]);
  });

  it("uses a new key for edited content or note, never an old key for another payload", async () => {
    const { worker, server } = setup();
    const batch = await share(worker, file("a.txt", "eins"), ["text", "Original"]);
    server.next("lost");
    await expect(saveBatch(batch, "Original", null, await senders(server))).rejects.toThrow();
    server.next("lost");
    await expect(saveBatch(batch, "Geändert", null, await senders(server))).rejects.toThrow();
    server.next("lost");
    await expect(saveBatch(batch, "Original", "mit Notiz", await senders(server))).rejects.toThrow();
    expect(new Set(server.requests).size).toBe(3);
    expect(server.items).toEqual([
      ["text", "Original", null],
      ["text", "Geändert", null],
      ["text", "Original", "mit Notiz"],
    ]);
    // The original input again replays its first key and item.
    await expect(saveBatch(batch, "Original", null, await senders(server))).resolves.toMatchObject({ saved: 1 });
    expect(server.requests[3]).toBe(server.requests[0]);
  });

  it("keeps different files with identical name, size and type in different shares apart", async () => {
    const { worker, server } = setup();
    const first = await share(worker, file("foto.txt", "aaaa"));
    const second = await share(worker, file("foto.txt", "bbbb"));
    server.next("lost");
    await expect(saveBatch(first, "", null, await senders(server))).rejects.toThrow();
    await saveBatch(second, "", null, await senders(server));
    await saveBatch(first, "", null, await senders(server));
    expect(server.items).toEqual([
      ["foto.txt", "aaaa", null],
      ["foto.txt", "bbbb", null],
    ]);
  });

  it("never counts a file as saved whose bytes are gone without server confirmation", async () => {
    const { worker, server, caches } = setup();
    const batch = await share(worker, file("a.txt", "eins"), file("b.txt", "zwei"), file("c.txt", "drei"));
    // Cache loss (eviction, cleared site data) removes one file's bytes before it was ever sent.
    await (await caches.open(SHARE_CACHE)).delete(`/shared/${batch.id}/${batch.files[1]!.id}`);
    expect(await missingFiles(batch)).toEqual([batch.files[1]]);

    const failure = await saveBatch(batch, "", null, await senders(server)).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(MissingFilesError);
    expect((failure as MissingFilesError).names).toEqual(["b.txt"]);
    // The intact files were still saved, the batch and the lost file's record stay.
    expect(server.items).toEqual([
      ["a.txt", "eins", null],
      ["c.txt", "drei", null],
    ]);
    expect(await loadBatch(batch.id)).toEqual(batch);
    expect((await remainingFiles(batch)).map((entry) => entry.name)).toEqual(["b.txt"]);
    expect(await missingFiles(batch)).toEqual([batch.files[1]]);

    // Saving again neither resends the confirmed files nor pretends b.txt exists.
    await expect(saveBatch(batch, "", null, await senders(server))).rejects.toBeInstanceOf(MissingFilesError);
    expect(server.requests).toHaveLength(2);

    // Only an explicit discard removes it, including the confirmation markers.
    await discardBatch(batch);
    expect(caches.paths(SHARE_CACHE)).toEqual([]);
  });

  it("tells a confirmed file from a lost one after a partial failure and a reload", async () => {
    const { worker, server, caches } = setup();
    const batch = await share(worker, file("a.txt", "eins"), file("b.txt", "zwei"), file("c.txt", "drei"));
    server.next("ok", "offline");
    await expect(saveBatch(batch, "", null, await senders(server))).rejects.toThrow(TypeError);
    // a is confirmed (bytes deleted); now b is also lost from the cache.
    await (await caches.open(SHARE_CACHE)).delete(`/shared/${batch.id}/${batch.files[1]!.id}`);

    const reopened = (await listBatches())[0]!;
    await expect(saveBatch(reopened, "", null, await senders(server))).rejects.toBeInstanceOf(MissingFilesError);
    // c was sent now, a never again, b never.
    expect(server.items.map((item) => (item as string[])[0])).toEqual(["a.txt", "c.txt"]);
    expect((await remainingFiles(reopened)).map((entry) => entry.name)).toEqual(["b.txt"]);
    expect(await loadBatch(batch.id)).not.toBeNull();
  });

  it("does not resend a confirmed file whose cleanup was interrupted, and finishes the batch", async () => {
    const { worker, server, caches } = setup();
    const batch = await share(worker, file("a.txt", "eins"));
    // Confirmed by the server, marker written, then the page died before the bytes were deleted.
    await senders(server).then((send) => send.file(new File(["eins"], "a.txt"), null, "interrupted"));
    await (await caches.open(SHARE_CACHE)).put(`/shared/${batch.id}/${batch.files[0]!.id}.saved`, new Response("saved"));
    const before = server.requests.length;
    await expect(saveBatch(batch, "", null, await senders(server))).resolves.toMatchObject({ saved: 0 });
    expect(server.requests).toHaveLength(before);
    expect(caches.paths(SHARE_CACHE)).toEqual([]);
  });

  it("keeps the batch when the confirmation marker cannot be written, and a retry creates no duplicate", async () => {
    const { worker, server, caches } = setup();
    const batch = await share(worker, file("a.txt", "eins"));
    caches.failPut = (_, key) => key.endsWith(".saved");
    await expect(saveBatch(batch, "", null, await senders(server))).rejects.toThrow("QuotaExceededError");
    caches.failPut = null;
    expect(await remainingFiles(batch)).toHaveLength(1);
    await saveBatch(batch, "", null, await senders(server));
    expect(server.items).toHaveLength(1);
    expect(server.requests[0]).toBe(server.requests[1]);
    expect(await listBatches()).toEqual([]);
  });

  it("serializes concurrent saves of one batch: everything is sent once, the loser learns the batch is gone", async () => {
    const { worker, server } = setup();
    const batch = await share(worker, file("a.txt", "eins"), file("b.txt", "zwei"), ["text", "Text"]);
    const slow = await senders(server);
    const send: BatchSenders = {
      text: async (...args) => (await new Promise((resolve) => setTimeout(resolve, 5)), slow.text(...args)),
      url: slow.url,
      file: async (...args) => (await new Promise((resolve) => setTimeout(resolve, 5)), slow.file(...args)),
    };
    const results = await Promise.allSettled([
      saveBatch(batch, "Text", null, send),
      saveBatch(batch, "Text", null, send),
      saveBatch(batch, "Text", null, send),
    ]);
    expect(results.filter((result) => result.status === "fulfilled")).toHaveLength(1);
    for (const result of results) {
      if (result.status === "rejected") expect(result.reason).toBeInstanceOf(BatchGoneError);
    }
    expect(server.requests).toHaveLength(3);
    expect(server.items).toHaveLength(3);
    expect(await listBatches()).toEqual([]);
  });

  it("lets a concurrent save continue after the first one failed, without repeating confirmed files", async () => {
    const { worker, server } = setup();
    const batch = await share(worker, file("a.txt", "eins"), file("b.txt", "zwei"));
    server.next("ok", "offline");
    const send = await senders(server);
    const [first, second] = await Promise.allSettled([
      saveBatch(batch, "", null, send),
      saveBatch(batch, "", null, send),
    ]);
    expect(first.status).toBe("rejected");
    expect(second.status).toBe("fulfilled");
    expect(server.items.map((item) => (item as string[])[0])).toEqual(["a.txt", "b.txt"]);
    expect(server.requests).toHaveLength(3);
    expect(await listBatches()).toEqual([]);
  });

  it("refuses to save a batch that was discarded in the meantime and sends nothing", async () => {
    const { worker, server } = setup();
    const batch = await share(worker, file("a.txt", "eins"), ["text", "Text"]);
    await discardBatch(batch);
    await expect(saveBatch(batch, "Text", null, await senders(server))).rejects.toBeInstanceOf(BatchGoneError);
    expect(server.requests).toEqual([]);
  });

  it("derives the same UUID-shaped key for the same inputs only", async () => {
    const key = await derivedKey("file", "b", "f", null);
    expect(key).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(await derivedKey("file", "b", "f", null)).toBe(key);
    expect(await derivedKey("file", "b", "f", "n")).not.toBe(key);
    expect(await derivedKey("file", "b2", "f", null)).not.toBe(key);
  });
});

describe("several pending shares", () => {
  it("saving the second share leaves the first one complete and reachable", async () => {
    const { worker, server } = setup();
    const first = await share(worker, file("a.txt", "erste"), ["text", "Erster Text"]);
    const second = await share(worker, file("a.txt", "zweite"), ["text", "Zweiter Text"]);
    expect((await listBatches()).map((batch) => batch.id).sort()).toEqual([first.id, second.id].sort());

    await saveBatch(second, "Zweiter Text", null, await senders(server));
    const left = await listBatches();
    expect(left).toEqual([first]);
    expect(await remainingFiles(first)).toEqual(first.files);

    await saveBatch(first, "Erster Text", null, await senders(server));
    expect(server.items).toEqual([
      ["text", "Zweiter Text", null],
      ["a.txt", "zweite", null],
      ["text", "Erster Text", null],
      ["a.txt", "erste", null],
    ]);
    expect(await listBatches()).toEqual([]);
  });

  it("lists newest first and describes a batch by its content or first file", async () => {
    const { worker } = setup();
    const older = await share(worker, file("scan.pdf", "x"));
    await new Promise((resolve) => setTimeout(resolve, 5));
    const newer = await share(worker, ["text", "Ein langer Text ".repeat(10)]);
    expect((await listBatches()).map((batch) => batch.id)).toEqual([newer.id, older.id]);
    expect(describeBatch(older)).toBe("scan.pdf");
    expect(describeBatch(newer).length).toBeLessThanOrEqual(80);
  });

  it("discards exactly the chosen batch, files first and meta last", async () => {
    const { worker, caches } = setup();
    const keep = await share(worker, file("keep.txt", "k"));
    const drop = await share(worker, file("drop.txt", "d"), ["text", "weg"]);
    await discardBatch(drop);
    expect(await loadBatch(drop.id)).toBeNull();
    expect(await listBatches()).toEqual([keep]);
    expect(caches.paths(SHARE_CACHE).every((path) => path.startsWith(`/shared/${keep.id}/`))).toBe(true);
  });

  it("does not delete anything when saving fails or the page is merely reopened", async () => {
    const { worker, server, caches } = setup();
    const batch = await share(worker, file("a.txt", "eins"), ["text", "Text"]);
    const stored = caches.paths(SHARE_CACHE);
    server.next("offline");
    await expect(saveBatch(batch, "Text", null, await senders(server))).rejects.toThrow();
    await listBatches();
    await loadBatch(batch.id);
    expect(caches.paths(SHARE_CACHE)).toEqual(stored);
  });

  it("ignores malformed meta entries and unknown batch IDs", async () => {
    const { caches } = setup();
    const cache = await caches.open(SHARE_CACHE);
    await cache.put("/shared/11111111-1111-4111-8111-111111111111/meta", new Response("{not json"));
    await cache.put(
      "/shared/22222222-2222-4222-8222-222222222222/meta",
      new Response(JSON.stringify({ id: "22222222-2222-4222-8222-222222222222", files: "x" })),
    );
    expect(await listBatches()).toEqual([]);
    expect(await loadBatch("11111111-1111-4111-8111-111111111111")).toBeNull();
    expect(await loadBatch("not-a-batch")).toBeNull();
  });
});
