import { render, type ComponentChild } from "preact";
import { act } from "preact/test-utils";
import { vi } from "vitest";

import type { ItemDetail, ItemSummary, ProcessingRun, SearchResult } from "./api";

const STAMP = "2026-10-31T10:00:00Z";

export function summary(fields: Partial<ItemSummary> & { id: string }): ItemSummary {
  return {
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
    thumbnail_available: false,
    created_at: STAMP,
    captured_at: STAMP,
    updated_at: STAMP,
    deleted_at: null,
    ...fields,
  };
}

export function image(id: string, fields: Partial<ItemSummary> = {}): ItemSummary {
  return summary({
    id,
    kind: "image",
    mime_type: "image/png",
    original_filename: `${id}.png`,
    thumbnail_available: true,
    ...fields,
  });
}

export function hit(item: ItemSummary, fields: Partial<SearchResult> = {}): SearchResult {
  return { ...item, rank: 1, snippet: "…", matched_fields: ["filename"], ...fields };
}

export function detail(item: ItemSummary, fields: Partial<ItemDetail> = {}): ItemDetail {
  return {
    id: item.id,
    kind: item.kind,
    source: item.source,
    source_url: item.source_url,
    title: item.title,
    content: null,
    user_note: item.user_note,
    mime_type: item.mime_type,
    original_filename: item.original_filename,
    content_hash: "0".repeat(64),
    extracted_text: null,
    language: null,
    tags: item.tags,
    collections: item.collections,
    processing_status: item.processing_status,
    thumbnail_available: item.thumbnail_available,
    created_at: item.created_at,
    captured_at: item.captured_at,
    updated_at: item.updated_at,
    ...fields,
  };
}

export function run(processor: string, status: string, fields: Partial<ProcessingRun> = {}): ProcessingRun {
  return { processor, status, attempts: 1, last_error: null, started_at: STAMP, finished_at: STAMP, ...fields };
}

export type Handler = (url: URL, init: RequestInit | undefined) => unknown;

/** Replaces fetch: a handler answers API paths with JSON, `undefined` is a 404. Returns the request log. */
export function stubFetch(handler: Handler): string[] {
  const calls: string[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "https://bag.test");
    calls.push(`${init?.method ?? "GET"} ${url.pathname}${url.search}`);
    const result = handler(url, init);
    if (result === undefined) return new Response('{"detail":"Not found"}', { status: 404 });
    return new Response(JSON.stringify(result), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  return calls;
}

/** Let pending promises and the state updates they cause finish. */
export async function settle(): Promise<void> {
  for (let round = 0; round < 6; round += 1) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

const mounted: HTMLElement[] = [];

export async function mount(node: ComponentChild): Promise<HTMLElement> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  mounted.push(container);
  await act(() => render(node, container));
  await settle();
  return container;
}

export function unmountAll(): void {
  for (const container of mounted.splice(0)) {
    render(null, container);
    container.remove();
  }
}

export async function fire(target: Element, type: "load" | "error"): Promise<void> {
  await act(() => {
    target.dispatchEvent(new Event(type));
  });
}
