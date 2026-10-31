import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import { uploadSharedFiles } from "./feed";
import { CaptureKeys, uploadFiles, type PendingFile } from "./retry";

type Outcome = "ok" | "lost" | "offline";

/** Mirrors the server contract: a known key replays the first item, whatever the payload. */
class FakeServer {
  readonly items: unknown[] = [];
  readonly byKey = new Map<string, { payload: unknown; id: number }>();
  readonly requests: string[] = [];
  private readonly outcomes: Outcome[] = [];

  next(...outcomes: Outcome[]): void {
    this.outcomes.push(...outcomes);
  }

  async capture(payload: unknown, key: string): Promise<{ id: number }> {
    this.requests.push(key);
    const outcome = this.outcomes.shift() ?? "ok";
    if (outcome === "offline") throw new TypeError("Failed to fetch");
    let stored = this.byKey.get(key);
    if (stored === undefined) {
      stored = { payload, id: this.items.push(payload) };
      this.byKey.set(key, stored);
    }
    // Committed, but the response never reaches the client.
    if (outcome === "lost") throw new TypeError("Failed to fetch");
    return { id: stored.id };
  }
}

let counter = 0;
const keys = () => new CaptureKeys(() => `key-${++counter}`);

/** The text/URL save of the capture form: key, send, confirm only on success. */
async function save(server: FakeServer, captureKeys: CaptureKeys, ...payload: unknown[]) {
  const key = captureKeys.key(...payload);
  const result = await server.capture(payload, key);
  captureKeys.confirm(...payload);
  return result;
}

describe("text and URL retries", () => {
  it("replays the same key after a lost response, so the retry stores nothing new", async () => {
    const server = new FakeServer();
    const captureKeys = keys();
    server.next("lost");
    await expect(save(server, captureKeys, "text", "Idee", null)).rejects.toThrow();
    expect(await save(server, captureKeys, "text", "Idee", null)).toEqual({ id: 1 });
    expect(server.items).toHaveLength(1);
    expect(new Set(server.requests).size).toBe(1);
  });

  it("retries a URL after a network error with the same key", async () => {
    const server = new FakeServer();
    const captureKeys = keys();
    server.next("offline", "offline");
    await expect(save(server, captureKeys, "url", "https://example.org/", "n")).rejects.toThrow();
    await expect(save(server, captureKeys, "url", "https://example.org/", "n")).rejects.toThrow();
    expect(server.items).toHaveLength(0);
    expect(await save(server, captureKeys, "url", "https://example.org/", "n")).toEqual({ id: 1 });
    expect(server.items).toHaveLength(1);
    expect(new Set(server.requests).size).toBe(1);
  });

  it("gives a deliberate repeat after a confirmed save a new key", async () => {
    const server = new FakeServer();
    const captureKeys = keys();
    await save(server, captureKeys, "text", "Idee", null);
    await save(server, captureKeys, "text", "Idee", null);
    expect(server.items).toHaveLength(2);
    expect(new Set(server.requests).size).toBe(2);
  });

  it("never reuses an uncertain key for changed input", async () => {
    const server = new FakeServer();
    const captureKeys = keys();
    server.next("lost");
    await expect(save(server, captureKeys, "text", "Idee", null)).rejects.toThrow();
    // Edited text, edited note and a switch to URL capture are all new payloads.
    await save(server, captureKeys, "text", "Idee!", null);
    await save(server, captureKeys, "text", "Idee", "mit Notiz");
    await save(server, captureKeys, "url", "Idee", null);
    expect(server.items).toEqual([
      ["text", "Idee", null],
      ["text", "Idee!", null],
      ["text", "Idee", "mit Notiz"],
      ["url", "Idee", null],
    ]);
    for (const { payload } of server.byKey.values()) expect(server.items).toContainEqual(payload);
    expect(new Set(server.requests).size).toBe(4);
    // Going back to the unconfirmed input reuses its key and replays the stored item.
    expect(await save(server, captureKeys, "text", "Idee", null)).toEqual({ id: 1 });
  });
});

