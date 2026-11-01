// @vitest-environment happy-dom
/// <reference types="node" />
import { render } from "preact";
import { act } from "preact/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EMPTY_FILTERS, FEED, type Route } from "./app";
import { Feed } from "./feed";
import { Nav, currentEntry } from "./nav";
import { Notice } from "./notice";
import { mount, settle, unmountAll } from "./test-ui";

beforeEach(() => {
  location.hash = "#/";
});

afterEach(() => {
  unmountAll();
  vi.unstubAllGlobals();
});

describe("Notice", () => {
  it("tells success, warning and error apart by class, glyph, spoken label and live-region role", async () => {
    const root = await mount(
      <>
        <Notice tone="success">gespeichert</Notice>
        <Notice tone="warning">unklar</Notice>
        <Notice tone="error">abgelehnt</Notice>
      </>,
    );
    const [success, warning, error] = [...root.querySelectorAll<HTMLElement>(".notice")];
    expect([success?.className, warning?.className, error?.className]).toEqual([
      "notice success",
      "notice warning",
      "notice error",
    ]);
    expect([success, warning, error].map((n) => n?.querySelector(".notice-mark")?.textContent)).toEqual(["✓", "⚠", "✕"]);
    expect([success, warning, error].map((n) => n?.querySelector(".sr-only")?.textContent)).toEqual([
      "Erfolg: ",
      "Warnung: ",
      "Fehler: ",
    ]);
    // Only a refusal interrupts; the rest is announced politely.
    expect([success, warning, error].map((n) => n?.getAttribute("role"))).toEqual(["status", "status", "alert"]);
  });
});

