// @vitest-environment happy-dom
/// <reference types="node" />
import { act } from "preact/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { copySource, itemLink, sharePayload } from "./actions";
import { FEED } from "./app";
import { clipboardFiles, copyText, pastedFile, pastedName, readClipboard } from "./clipboard";
import { Feed } from "./feed";
import { ItemView } from "./item";
import { itemStatusLabel, kindLabel, processorLabel, runStatusLabel } from "./labels";
import { detail, mount, run, settle, stubFetch, summary, unmountAll } from "./test-ui";

const A = "01a0de51-0fcc-7089-ae37-1a0e48153325";
const B = "01a0de51-0fcc-7089-ae37-1a0e48153326";
const C = "01a0de51-0fcc-7089-ae37-1a0e48153327";
const NOW = new Date(2026, 10, 21, 14, 5, 9);

const restore: (() => void)[] = [];

/** Replaces one navigator property for the test; afterEach puts the original back. */
function stubNavigator(name: "clipboard" | "share" | "canShare", value: unknown): void {
  const previous = Object.getOwnPropertyDescriptor(navigator, name);
  Object.defineProperty(navigator, name, { value, configurable: true, writable: true });
  restore.push(() => {
    if (previous) Object.defineProperty(navigator, name, previous);
    else delete (navigator as unknown as Record<string, unknown>)[name];
  });
}

function pasteEvent(files: File[], text = ""): Event {
  const event = new Event("paste", { bubbles: true, cancelable: true });
  const data = { files, getData: (type: string) => (type === "text/plain" ? text : "") };
  Object.defineProperty(event, "clipboardData", { value: data });
  return event;
}

beforeEach(() => {
  location.hash = "#/";
});

afterEach(() => {
  unmountAll();
  vi.unstubAllGlobals();
  for (const undo of restore.splice(0).reverse()) undo();
});

describe("labels", () => {
  it("says what kinds, states and processors mean, and shows unknown values as sent", () => {
    expect(["text", "url", "image", "document", "file"].map(kindLabel)).toEqual(["Text", "Link", "Bild", "Dokument", "Datei"]);
    expect(itemStatusLabel("queued")).toBe("wartet auf Verarbeitung");
    expect(itemStatusLabel("partial")).toBe("teilweise verarbeitet");
    expect(runStatusLabel("skipped")).toBe("übersprungen");
    expect(processorLabel("url_fetch")).toBe("Webseite abrufen");
    expect(processorLabel("thumbnail")).toBe("Vorschaubild erstellen");
    expect(processorLabel("future_ocr")).toBe("future_ocr");
    expect(itemStatusLabel("toString")).toBe("toString");
  });
});

describe("clipboard helpers", () => {
  it("names pasted images by date and keeps real file names", () => {
    expect(pastedName("image/png", NOW)).toBe("Zwischenablage 2026-11-21 14-05-09.png");
    expect(pastedName("application/x-unknown", NOW)).toBe("Zwischenablage 2026-11-21 14-05-09");
    const generic = pastedFile(new File(["x"], "image.png", { type: "image/png" }), NOW);
    expect(generic.name).toBe("Zwischenablage 2026-11-21 14-05-09.png");
    expect(generic.type).toBe("image/png");
    const real = new File(["x"], "Rechnung.pdf", { type: "application/pdf" });
    expect(pastedFile(real, NOW)).toBe(real);
    expect(clipboardFiles(null)).toEqual([]);
  });

  it("reads images before text, falls back to readText and reports an empty clipboard", async () => {
    const png = new Blob(["png"], { type: "image/png" });
    stubNavigator("clipboard", {
      read: async () => [
        { types: ["text/plain"], getType: async () => new Blob(["daneben"]) },
        { types: ["text/html", "image/png"], getType: async () => png },
      ],
    });
    const content = await readClipboard(NOW);
    expect(content && "files" in content && content.files.map((file) => [file.name, file.type])).toEqual([
      ["Zwischenablage 2026-11-21 14-05-09.png", "image/png"],
    ]);

    stubNavigator("clipboard", { read: async () => [{ types: ["text/plain"], getType: async () => new Blob(["Notiz"]) }] });
    expect(await readClipboard()).toEqual({ text: "Notiz" });
    stubNavigator("clipboard", { readText: async () => "nur Text" });
    expect(await readClipboard()).toEqual({ text: "nur Text" });
    stubNavigator("clipboard", { readText: async () => "" });
    expect(await readClipboard()).toBeNull();
    stubNavigator("clipboard", { readText: async () => Promise.reject(new DOMException("no", "NotAllowedError")) });
    await expect(readClipboard()).rejects.toThrow();
  });

  it("copies directly, through a pending ClipboardItem, or after loading where that is refused", async () => {
    const writeText = vi.fn(async () => undefined);
    const write = vi.fn(async (items: { data: Record<string, Promise<Blob>> }[]) => {
      await items[0]?.data["text/plain"];
    });
    vi.stubGlobal(
      "ClipboardItem",
      class {
        constructor(public data: Record<string, Promise<Blob>>) {}
      },
    );
    stubNavigator("clipboard", { writeText, write });
    await copyText("direkt");
    expect(writeText).toHaveBeenCalledWith("direkt");
    const load = vi.fn(async () => "geladen");
    await copyText(load);
    expect(write).toHaveBeenCalledTimes(1);
    expect(load).toHaveBeenCalledTimes(1);

    write.mockImplementationOnce(async () => {
      throw new TypeError("pending blobs unsupported");
    });
    await copyText(load);
    expect(writeText).toHaveBeenLastCalledWith("geladen");
    // The text is loaded once per copy, also on the fallback path.
    expect(load).toHaveBeenCalledTimes(2);
    stubNavigator("clipboard", undefined);
    await expect(copyText("x")).rejects.toThrow();
  });
});

