// @vitest-environment happy-dom
/// <reference types="node" />
import { act } from "preact/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ItemSummary } from "./api";
import { FEED, href, parseRoute } from "./app";
import { Feed } from "./feed";
import { ItemView } from "./item";
import { Nav, currentEntry } from "./nav";
import { Pins } from "./pins";
import { detail, mount, settle, summary, unmountAll } from "./test-ui";

const A = "01a0de51-0fcc-7089-ae37-1a0e48153325";
const B = "01a0de51-0fcc-7089-ae37-1a0e48153326";
const C = "01a0de51-0fcc-7089-ae37-1a0e48153327";
const PINNED = "2026-11-21T09:00:00Z";

beforeEach(() => {
  location.hash = "#/";
});

afterEach(() => {
  unmountAll();
  vi.unstubAllGlobals();
});

/** A fake server that keeps pin state, so a later list reflects earlier pins. */
function server(items: ItemSummary[], fail: { pin?: boolean } = {}) {
  const state = new Map(items.map((item) => [item.id, { ...item }]));
  const calls: string[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "https://bag.test");
    const method = init?.method ?? "GET";
    calls.push(`${method} ${url.pathname}${url.search}`);
    const json = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    const pin = /^\/api\/v1\/items\/([0-9a-f-]+)\/pin$/.exec(url.pathname);
    if (pin?.[1]) {
      if (fail.pin) return json({ detail: "kaputt" }, 503);
      const item = state.get(pin[1]);
      if (!item) return json({ detail: "Item not found" }, 404);
      item.pinned_at = method === "PUT" ? (item.pinned_at ?? new Date().toISOString()) : null;
      return json(detail(item));
    }
    if (url.pathname === "/api/v1/pins") {
      const limit = Number(url.searchParams.get("limit"));
      const offset = Number(url.searchParams.get("offset"));
      const pinned = [...state.values()]
        .filter((item) => item.pinned_at)
        .sort((a, b) => (b.pinned_at ?? "").localeCompare(a.pinned_at ?? ""));
      const page = pinned.slice(offset, offset + limit);
      return json({ items: page, next_offset: offset + limit < pinned.length ? offset + limit : null });
    }
    if (url.pathname === "/api/v1/items") return json({ items: [...state.values()], next_cursor: null });
    if (url.pathname === "/api/v1/tags" || url.pathname === "/api/v1/collections") return json([]);
    const one = /^\/api\/v1\/items\/([0-9a-f-]+)$/.exec(url.pathname);
    if (one?.[1] && state.has(one[1])) return json(detail(state.get(one[1])!));
    if (/\/processing$/.test(url.pathname)) return json([]);
    return json({ detail: "Not found" }, 404);
  });
  return { calls, state };
}

const button = (root: ParentNode, text: string) =>
  [...root.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.includes(text));

async function openMenu(root: HTMLElement, index: number) {
  const details = root.querySelectorAll<HTMLDetailsElement>("li.item-row .row-actions")[index]!;
  await act(() => {
    details.open = true;
    details.dispatchEvent(new Event("toggle"));
  });
  return details;
}

describe("route and navigation", () => {
  it("has its own route and menu entry", async () => {
    expect(parseRoute("#/merkliste")).toEqual({ name: "pins" });
    expect(href({ name: "pins" })).toBe("#/merkliste");
    expect(currentEntry({ name: "pins" })).toBe("pins");
    const root = await mount(<Nav route={{ name: "pins" }} user="atlas" onSignOut={() => undefined} />);
    const link = root.querySelector<HTMLAnchorElement>('a[href="#/merkliste"]');
    expect(link?.textContent).toBe("Merkliste");
    expect(link?.getAttribute("aria-current")).toBe("page");
  });
});