describe("navigation", () => {
  const nav = (route: Route = FEED, onSignOut = () => undefined) => <Nav route={route} user="atlas" onSignOut={onSignOut} />;
  const labels = (root: HTMLElement) => [...root.querySelectorAll(".nav-links a")].map((a) => a.textContent);

  it("keeps every destination, renames Tags to Tags & Sammlungen and still offers token management and sign-out", async () => {
    const onSignOut = vi.fn();
    const root = await mount(nav(FEED, onSignOut));
    expect(labels(root)).toEqual(["Feed", "Tags & Sammlungen", "Papierkorb", "Token"]);
    expect(root.querySelector('a[href="#/organize"]')?.textContent).toBe("Tags & Sammlungen");
    expect(root.querySelector('a[href="#/tokens"]')).not.toBeNull();
    const signOut = root.querySelector<HTMLButtonElement>(".signout");
    expect(signOut?.textContent).toBe("Abmelden (atlas)");
    await act(() => signOut?.click());
    expect(onSignOut).toHaveBeenCalledOnce();
  });

  it("discloses the links from a labelled button and closes again on Escape, returning focus", async () => {
    const root = await mount(nav());
    const toggle = root.querySelector<HTMLButtonElement>(".menu-toggle");
    const links = root.querySelector("#nav-links");
    expect(toggle?.getAttribute("aria-controls")).toBe("nav-links");
    expect(toggle?.getAttribute("aria-expanded")).toBe("false");
    expect(links?.classList.contains("open")).toBe(false);

    await act(() => toggle?.click());
    expect(toggle?.getAttribute("aria-expanded")).toBe("true");
    expect(links?.classList.contains("open")).toBe(true);

    await act(() => {
      dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(toggle?.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(toggle);
  });

  it("closes when the page changes and when the person taps outside the menu", async () => {
    const root = await mount(nav());
    const toggle = root.querySelector<HTMLButtonElement>(".menu-toggle");
    await act(() => toggle?.click());
    expect(toggle?.getAttribute("aria-expanded")).toBe("true");

    await act(() => render(nav({ name: "tokens" }), root));
    expect(toggle?.getAttribute("aria-expanded")).toBe("false");

    await act(() => toggle?.click());
    await act(() => {
      document.body.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    });
    expect(toggle?.getAttribute("aria-expanded")).toBe("false");
  });

  it("marks the current page", async () => {
    expect(currentEntry(FEED)).toBe("feed");
    expect(currentEntry({ ...FEED, trashed: true })).toBe("trash");
    expect(currentEntry({ name: "organize" })).toBe("organize");
    expect(currentEntry({ name: "tokens" })).toBe("tokens");
    expect(currentEntry({ name: "item", id: "x" })).toBeNull();
    const root = await mount(nav({ name: "organize" }));
    const current = [...root.querySelectorAll('[aria-current="page"]')].map((a) => a.textContent);
    expect(current).toEqual(["Tags & Sammlungen"]);
  });
});

describe("filters", () => {
  const stubList = () =>
    vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
      const path = new URL(String(input), "https://bag.test").pathname;
      const body = path === "/api/v1/items" ? { items: [], next_cursor: null } : [];
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    });

  it("labels every control visibly, including the two date fields", async () => {
    stubList();
    const root = await mount(<Feed route={FEED} share={null} />);
    const labelled = [...root.querySelectorAll(".filter-grid label")].map((label) => ({
      text: label.firstChild?.textContent?.trim(),
      control: label.querySelector("select, input")?.tagName,
      type: label.querySelector("input")?.getAttribute("type"),
    }));
    expect(labelled).toEqual([
      { text: "Art", control: "SELECT", type: undefined },
      { text: "Tag", control: "SELECT", type: undefined },
      { text: "Sammlung", control: "SELECT", type: undefined },
      { text: "Erfasst von", control: "INPUT", type: "date" },
      { text: "Erfasst bis", control: "INPUT", type: "date" },
    ]);
    expect(root.querySelector('input[type="search"]')?.getAttribute("aria-label")).toBe("Suchen");
  });

  it("stays open and counts the active filters so none is hidden", async () => {
    stubList();
    const route = { ...FEED, filters: { ...EMPTY_FILTERS, kind: "image", from: "2026-10-01" } };
    const root = await mount(<Feed route={route} share={null} />);
    expect(root.querySelector("details.filters")?.hasAttribute("open")).toBe(true);
    expect(root.querySelector("summary .badge")?.textContent).toBe("2 aktiv");
    expect(root.querySelector(".filter-grid .clear")?.textContent).toBe("Filter löschen");
  });
});

describe("capture feedback", () => {
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

  /** The capture endpoints answer with `capture()`; the feed lists are empty. */
  const stubCapture = (capture: () => Response | Promise<Response>) => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), "https://bag.test").pathname;
      if (path.startsWith("/api/v1/capture")) {
        calls.push(`${init?.method ?? "GET"} ${path}`);
        return capture();
      }
      return json(path === "/api/v1/items" ? { items: [], next_cursor: null } : []);
    });
    return calls;
  };

  const notices = (root: HTMLElement) =>
    [...root.querySelectorAll<HTMLElement>(".capture .notice")].map((n) => [n.className, n.getAttribute("role")]);

  const type = async (root: HTMLElement, value: string) => {
    const box = root.querySelector<HTMLTextAreaElement>("form.capture textarea")!;
    await act(() => {
      box.value = value;
      box.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(() => {
      root.querySelector("form.capture")?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    await settle();
  };

  const pick = async (root: HTMLElement, ...names: string[]) => {
    const input = root.querySelector<HTMLInputElement>('form.capture input[type="file"]')!;
    Object.defineProperty(input, "files", { configurable: true, value: names.map((n) => new File(["x"], n)) });
    await act(() => {
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await settle();
  };

  it("shows only a confirmed save as success", async () => {
    stubCapture(() => json({ id: "a", duplicate_of: null }));
    const root = await mount(<Feed route={FEED} share={null} />);
    await type(root, "Hallo");
    expect(notices(root)).toEqual([["notice success", "status"]]);
    expect(root.querySelector(".capture .notice-text")?.textContent).toBe("Gespeichert");
  });

  it("shows a refused save as an error that names the cause once", async () => {
    stubCapture(() => json({ detail: "Datei zu groß." }, 413));
    const root = await mount(<Feed route={FEED} share={null} />);
    await type(root, "Hallo");
    expect(notices(root)).toEqual([["notice error", "alert"]]);
    const text = root.querySelector(".capture .notice-text")?.textContent ?? "";
    expect(text).toContain("Nicht gespeichert: Datei zu groß.");
    expect(text).not.toContain("Fehler");
    expect(text).not.toContain("..");
    expect(text).toContain("kein Duplikat");
  });

  it("shows an unanswered save as a warning, never as success", async () => {
    stubCapture(() => Promise.reject(new TypeError("Failed to fetch")));
    const root = await mount(<Feed route={FEED} share={null} />);
    await type(root, "Hallo");
    expect(notices(root)).toEqual([["notice warning", "status"]]);
    expect(root.querySelector(".capture .notice-text")?.textContent).toContain("Speichern nicht bestätigt.");
    expect(root.querySelector(".notice.success")).toBeNull();
  });

  it("uploads a picked file at once and keeps retry and discard for the unconfirmed one", async () => {
    let answer: () => Response | Promise<Response> = () => Promise.reject(new TypeError("Failed to fetch"));
    const calls = stubCapture(() => answer());
    const root = await mount(<Feed route={FEED} share={null} />);

    const label = root.querySelector("label.file-button");
    expect(label?.textContent).toContain("Dateien hochladen");
    expect(label?.querySelector('input[type="file"]')).not.toBeNull();

    await pick(root, "a.pdf");
    expect(calls).toEqual(["POST /api/v1/capture/file"]);
    expect(notices(root)).toEqual([
      ["notice warning", "status"],
      ["notice warning", "status"],
    ]);
    const unconfirmed = root.querySelector(".capture .notice")!;
    expect(unconfirmed.textContent).toContain("1 Datei(en) nicht bestätigt: a.pdf");
    const [retry, discard] = [...unconfirmed.querySelectorAll("button")];
    expect(retry?.textContent).toBe("Erneut versuchen");
    expect(discard?.textContent).toBe("Verwerfen");

    answer = () => json({ id: "a", duplicate_of: null });
    await act(() => retry?.click());
    await settle();
    expect(calls).toHaveLength(2);
    expect(notices(root)).toEqual([["notice success", "status"]]);
  });

  it("discards an unconfirmed file without saving it", async () => {
    const calls = stubCapture(() => json({ detail: "kaputt" }, 500));
    const root = await mount(<Feed route={FEED} share={null} />);
    await pick(root, "b.pdf");
    expect(notices(root)).toEqual([
      ["notice warning", "status"],
      ["notice error", "alert"],
    ]);
    const discard = [...root.querySelectorAll<HTMLButtonElement>(".capture .notice button")].find((b) => b.textContent === "Verwerfen");
    await act(() => discard?.click());
    expect(calls).toHaveLength(1);
    expect(root.querySelector(".capture .notice-actions")).toBeNull();
  });
});