describe("action payloads", () => {
  const base = { title: null, source_url: null, original_filename: null };

  it("copies the text, the saved link, or a link back to the item", async () => {
    expect(copySource({ ...base, id: A, kind: "text", content: "Volltext" })).toBe("Volltext");
    expect(copySource({ ...base, id: A, kind: "url", source_url: "https://example.org/a" })).toBe("https://example.org/a");
    expect(copySource({ ...base, id: A, kind: "image" })).toBe(itemLink(A));
    expect(itemLink(A)).toMatch(new RegExp(`#/items/${A}$`));
    stubFetch((url) => (url.pathname === `/api/v1/items/${A}` ? detail(summary({ id: A }), { content: "vom Server" }) : undefined));
    const loader = copySource({ ...base, id: A, kind: "text" });
    expect(typeof loader).toBe("function");
    expect(await (loader as () => Promise<string>)()).toBe("vom Server");
  });

  it("shares text and links directly and files as the original", async () => {
    expect(await sharePayload({ ...base, id: A, kind: "text", title: "Idee", content: "Inhalt" })).toEqual({
      title: "Idee",
      text: "Inhalt",
    });
    expect(await sharePayload({ ...base, id: A, kind: "url", source_url: "https://example.org/" })).toEqual({
      url: "https://example.org/",
    });
    vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
      expect(String(input)).toBe(`/api/v1/items/${B}/content`);
      return new Response(new Blob(["%PDF"], { type: "application/pdf" }));
    });
    const payload = await sharePayload({ ...base, id: B, kind: "document", original_filename: "Brief.pdf" });
    expect(payload.title).toBe("Brief.pdf");
    expect(payload.files?.map((file) => [file.name, file.type])).toEqual([["Brief.pdf", "application/pdf"]]);
  });
});