describe("pinning from the feed and the item view", () => {
  it("pins from the row menu, marks the row and unpins again, only on the server's answer", async () => {
    const { calls } = server([summary({ id: A, title: "Rezept" }), summary({ id: B, title: "Anderes" })]);
    const root = await mount(<Feed route={FEED} share={null} />);
    expect(root.querySelector(".pinned")).toBeNull();
    let details = await openMenu(root, 0);
    const pin = button(details, "Merken")!;
    expect(pin.getAttribute("aria-pressed")).toBe("false");
    await act(() => pin.click());
    await settle();
    expect(calls).toContain(`PUT /api/v1/items/${A}/pin`);
    const rows = root.querySelectorAll("li.item-row");
    expect(rows[0]?.querySelector(".meta")?.textContent).toContain("★ gemerkt");
    expect(rows[1]?.querySelector(".pinned")).toBeNull();
    details = root.querySelectorAll<HTMLDetailsElement>("li.item-row .row-actions")[0]!;
    expect(button(details, "Gemerkt")?.getAttribute("aria-pressed")).toBe("true");
    await act(() => button(details, "Gemerkt")!.click());
    await settle();
    expect(calls).toContain(`DELETE /api/v1/items/${A}/pin`);
    expect(root.querySelector(".pinned")).toBeNull();
  });

  it("keeps the state and says so when the server does not confirm", async () => {
    server([summary({ id: A, title: "Rezept" })], { pin: true });
    const root = await mount(<Feed route={FEED} share={null} />);
    const details = await openMenu(root, 0);
    await act(() => button(details, "Merken")!.click());
    await settle();
    expect(root.querySelector(".pinned")).toBeNull();
    expect(details.querySelector(".notice.error")?.textContent).toContain("Nicht gemerkt");
  });

  it("toggles in the item view", async () => {
    server([summary({ id: C, kind: "text", title: "Notiz" })]);
    const root = await mount(<ItemView id={C} />);
    await act(() => button(root, "Merken")!.click());
    await settle();
    expect(button(root, "Gemerkt")?.getAttribute("aria-pressed")).toBe("true");
    expect(root.querySelector(".notice.success")?.textContent).toContain("Gemerkt");
  });
});

describe("Merkliste page", () => {
  const pinned = (id: string, at: string, title: string) => summary({ id, title, pinned_at: at });

  it("lists pinned items newest pin first and explains an empty list", async () => {
    server([pinned(A, "2026-11-20T10:00:00Z", "Älter"), pinned(B, PINNED, "Neuer"), summary({ id: C, title: "Lose" })]);
    const root = await mount(<Pins />);
    expect(root.querySelector("h1")?.textContent).toBe("Merkliste");
    expect([...root.querySelectorAll("li.item-row .title")].map((t) => t.textContent)).toEqual(["Neuer", "Älter"]);
    unmountAll();
    server([summary({ id: C, title: "Lose" })]);
    const empty = await mount(<Pins />);
    expect(empty.textContent).toContain("Noch nichts gemerkt");
  });

  it("removes an unpinned row and can undo it", async () => {
    const { calls } = server([pinned(A, "2026-11-20T10:00:00Z", "Älter"), pinned(B, PINNED, "Neuer")]);
    const root = await mount(<Pins />);
    const details = await openMenu(root, 1);
    await act(() => button(details, "Gemerkt")!.click());
    await settle();
    expect([...root.querySelectorAll("li.item-row .title")].map((t) => t.textContent)).toEqual(["Neuer"]);
    const undo = button(root, "Rückgängig")!;
    expect(undo.closest(".notice")?.textContent).toContain("Von der Merkliste genommen");
    await act(() => undo.click());
    await settle();
    expect(calls.filter((call) => call === `PUT /api/v1/items/${A}/pin`)).toHaveLength(1);
    // Pinned again now: it is the newest pin.
    expect([...root.querySelectorAll("li.item-row .title")].map((t) => t.textContent)).toEqual(["Älter", "Neuer"]);
    expect(button(root, "Rückgängig")).toBeUndefined();
  });

  it("continues after the rows shown, also after unpinning some", async () => {
    const items = Array.from({ length: 52 }, (_, n) =>
      pinned(`01a0de51-0fcc-7089-ae37-${String(n).padStart(12, "0")}`, `2026-11-21T09:${String(59 - n).padStart(2, "0")}:00Z`, `Nr ${n}`),
    );
    const { calls } = server(items);
    const root = await mount(<Pins />);
    expect(root.querySelectorAll("li.item-row")).toHaveLength(50);
    const details = await openMenu(root, 0);
    await act(() => button(details, "Gemerkt")!.click());
    await settle();
    await act(() => button(root, "Mehr laden")!.click());
    await settle();
    expect(calls).toContain("GET /api/v1/pins?limit=50&offset=49");
    const titles = [...root.querySelectorAll("li.item-row .title")].map((t) => t.textContent);
    expect(titles).toHaveLength(51);
    expect(new Set(titles).size).toBe(51);
    expect(titles).not.toContain("Nr 0");
  });
});
