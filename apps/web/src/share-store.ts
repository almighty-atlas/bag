import { looksLikeUrl } from "./api";
import { shareDraft, type ShareDraft } from "./share";

/**
 * Pending shares in the Cache API. The service worker writes one batch per
 * share under /shared/<batch>/…, the app reads, saves and discards it:
 *   /shared/<batch>/meta    JSON: batch id, time, url/title/text values, file list
 *   /shared/<batch>/<file>  the file bytes (type in Content-Type)
 * The meta entry is written last, so a batch without it was never acknowledged.
 * A file entry is deleted only after the server confirmed that file; the batch
 * disappears only after everything in it was saved or the user discarded it.
 */
export const SHARE_CACHE = "bag-share";

export interface SharedFile {
  id: string;
  name: string;
  type: string;
  size: number;
}

export interface ShareBatch {
  id: string;
  created_at: string;
  url: string[];
  title: string[];
  text: string[];
  files: SharedFile[];
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const META_PATH = /^\/shared\/([0-9a-f-]{36})\/meta$/;

const strings = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every((entry) => typeof entry === "string");

function parseBatch(value: unknown): ShareBatch | null {
  if (typeof value !== "object" || value === null) return null;
  const meta = value as Record<string, unknown>;
  const files = meta["files"];
  if (typeof meta["id"] !== "string" || !UUID.test(meta["id"]) || typeof meta["created_at"] !== "string") return null;
  if (!strings(meta["url"]) || !strings(meta["title"]) || !strings(meta["text"]) || !Array.isArray(files)) return null;
  const valid = files.every(
    (file: Record<string, unknown>) =>
      typeof file === "object" &&
      file !== null &&
      typeof file["id"] === "string" &&
      UUID.test(file["id"]) &&
      typeof file["name"] === "string" &&
      typeof file["type"] === "string" &&
      typeof file["size"] === "number",
  );
  if (!valid) return null;
  return {
    id: meta["id"],
    created_at: meta["created_at"],
    url: meta["url"],
    title: meta["title"],
    text: meta["text"],
    files: files as SharedFile[],
  };
}

const metaPath = (batchId: string) => `/shared/${batchId}/meta`;
const filePath = (batchId: string, fileId: string) => `/shared/${batchId}/${fileId}`;

async function openCache(): Promise<Cache | null> {
  return "caches" in globalThis ? caches.open(SHARE_CACHE) : null;
}

async function readBatch(cache: Cache, batchId: string): Promise<ShareBatch | null> {
  const stored = await cache.match(metaPath(batchId));
  if (!stored) return null;
  try {
    return parseBatch(await stored.json());
  } catch {
    return null;
  }
}

/** Acknowledged batches, newest first. */
export async function listBatches(): Promise<ShareBatch[]> {
  const cache = await openCache();
  if (!cache) return [];
  const ids = (await cache.keys())
    .map((request) => META_PATH.exec(new URL(request.url).pathname)?.[1])
    .filter((id): id is string => id !== undefined);
  const batches = await Promise.all(ids.map((id) => readBatch(cache, id)));
  return batches
    .filter((batch): batch is ShareBatch => batch !== null)
    .sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id));
}

export async function loadBatch(batchId: string): Promise<ShareBatch | null> {
  const cache = await openCache();
  return cache && UUID.test(batchId) ? readBatch(cache, batchId) : null;
}

/** The files whose bytes are still stored, i.e. not yet confirmed by the server. */
export async function remainingFiles(batch: ShareBatch): Promise<SharedFile[]> {
  const cache = await openCache();
  if (!cache) return [];
  const present = await Promise.all(batch.files.map(async (file) => (await cache.match(filePath(batch.id, file.id))) !== undefined));
  return batch.files.filter((_, index) => present[index]);
}

async function readFile(cache: Cache, batchId: string, file: SharedFile): Promise<File | null> {
  const stored = await cache.match(filePath(batchId, file.id));
  if (!stored) return null;
  return new File([await stored.blob()], file.name, { type: file.type });
}

/** Explicit discard: files first, the meta entry last, so an interrupted discard stays visible. */
export async function discardBatch(batch: ShareBatch): Promise<void> {
  const cache = await openCache();
  if (!cache) return;
  await Promise.all(batch.files.map((file) => cache.delete(filePath(batch.id, file.id))));
  await cache.delete(metaPath(batch.id));
}

export function batchDraft(batch: Pick<ShareBatch, "url" | "title" | "text">): ShareDraft {
  return shareDraft({ url: batch.url, title: batch.title, text: batch.text });
}

/** Short label for a pending share in lists. */
export function describeBatch(batch: ShareBatch): string {
  const { content } = batchDraft(batch);
  const label = content || batch.files[0]?.name || "Geteilter Inhalt";
  return label.length > 80 ? `${label.slice(0, 79)}…` : label;
}

/**
 * Capture IDs derived from the batch, file ID and payload: identical across page
 * reloads, so an unanswered request is replayed instead of duplicated, while
 * changed input (another note or text) gets a different ID.
 */
export async function derivedKey(...parts: unknown[]): Promise<string> {
  const bytes = new TextEncoder().encode(JSON.stringify(["bag-share-v1", ...parts]));
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)).slice(0, 16);
  digest[6] = (digest[6] ?? 0) & 0x0f | 0x50;
  digest[8] = (digest[8] ?? 0) & 0x3f | 0x80;
  const hex = Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export interface BatchSenders {
  text: (content: string, note: string | null, key: string) => Promise<{ duplicate_of: string | null }>;
  url: (url: string, note: string | null, key: string) => Promise<{ duplicate_of: string | null }>;
  file: (file: File, note: string | null, key: string) => Promise<unknown>;
}

/**
 * Saves the batch's content and its remaining files, in order, and discards the
 * batch only when all of it is confirmed. The first failure is thrown; files
 * confirmed before it are already gone from the cache and are not sent again.
 * `content` is what the form currently holds (the user may have edited it).
 */
export async function saveBatch(
  batch: ShareBatch,
  content: string,
  note: string | null,
  send: BatchSenders,
): Promise<{ duplicate: string | null; saved: number }> {
  const cache = await openCache();
  if (!cache) throw new Error("Cache storage unavailable");
  let duplicate: string | null = null;
  const link = content.trim();
  if (link) {
    const isUrl = looksLikeUrl(link);
    const key = await derivedKey("content", batch.id, isUrl ? "url" : "text", isUrl ? link : content, note);
    const result = isUrl ? await send.url(link, note, key) : await send.text(content, note, key);
    duplicate = result.duplicate_of;
  }
  let saved = 0;
  for (const file of batch.files) {
    const stored = await readFile(cache, batch.id, file);
    if (stored === null) continue;
    await send.file(stored, note, await derivedKey("file", batch.id, file.id, note));
    await cache.delete(filePath(batch.id, file.id));
    saved += 1;
  }
  await discardBatch(batch);
  return { duplicate, saved };
}