describe("capture by paste", () => {
  function server(rows = [summary({ id: A, title: "Alt" })]) {
    const uploads: string[] = [];
    const calls = stubFetch((url, init) => {
      if (url.pathname === "/api/v1/items") return { items: rows, next_cursor: null };
      if (url.pathname === "/api/v1/tags" || url.pathname === "/api/v1/collections") return [];
      if (url.pathname === "/api/v1/capture/file") {
        const file = (init?.body as FormData).get("file") as File;
        uploads.push(file.name);
        return { id: C, status: "stored", processing_status: "queued", duplicate_of: null };
      }
      return undefined;
    });
    return { calls, uploads };
  }

  it("saves a pasted image right away under a dated name", async () => {
    const { uploads } = server();
    const root = await mount(<Feed route={FEED} share={null} />);
    const textarea = root.querySelector<HTMLTextAreaElement>(".capture textarea")!;
    const event = pasteEvent([new File(["png"], "image.png", { type: "image/png" })]);
    await act(() => {
      textarea.dispatchEvent(event);
    });
    await settle();
    expect(event.defaultPrevented).toBe(true);
    expect(uploads).toHaveLength(1);
    expect(uploads[0]).toMatch(/^Zwischenablage \d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}\.png$/);
    expect(root.querySelector(".capture .notice.success")?.textContent).toContain("Gespeichert");
  });

  it("leaves pasted text in a text field to the browser", async () => {
    const { uploads } = server();
    const root = await mount(<Feed route={FEED} share={null} />);
    const search = root.querySelector<HTMLInputElement>("form.search input")!;
    const event = pasteEvent([], "Suchwort");
    await act(() => {
      search.dispatchEvent(event);
    });
    expect(event.defaultPrevented).toBe(false);
    expect(root.querySelector<HTMLTextAreaElement>(".capture textarea")?.value).toBe("");
    expect(uploads).toEqual([]);
  });

  it("puts text pasted outside a field into the form without saving it", async () => {
    const { calls, uploads } = server();
    const root = await mount(<Feed route={FEED} share={null} />);
    const event = pasteEvent([], "Eine Idee aus der Zwischenablage");
    await act(() => {
      document.body.dispatchEvent(event);
    });
    expect(event.defaultPrevented).toBe(true);
    const textarea = root.querySelector<HTMLTextAreaElement>(".capture textarea")!;
    expect(textarea.value).toBe("Eine Idee aus der Zwischenablage");
    expect(document.activeElement).toBe(textarea);
    expect(root.querySelector(".capture .notice.warning")?.textContent).toContain("noch nicht gespeichert");
    expect(calls.some((call) => call.startsWith("POST"))).toBe(false);
    // A second paste appends on a new line; files pasted outside a field are saved.
    await act(() => {
      document.body.dispatchEvent(pasteEvent([], "Zweite Zeile"));
    });
    expect(textarea.value).toBe("Eine Idee aus der Zwischenablage\nZweite Zeile");
    await act(() => {
      document.body.dispatchEvent(pasteEvent([new File(["%PDF"], "Brief.pdf", { type: "application/pdf" })]));
    });
    await settle();
    expect(uploads).toEqual(["Brief.pdf"]);
  });

  it("offers an Einfügen button where the clipboard can be read, and explains a refusal", async () => {
    server();
    stubNavigator("clipboard", { readText: async () => "Von der Taste" });
    const root = await mount(<Feed route={FEED} share={null} />);
    const button = [...root.querySelectorAll<HTMLButtonElement>(".capture button")].find((b) =>
      b.textContent?.includes("Einfügen"),
    );
    expect(button).toBeDefined();
    await act(() => button!.click());
    await settle();
    expect(root.querySelector<HTMLTextAreaElement>(".capture textarea")?.value).toBe("Von der Taste");

    stubNavigator("clipboard", { readText: async () => Promise.reject(new DOMException("no", "NotAllowedError")) });
    await act(() => button!.click());
    await settle();
    expect(root.querySelector(".capture .notice.error")?.textContent).toContain("Strg+V");
  });

  it("hides the Einfügen button where the clipboard cannot be read", async () => {
    server();
    stubNavigator("clipboard", undefined);
    const root = await mount(<Feed route={FEED} share={null} />);
    expect([...root.querySelectorAll(".capture button")].some((b) => b.textContent?.includes("Einfügen"))).toBe(false);
  });
});

