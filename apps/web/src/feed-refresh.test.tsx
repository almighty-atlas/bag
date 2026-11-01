// @vitest-environment happy-dom
import { render } from "preact";
import { act } from "preact/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FEED } from "./app";
import { Feed } from "./feed";
import { REFRESH_BASE_MS, REFRESH_MAX_FAILURES, mergeDetails } from "./feed-refresh";
import { detail, image, summary, unmountAll } from "./test-ui";

const A = "01a0de51-0fcc-7089-ae37-1a0e48153325";
const B = "01a0de51-0fcc-7089-ae37-1a0e48153326";
const C = "01a0de51-0fcc-7089-ae37-1a0e48153327";

type Item = ReturnType<typeof summary>;
type Reply = unknown | Promise<unknown>;

/** The server's current state per item; the feed list and the item lookups both read from it. */
function server(initial: Item[]) {
  const state = { items: initial, itemCalls: [] as string[], listCalls: 0, inFlight: 0, maxInFlight: 0, fail: false };
  const handlers: { item?: (id: string) => Reply } = {};
  vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "https://bag.test");
    const respond = (body: unknown) =>
      new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    if (url.pathname === "/api/v1/tags" || url.pathname === "/api/v1/collections") return respond([]);
    if (url.pathname === "/api/v1/items") {
      state.listCalls += 1;
      return respond({ items: state.items, next_cursor: null });
    }
    if (url.pathname === "/api/v1/search") {
      state.listCalls += 1;
      return respond({ results: state.items.map((item) => ({ ...item, rank: 1, snippet: "", matched_fields: [] })), next_offset: null });
    }
    const id = url.pathname.split("/").pop()!;
    state.itemCalls.push(id);
    state.inFlight += 1;
    state.maxInFlight = Math.max(state.maxInFlight, state.inFlight);
    try {
      if (state.fail) throw new TypeError("network down");
      const body = handlers.item ? await handlers.item(id) : undefined;
      const found = body ?? state.items.find((item) => item.id === id);
      if (!found) return new Response('{"detail":"Not found"}', { status: 404 });
      return respond(body ? found : detail(found as Item));
    } finally {
      state.inFlight -= 1;
    }
  });
  return Object.assign(state, {
    set(id: string, fields: Partial<Item>) {
      state.items = state.items.map((item) => (item.id === id ? { ...item, ...fields } : item));
    },
    handlers,
  });
}

/** Fake timers are on, so promises are flushed through the timer API instead of real timeouts. */
const advance = (ms: number) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

let container: HTMLElement;

async function mountFeed(route = FEED) {
  container = document.createElement("div");
  document.body.appendChild(container);
  await act(() => render(<Feed route={route} share={null} />, container));
  await advance(0);
  return container;
}

const rerender = async (route: typeof FEED) => {
  await act(() => render(<Feed route={route} share={null} />, container));
  await advance(0);
};

const meta = (root: HTMLElement, index = 0) => root.querySelectorAll("li.item-row .meta")[index]?.textContent ?? "";

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
  document.dispatchEvent(new Event("visibilitychange"));
}

beforeEach(() => {
  location.hash = "#/";
  vi.useFakeTimers();
});