describe("file batches", () => {
  const files = (...names: string[]): PendingFile[] =>
    names.map((name) => ({ file: new File([name], name, { type: "text/plain" }), note: "Beleg" }));

  it("uploads every file once and reports nothing pending", async () => {
    const server = new FakeServer();
    const batch = await uploadFiles(files("a", "b"), keys(), (file, note, key) =>
      server.capture([file.name, note], key),
    );
    expect(batch).toEqual({ saved: 2, pending: [], error: null });
    expect(server.items).toHaveLength(2);
  });

  it("retries only unconfirmed files after a partial failure, with their original keys", async () => {
    const server = new FakeServer();
    const captureKeys = keys();
    const upload = (file: File, note: string | null, key: string) => server.capture([file.name, note], key);
    const selection = files("a", "b", "c");
    server.next("ok", "lost");
    const first = await uploadFiles(selection, captureKeys, upload);
    expect(first.saved).toBe(1);
    expect(first.pending.map(({ file }) => file.name)).toEqual(["b", "c"]);
    expect(first.error).toBeInstanceOf(TypeError);

    const second = await uploadFiles(first.pending, captureKeys, upload);
    expect(second).toEqual({ saved: 2, pending: [], error: null });
    // a was never resent, b replayed its key, c was sent once.
    const [a, b, bAgain, c] = server.requests;
    expect(server.requests).toHaveLength(4);
    expect(bAgain).toBe(b);
    expect(new Set([a, b, c]).size).toBe(3);
    expect(server.items).toEqual([["a", "Beleg"], ["b", "Beleg"], ["c", "Beleg"]]);
  });

  it("keeps the failed and unattempted files pending after a network error", async () => {
    const server = new FakeServer();
    server.next("offline");
    const batch = await uploadFiles(files("a", "b"), keys(), (file, note, key) =>
      server.capture([file.name, note], key),
    );
    expect(batch.saved).toBe(0);
    expect(batch.pending.map(({ file }) => file.name)).toEqual(["a", "b"]);
    expect(server.items).toHaveLength(0);
  });

  it("treats a newly picked file, or the same file with another note, as a new capture", () => {
    const captureKeys = keys();
    const [entry] = files("a");
    const picked = entry!.file;
    const again = new File(["a"], "a", { type: "text/plain", lastModified: picked.lastModified });
    const first = captureKeys.key(captureKeys.file(picked), "Beleg");
    expect(captureKeys.key(captureKeys.file(picked), "Beleg")).toBe(first);
    expect(captureKeys.key(captureKeys.file(again), "Beleg")).not.toBe(first);
    expect(captureKeys.key(captureKeys.file(picked), "anders")).not.toBe(first);
  });
});

describe("shared file retries", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("replays the key of a shared file whose response was lost and forgets it once confirmed", async () => {
    const store = new Map<string, Response>([
      ["/shared/count", new Response("2")],
      ["/shared/0", new Response("eins", { headers: { "Content-Type": "text/plain", "X-Bag-Filename": "a.txt" } })],
      ["/shared/1", new Response("zwei", { headers: { "Content-Type": "text/plain", "X-Bag-Filename": "b.txt" } })],
    ]);
    // Cache reads consume bodies, so hand out clones like the Cache API does.
    vi.stubGlobal("caches", {
      open: async () => ({
        match: async (key: string) => store.get(key)?.clone(),
        delete: async (key: string) => store.delete(key),
      }),
    });
    const server = new FakeServer();
    const sent: string[] = [];
    vi.spyOn(api, "captureFile").mockImplementation(async (file, note, key) => {
      sent.push(`${file.name}:${key}`);
      await server.capture([file.name, note], key);
      return { id: "x", status: "stored", processing_status: "queued", duplicate_of: null };
    });
    const captureKeys = keys();
    server.next("ok", "lost");
    await expect(uploadSharedFiles("Notiz", captureKeys)).rejects.toThrow();
    expect(store.has("/shared/0")).toBe(false);
    expect(store.has("/shared/1")).toBe(true);

    expect(await uploadSharedFiles("Notiz", captureKeys)).toBe(1);
    expect(sent.map((entry) => entry.split(":")[0])).toEqual(["a.txt", "b.txt", "b.txt"]);
    expect(sent[1]).toBe(sent[2]);
    expect(server.items).toEqual([["a.txt", "Notiz"], ["b.txt", "Notiz"]]);
    expect(store.size).toBe(0);
  });
});

describe("API client", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("sends the caller's client_capture_id for text, URL and file captures", async () => {
    const bodies: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_path: string, init: RequestInit) => {
        bodies.push(init.body instanceof FormData ? JSON.parse(String(init.body.get("metadata"))) : JSON.parse(String(init.body)));
        return new Response(JSON.stringify({ id: "1", status: "stored", processing_status: "queued", duplicate_of: null }), {
          status: 201,
        });
      }),
    );
    await api.captureText("t", null, "11111111-1111-4111-8111-111111111111");
    await api.captureUrl("https://example.org/", "n", "22222222-2222-4222-8222-222222222222");
    await api.captureFile(new File(["f"], "f.txt"), null, "33333333-3333-4333-8333-333333333333");
    expect(bodies.map((body) => (body as { client_capture_id: string }).client_capture_id)).toEqual([
      "11111111-1111-4111-8111-111111111111",
      "22222222-2222-4222-8222-222222222222",
      "33333333-3333-4333-8333-333333333333",
    ]);
  });
});