describe("item actions", () => {
  const rows = [
    summary({ id: A, kind: "url", source_url: "https://example.org/artikel", title: "Artikel" }),
    summary({ id: B, kind: "text", excerpt: "Kurz" }),
    summary({ id: C, kind: "document", original_filename: "Brief.pdf", processing_status: "partial" }),
  ];
  const serve = () =>
    stubFetch((url) => {
      if (url.pathname === "/api/v1/items") return { items: rows, next_cursor: null };
      if (url.pathname === "/api/v1/tags" || url.pathname === "/api/v1/collections") return [];
      if (url.pathname === `/api/v1/items/${B}`) return detail(rows[1]!, { content: "Der ganze Text" });
      return undefined;
    });
  const menu = (root: HTMLElement, index: number) => root.querySelectorAll<HTMLDetailsElement>("li.item-row .row-actions")[index]!;
  const openMenu = async (root: HTMLElement, index: number) => {
    const details = menu(root, index);
    await act(() => {
      details.open = true;
      details.dispatchEvent(new Event("toggle"));
    });
    return details;
  };

  it("keeps a closed menu free of links, opens with an accessible name and closes on Escape", async () => {
    serve();
    const root = await mount(<Feed route={FEED} share={null} />);
    expect(root.querySelectorAll("li.item-row .row-actions")).toHaveLength(3);
    expect(root.querySelectorAll(".row-actions a, .row-actions button")).toHaveLength(0);
    expect(menu(root, 0).querySelector("summary")?.getAttribute("aria-label")).toBe("Aktionen für Artikel");
    // The menu is a sibling of the row link, never inside it.
    expect(root.querySelector("a.item .row-actions")).toBeNull();
    const details = await openMenu(root, 0);
    const open = details.querySelector<HTMLAnchorElement>("a.button")!;
    expect(open.textContent).toBe("Link öffnen");
    expect(open.getAttribute("href")).toBe("https://example.org/artikel");
    expect(open.getAttribute("rel")).toBe("noopener noreferrer nofollow");
    expect(open.getAttribute("target")).toBe("_blank");
    await act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(menu(root, 0).querySelector(".actions-menu")).toBeNull();
  });

  it("copies a link, loads and copies a text, and offers the original of a file", async () => {
    const calls = serve();
    const writeText = vi.fn(async () => undefined);
    stubNavigator("clipboard", { writeText });
    const root = await mount(<Feed route={FEED} share={null} />);
    const button = (details: HTMLElement, label: string) =>
      [...details.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent === label)!;

    let details = await openMenu(root, 0);
    await act(() => button(details, "Link kopieren").click());
    await settle();
    expect(writeText).toHaveBeenLastCalledWith("https://example.org/artikel");
    expect(details.querySelector(".notice.success")?.textContent).toContain("kopiert");

    details = await openMenu(root, 1);
    await act(() => button(details, "Text kopieren").click());
    await settle();
    expect(calls).toContain(`GET /api/v1/items/${B}`);
    expect(writeText).toHaveBeenLastCalledWith("Der ganze Text");

    details = await openMenu(root, 2);
    const download = details.querySelector<HTMLAnchorElement>("a.button")!;
    expect(download.textContent).toBe("Original herunterladen");
    expect(download.getAttribute("href")).toBe(`/api/v1/items/${C}/content`);
    expect(download.getAttribute("download")).toBe("Brief.pdf");
    expect(button(details, "Link zum Eintrag kopieren")).toBeDefined();
    // No share sheet in this browser: no share button.
    expect(button(details, "Teilen…")).toBeUndefined();
  });

  it("shares through the system sheet and treats closing it as no error", async () => {
    serve();
    const share = vi.fn(async () => Promise.reject(new DOMException("closed", "AbortError")));
    stubNavigator("share", share);
    stubNavigator("canShare", () => false);
    const root = await mount(<Feed route={FEED} share={null} />);
    const details = await openMenu(root, 0);
    const button = [...details.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent === "Teilen…")!;
    await act(() => button.click());
    await settle();
    expect(share).toHaveBeenCalledWith({ title: "Artikel", url: "https://example.org/artikel" });
    expect(details.querySelector(".notice")).toBeNull();
    // Files need canShare({files}); without it a document offers no share button.
    const file = await openMenu(root, 2);
    expect([...file.querySelectorAll("button")].some((b) => b.textContent === "Teilen…")).toBe(false);
  });

  it("names kinds and states in the feed instead of raw values", async () => {
    serve();
    const root = await mount(<Feed route={FEED} share={null} />);
    const meta = [...root.querySelectorAll("li.item-row .meta")].map((node) => node.textContent ?? "");
    expect(meta[0]).toContain("Link");
    expect(meta[2]).toContain("Dokument · teilweise verarbeitet");
    expect(meta.join(" ")).not.toMatch(/partial|document/);
  });

  it("shows actions and readable processing steps in the item view", async () => {
    const item = summary({ id: C, kind: "document", original_filename: "Brief.pdf", processing_status: "partial" });
    stubFetch((url) => {
      if (url.pathname === `/api/v1/items/${C}`) return detail(item, { language: "de" });
      if (url.pathname === `/api/v1/items/${C}/processing`) {
        return [run("mime_detect", "succeeded"), run("pdf_text", "skipped"), run("thumbnail", "failed", { last_error: "kaputt" })];
      }
      return undefined;
    });
    const root = await mount(<ItemView id={C} />);
    expect(root.querySelector(".detail .meta")?.textContent).toContain("Dokument");
    expect(root.querySelector(".detail .meta")?.textContent).toContain("teilweise verarbeitet");
    expect(root.querySelector(".detail .meta")?.textContent).toContain("Sprache Deutsch");
    const rowsText = [...root.querySelectorAll("table.stack tr")].map((row) => row.textContent ?? "");
    expect(rowsText[0]).toContain("Dateityp erkennen");
    expect(rowsText[0]).toContain("erledigt");
    expect(rowsText[1]).toContain("PDF-Text auslesen");
    expect(rowsText[1]).toContain("übersprungen");
    expect(rowsText[2]).toContain("fehlgeschlagen");
    expect(rowsText[2]).toContain("kaputt");
    const actions = root.querySelector(".detail > .item-actions")!;
    expect([...actions.querySelectorAll("a, button")].map((node) => node.textContent)).toEqual([
      "☆ Merken",
      "Original herunterladen",
      "Link zum Eintrag kopieren",
    ]);
  });
});