afterEach(() => {
  container?.remove();
  unmountAll();
  setVisibility("visible");
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("feed refresh", () => {
  it("follows queued → processing → ready without a reload and then stops polling", async () => {
    const bag = server([summary({ id: A, processing_status: "queued", title: "Neu" })]);
    const root = await mountFeed();
    expect(meta(root)).toContain("queued");

    bag.set(A, { processing_status: "processing" });
    await advance(REFRESH_BASE_MS);
    expect(meta(root)).toContain("processing");

    bag.set(A, { processing_status: "ready", title: "Fertiger Titel" });
    await advance(REFRESH_BASE_MS * 2);
    expect(meta(root)).not.toMatch(/queued|processing/);
    expect(root.querySelector("li.item-row .title")?.textContent).toBe("Fertiger Titel");

    const calls = bag.itemCalls.length;
    await advance(REFRESH_BASE_MS * 20);
    expect(bag.itemCalls.length).toBe(calls);
    expect(vi.getTimerCount()).toBe(0);
    // The list itself was requested once: refreshing used the item endpoint only.
    expect(bag.listCalls).toBe(1);
  });

  it("shows a failed run as failed and stops polling", async () => {
    const bag = server([summary({ id: A, processing_status: "queued" })]);
    const root = await mountFeed();
    bag.set(A, { processing_status: "failed" });
    await advance(REFRESH_BASE_MS);
    expect(meta(root)).toContain("failed");
    const calls = bag.itemCalls.length;
    await advance(REFRESH_BASE_MS * 10);
    expect(bag.itemCalls.length).toBe(calls);
  });

  it("only looks up unfinished items and keeps a finished thumbnail untouched when a delayed one appears", async () => {
    const bag = server([
      image(A, { processing_status: "ready" }),
      image(B, { processing_status: "processing", thumbnail_available: false }),
      summary({ id: C, processing_status: "ready" }),
    ]);
    const root = await mountFeed();
    const rows = () => [...root.querySelectorAll<HTMLElement>("li.item-row")];
    const first = rows()[0]!.querySelector("img");
    expect(first?.getAttribute("src")).toBe(`/api/v1/items/${A}/thumbnail`);
    expect(rows()[1]!.textContent).toContain("Vorschau wird erstellt…");
    expect(rows()[1]!.querySelector("img")).toBeNull();

    bag.set(B, { processing_status: "ready", thumbnail_available: true });
    await advance(REFRESH_BASE_MS);

    expect(bag.itemCalls).toEqual([B]);
    expect(rows()[1]!.querySelector("img")?.getAttribute("src")).toBe(`/api/v1/items/${B}/thumbnail`);
    expect(rows()[0]!.querySelector("img")).toBe(first);
  });

  it("pauses while the tab is hidden and refreshes at once on return", async () => {
    const bag = server([summary({ id: A, processing_status: "queued" })]);
    const root = await mountFeed();
    setVisibility("hidden");
    bag.set(A, { processing_status: "ready" });
    await advance(REFRESH_BASE_MS * 10);
    expect(bag.itemCalls).toEqual([]);
    expect(vi.getTimerCount()).toBe(0);

    setVisibility("visible");
    await advance(0);
    expect(bag.itemCalls).toEqual([A]);
    expect(meta(root)).not.toContain("queued");
  });

  it("backs off on network errors, recovers, and stops with a manual retry after repeated failure", async () => {
    const bag = server([summary({ id: A, processing_status: "queued" })]);
    const root = await mountFeed();

    bag.fail = true;
    await advance(REFRESH_BASE_MS);
    expect(bag.itemCalls).toHaveLength(1);
    // The next attempt is not due after the base delay again: the wait has doubled.
    await advance(REFRESH_BASE_MS);
    expect(bag.itemCalls).toHaveLength(1);
    await advance(REFRESH_BASE_MS);
    expect(bag.itemCalls).toHaveLength(2);

    bag.fail = false;
    bag.set(A, { processing_status: "ready" });
    await advance(REFRESH_BASE_MS * 4);
    expect(meta(root)).not.toContain("queued");
    expect(root.textContent).not.toContain("pausiert");
  });

  it("gives up after bounded failures and resumes on request", async () => {
    const bag = server([summary({ id: A, processing_status: "queued" })]);
    const root = await mountFeed();
    bag.fail = true;
    await advance(120_000);
    expect(bag.itemCalls).toHaveLength(REFRESH_MAX_FAILURES);
    expect(root.textContent).toContain("Die automatische Aktualisierung ist pausiert.");
    expect(vi.getTimerCount()).toBe(0);
    // The card keeps showing what it knew; nothing is reported as an error.
    expect(root.querySelector(".error")).toBeNull();
    expect(meta(root)).toContain("queued");

    bag.fail = false;
    bag.set(A, { processing_status: "ready" });
    const button = [...root.querySelectorAll("button")].find((b) => b.textContent === "Jetzt aktualisieren")!;
    await act(() => button.click());
    await advance(REFRESH_BASE_MS);
    expect(meta(root)).not.toContain("queued");
    expect(root.textContent).not.toContain("pausiert");
  });

  it("removes its timer and listener on unmount, even with a request in flight", async () => {
    const bag = server([summary({ id: A, processing_status: "queued" })]);
    let release: () => void = () => undefined;
    bag.handlers.item = () => new Promise((resolve) => (release = () => resolve(undefined)));
    const root = await mountFeed();
    await advance(REFRESH_BASE_MS);
    expect(bag.inFlight).toBe(1);

    await act(() => render(null, root));
    release();
    await advance(0);
    expect(vi.getTimerCount()).toBe(0);
    const calls = bag.itemCalls.length;
    setVisibility("hidden");
    setVisibility("visible");
    await advance(REFRESH_BASE_MS * 10);
    expect(bag.itemCalls.length).toBe(calls);
  });

  it("never has two lookups in flight, even when answers are slower than the interval", async () => {
    const bag = server([summary({ id: A, processing_status: "queued" })]);
    const releases: (() => void)[] = [];
    bag.handlers.item = () => new Promise((resolve) => releases.push(() => resolve(undefined)));
    await mountFeed();
    await advance(REFRESH_BASE_MS * 5);
    expect(bag.itemCalls).toHaveLength(1);
    expect(bag.maxInFlight).toBe(1);
    releases.forEach((release) => release());
    await advance(0);
    expect(bag.maxInFlight).toBe(1);
  });

  it("discards an answer that began before the list was replaced", async () => {
    const bag = server([summary({ id: A, processing_status: "queued", title: "Alt" })]);
    let release: () => void = () => undefined;
    bag.handlers.item = () =>
      new Promise((resolve) => {
        release = () => resolve(detail(summary({ id: A, processing_status: "ready", title: "Veraltet" })));
      });
    const root = await mountFeed();
    await advance(REFRESH_BASE_MS);
    expect(bag.inFlight).toBe(1);

    bag.handlers.item = undefined as never;
    await rerender({ ...FEED, query: "alt" });
    release();
    await advance(0);

    expect(root.querySelector("li.item-row .title")?.textContent).toBe("Alt");
    expect(meta(root)).toContain("queued");
  });

  it("ignores items that disappeared and does not poll them forever", async () => {
    const bag = server([summary({ id: A, processing_status: "queued" })]);
    await mountFeed();
    bag.items = [];
    await advance(REFRESH_BASE_MS * 20);
    expect(bag.itemCalls).toEqual([A]);
  });

  it("keeps search, filters, pagination state and the capture draft while refreshing", async () => {
    const bag = server([summary({ id: A, processing_status: "queued", kind: "text" })]);
    const route = { ...FEED, query: "alt", filters: { ...FEED.filters, kind: "text", tag: "x" } };
    const root = await mountFeed(route);
    const typed = root.querySelector<HTMLTextAreaElement>("form.capture textarea")!;
    await act(() => {
      typed.value = "Halb getippt";
      typed.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const search = root.querySelector<HTMLInputElement>("input[type=search]")!;
    const lists = bag.listCalls;

    bag.set(A, { processing_status: "ready" });
    await advance(REFRESH_BASE_MS);

    expect(meta(root)).not.toContain("queued");
    expect(bag.listCalls).toBe(lists);
    expect(root.querySelector("input[type=search]")).toBe(search);
    expect(search.value).toBe("alt");
    expect(root.querySelector("form.capture textarea")).toBe(typed);
    expect(typed.value).toBe("Halb getippt");
    expect(location.hash).toBe("#/");
  });

  it("does not poll the trash", async () => {
    const bag = server([summary({ id: A, processing_status: "queued", deleted_at: "2026-10-31T10:00:00Z" })]);
    await mountFeed({ ...FEED, trashed: true });
    await advance(REFRESH_BASE_MS * 5);
    expect(bag.itemCalls).toEqual([]);
  });
});

describe("mergeDetails", () => {
  it("returns the same array when nothing changed, and only replaces changed rows", () => {
    const rows = [summary({ id: A, processing_status: "queued" }), summary({ id: B, processing_status: "ready" })];
    expect(mergeDetails(rows, [detail(rows[0]!)])).toBe(rows);
    const merged = mergeDetails(rows, [detail(rows[0]!, { processing_status: "ready" })]);
    expect(merged).not.toBe(rows);
    expect(merged[0]!.processing_status).toBe("ready");
    expect(merged[1]).toBe(rows[1]);
  });
});
