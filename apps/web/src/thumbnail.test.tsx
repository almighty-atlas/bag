// @vitest-environment happy-dom
/// <reference types="node" />
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FEED } from "./app";
import { Feed } from "./feed";
import { ItemView, PREVIEW_POLL_LIMIT, PREVIEW_POLL_MS } from "./item";
import { PREVIEW_MESSAGES, ORIGINAL_SAFE, previewState, thumbnailAlt } from "./thumbnail";
import { act } from "preact/test-utils";
import { detail, fire, hit, image, mount, run, settle, stubFetch, summary, unmountAll } from "./test-ui";

// Vitest turns CSS imports (even ?raw) into empty modules, so the stylesheet is read as a file.
const styles = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "styles.css"), "utf8");

const A = "01a0de51-0fcc-7089-ae37-1a0e48153325";
const B = "01a0de51-0fcc-7089-ae37-1a0e48153326";
const C = "01a0de51-0fcc-7089-ae37-1a0e48153327";

const cards = (root: HTMLElement) => [...root.querySelectorAll<HTMLElement>("li.item-row")];
const imgs = (root: ParentNode) => [...root.querySelectorAll<HTMLImageElement>("img")];

beforeEach(() => {
  location.hash = "#/";
});

afterEach(() => {
  unmountAll();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("previewState", () => {
  const base = { kind: "image", thumbnail_available: false, processing_status: "ready" };

  it("only images have previews, and only the server's flag leads to a request", () => {
    expect(previewState({ ...base, kind: "text", thumbnail_available: true })).toBe("none");
    expect(previewState({ ...base, kind: "url", thumbnail_available: true })).toBe("none");
    expect(previewState({ ...base, kind: "file", thumbnail_available: true })).toBe("none");
    expect(previewState({ ...base, thumbnail_available: true })).toBe("try");
    expect(previewState(base)).toBe("missing");
  });

  it("never requests a trashed item's preview", () => {
    expect(previewState({ ...base, thumbnail_available: true }, { trashed: true })).toBe("trashed");
  });

  it("tells running work from a missing preview, from the summary alone", () => {
    expect(previewState({ ...base, processing_status: "queued" })).toBe("pending");
    expect(previewState({ ...base, processing_status: "processing" })).toBe("pending");
    for (const status of ["partial", "failed", "ready"]) {
      expect(previewState({ ...base, processing_status: status })).toBe("missing");
    }
  });

  it("uses the thumbnail run, where known, to tell failure, skipping and waiting apart", () => {
    const state = (status: string) => previewState({ ...base, processing_status: "partial" }, { run: run("thumbnail", status) });
    expect(state("pending")).toBe("pending");
    expect(state("running")).toBe("pending");
    expect(state("failed")).toBe("failed");
    expect(state("skipped")).toBe("missing");
    expect(state("succeeded")).toBe("missing");
  });

  it("names the picture by title or file name, never by stored text", () => {
    expect(thumbnailAlt({ title: "Wohnzimmer", original_filename: "IMG_1.png" })).toBe("Vorschau: Wohnzimmer");
    expect(thumbnailAlt({ title: null, original_filename: "IMG_1.png" })).toBe("Vorschau: IMG_1.png");
    expect(thumbnailAlt({ title: null, original_filename: null })).toBe("Vorschau: Bild");
  });
});

describe("feed cards", () => {
  const feed = (rows: ReturnType<typeof summary>[], extra: (url: URL) => unknown = () => undefined) =>
    stubFetch((url) => {
      if (url.pathname === "/api/v1/items") return { items: rows, next_cursor: null };
      if (url.pathname === "/api/v1/tags" || url.pathname === "/api/v1/collections") return [];
      return extra(url);
    });

  it("shows a lazy, labelled thumbnail from the thumbnail endpoint and keeps the card's text and link", async () => {
    feed([
      image(A, { title: "Wohnzimmer", user_note: "Vor dem Umbau", tags: ["umzug"], captured_at: "2026-09-02T08:30:00Z" }),
    ]);
    const root = await mount(<Feed route={FEED} share={null} />);

    const [card] = cards(root);
    const [picture] = imgs(card!);
    expect(picture?.getAttribute("src")).toBe(`/api/v1/items/${A}/thumbnail`);
    expect(picture?.getAttribute("loading")).toBe("lazy");
    expect(picture?.getAttribute("alt")).toBe("Vorschau: Wohnzimmer");
    const link = card?.querySelector<HTMLAnchorElement>("a.item");
    expect(link?.getAttribute("href")).toBe(`#/items/${A}`);
    expect(link?.contains(picture ?? null)).toBe(true);
    expect(card?.textContent).toContain("Wohnzimmer");
    expect(card?.textContent).toContain("Vor dem Umbau");
    expect(card?.textContent).toContain(new Date("2026-09-02T08:30:00Z").toLocaleString());
    expect(card?.querySelector("a.tag")?.textContent).toBe("umzug");
  });

  it("holds a quiet placeholder until the picture has loaded", async () => {
    feed([image(A)]);
    const root = await mount(<Feed route={FEED} share={null} />);
    const frame = root.querySelector<HTMLElement>(".thumb")!;
    expect(frame.classList.contains("loading")).toBe(true);
    expect(frame.querySelector(".thumb-icon")?.getAttribute("aria-hidden")).toBe("true");
    await fire(imgs(root)[0]!, "load");
    expect(frame.classList.contains("loaded")).toBe(true);
    expect(frame.querySelector(".thumb-icon")).toBeNull();
    expect(imgs(root)).toHaveLength(1);
  });

  it("replaces an unloadable picture by readable text without touching other cards", async () => {
    feed([image(A, { title: "Kaputt" }), image(B, { title: "Intakt" })]);
    const root = await mount(<Feed route={FEED} share={null} />);
    const [first, second] = cards(root);
    await fire(imgs(first!)[0]!, "error");

    expect(imgs(first!)).toHaveLength(0);
    expect(first?.querySelector(".thumb")?.getAttribute("data-preview")).toBe("unloadable");
    expect(first?.textContent).toContain(PREVIEW_MESSAGES.unloadable);
    expect(first?.textContent).toContain("Kaputt");
    expect(first?.querySelector("a.item")?.getAttribute("href")).toBe(`#/items/${A}`);
    expect(imgs(second!)).toHaveLength(1);
    expect(second?.querySelector(".thumb")?.getAttribute("data-preview")).toBe("loading");
    // The message is plain text: no broken-image icon, and no alert that would interrupt a list.
    expect(first?.querySelector("[role]")).toBeNull();
  });

  it("does not ask for a thumbnail that is still being made and says so", async () => {
    const calls = feed([image(A, { thumbnail_available: false, processing_status: "processing" })]);
    const root = await mount(<Feed route={FEED} share={null} />);
    expect(imgs(root)).toHaveLength(0);
    expect(root.querySelector(".thumb")?.getAttribute("data-preview")).toBe("pending");
    expect(root.textContent).toContain(PREVIEW_MESSAGES.pending);
    expect(calls.some((call) => call.includes("/thumbnail"))).toBe(false);
  });

  it("shows a fallback without a request when the server has no thumbnail", async () => {
    const calls = feed([image(A, { thumbnail_available: false, title: "Scan" })]);
    const root = await mount(<Feed route={FEED} share={null} />);
    expect(imgs(root)).toHaveLength(0);
    expect(root.textContent).toContain(PREVIEW_MESSAGES.missing);
    expect(root.textContent).toContain("Scan");
    expect(root.querySelector("a.item")?.getAttribute("href")).toBe(`#/items/${A}`);
    expect(calls.some((call) => call.includes("/thumbnail"))).toBe(false);
  });

  it("loads no thumbnails in the trash and keeps restoring possible", async () => {
    const calls = feed([image(A, { deleted_at: "2026-10-31T09:00:00Z", title: "Weg" })]);
    const root = await mount(<Feed route={{ ...FEED, trashed: true }} share={null} />);
    expect(imgs(root)).toHaveLength(0);
    expect(root.querySelector(".thumb")).toBeNull();
    expect(root.querySelector(".icon")?.textContent).toBe("🖼️");
    expect(root.querySelector(".restore")?.textContent).toBe("Wiederherstellen");
    expect(root.querySelector("a.item")?.hasAttribute("href")).toBe(false);
    expect(calls.some((call) => call.includes("/thumbnail"))).toBe(false);
    expect(calls).toContain("GET /api/v1/items?limit=20&trashed=true");
  });

  it("never requests the preview of a trashed row, even while a stale list is shown under a live route", async () => {
    const calls = feed([image(A, { deleted_at: "2026-10-31T09:00:00Z" })]);
    const root = await mount(<Feed route={FEED} share={null} />);
    expect(imgs(root)).toHaveLength(0);
    expect(root.querySelector(".thumb")).toBeNull();
    expect(calls.some((call) => call.includes("/thumbnail"))).toBe(false);
  });

  it("leaves text, link and file cards exactly as they were", async () => {
    feed([
      summary({ id: A, kind: "text", excerpt: "Eine Idee", thumbnail_available: true }),
      summary({ id: B, kind: "url", source_url: "https://example.org/bild.png", title: "Seite", thumbnail_available: true }),
      summary({ id: C, kind: "file", original_filename: "scan.png", mime_type: "image/png", thumbnail_available: true }),
      summary({ id: "01a0de51-0fcc-7089-ae37-1a0e48153328", kind: "document", original_filename: "a.pdf" }),
    ]);
    const root = await mount(<Feed route={FEED} share={null} />);
    expect(cards(root)).toHaveLength(4);
    expect(imgs(root)).toHaveLength(0);
    expect(root.querySelector(".thumb")).toBeNull();
    expect([...root.querySelectorAll(".icon")].map((icon) => icon.textContent)).toEqual(["📝", "🔗", "📦", "📄"]);
    expect(root.querySelectorAll("a.item.with-thumb")).toHaveLength(0);
    expect(cards(root)[0]?.textContent).toContain("Eine Idee");
  });

  it("never renders an original or an external URL inline", async () => {
    feed([
      image(A, { source_url: "https://evil.example/tracker.png", mime_type: "image/svg+xml" }),
      image(B, { thumbnail_available: false }),
      summary({ id: C, kind: "url", source_url: "https://evil.example/pixel.gif" }),
    ]);
    const root = await mount(<Feed route={FEED} share={null} />);
    const sources = imgs(root).map((picture) => picture.getAttribute("src"));
    expect(sources).toEqual([`/api/v1/items/${A}/thumbnail`]);
    const references = [...root.querySelectorAll("[src], [href], [srcset], [data], [poster]")].flatMap((node) =>
      ["src", "href", "srcset", "data", "poster"].map((name) => node.getAttribute(name) ?? ""),
    );
    expect(references.join(" ")).not.toMatch(/\/content|evil\.example/);
    expect(root.querySelectorAll("img[srcset], picture, video, object, embed, iframe")).toHaveLength(0);
    // Same origin, no credentials in the URL and no cross-origin mode: the session cookie authenticates it.
    expect(imgs(root)[0]?.hasAttribute("crossorigin")).toBe(false);
    expect(sources[0]).not.toMatch(/token|\?/);
  });

  it("shows thumbnails and match reasons in search results and in every further page", async () => {
    const calls = stubFetch((url) => {
      if (url.pathname === "/api/v1/tags" || url.pathname === "/api/v1/collections") return [];
      if (url.pathname !== "/api/v1/search") return undefined;
      const offset = Number(url.searchParams.get("offset") ?? "0");
      return offset === 0
        ? {
            results: [
              hit(image(A, { title: "Küche" }), { matched_fields: ["title", "filename"], snippet: "«Küche» vorher" }),
              hit(summary({ id: B, kind: "text", excerpt: "Küche Notiz" }), { matched_fields: ["content"] }),
            ],
            next_offset: 2,
          }
        : {
            results: [hit(image(C, { thumbnail_available: false, processing_status: "queued" }), { matched_fields: ["tag"] })],
            next_offset: null,
          };
    });
    const root = await mount(<Feed route={{ ...FEED, query: "Küche" }} share={null} />);
    expect(calls).toContain("GET /api/v1/search?limit=20&q=K%C3%BCche");
    expect(imgs(root).map((picture) => picture.getAttribute("src"))).toEqual([`/api/v1/items/${A}/thumbnail`]);
    expect(cards(root)[0]?.textContent).toContain("Gefunden in");
    expect(cards(root)[0]?.textContent).toContain("«Küche» vorher");

    const more = [...root.querySelectorAll("button")].find((button) => button.textContent === "Mehr laden")!;
    await act(() => {
      more.click();
    });
    await settle();
    expect(calls).toContain("GET /api/v1/search?limit=20&offset=2&q=K%C3%BCche");
    expect(cards(root)).toHaveLength(3);
    // The first page's picture is kept, the new image card shows its fallback instead of a request.
    expect(imgs(root)).toHaveLength(1);
    expect(cards(root)[2]?.textContent).toContain(PREVIEW_MESSAGES.pending);
    expect(cards(root)[2]?.textContent).toContain("Gefunden in");
  });

  it("is operable with the keyboard through the card link alone", async () => {
    feed([image(A, { title: "Tür" })]);
    const root = await mount(<Feed route={FEED} share={null} />);
    const link = root.querySelector<HTMLAnchorElement>("a.item")!;
    link.focus();
    expect(document.activeElement).toBe(link);
    const picture = imgs(root)[0]!;
    expect(picture.hasAttribute("tabindex")).toBe(false);
    expect(picture.getAttribute("alt")).not.toBe("");
    expect(root.querySelectorAll("a.item button, a.item [tabindex]")).toHaveLength(0);
  });
});

describe("detail view", () => {
  const open = (item = image(A, { title: "Wohnzimmer" }), runs = [run("thumbnail", "succeeded")], patch = (body: unknown) => body) =>
    stubFetch((url, init) => {
      if (url.pathname === `/api/v1/items/${A}` && init?.method === "PATCH") return patch(detail(item, JSON.parse(String(init.body))));
      if (url.pathname === `/api/v1/items/${A}`) return detail(item);
      if (url.pathname === `/api/v1/items/${A}/processing`) return runs;
      return undefined;
    });

  it("shows the larger preview first and keeps editing and the original download", async () => {
    open();
    const root = await mount(<ItemView id={A} />);
    const picture = root.querySelector<HTMLImageElement>(".detail-preview img");
    expect(picture?.getAttribute("src")).toBe(`/api/v1/items/${A}/thumbnail`);
    expect(picture?.getAttribute("alt")).toBe("Vorschau: Wohnzimmer");
    expect(root.querySelector(".detail-preview")?.compareDocumentPosition(root.querySelector("form.card")!)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    expect(root.querySelector<HTMLInputElement>("label input")?.value).toBe("Wohnzimmer");
    const download = [...root.querySelectorAll<HTMLAnchorElement>("a.button")].find((a) => a.textContent === "Original herunterladen");
    expect(download?.getAttribute("href")).toBe(`/api/v1/items/${A}/content`);
    expect(download?.hasAttribute("download")).toBe(true);
    // The original is only ever linked as a download, never an image source.
    expect(imgs(root).map((node) => node.getAttribute("src"))).toEqual([`/api/v1/items/${A}/thumbnail`]);
    for (const label of ["Titel", "Gedanke", "Tags (durch Komma getrennt)", "Sammlungen (durch Komma getrennt)"]) {
      expect(root.textContent).toContain(label);
    }
  });

  it("opens and saves normally when the thumbnail cannot be loaded", async () => {
    const calls = open();
    const root = await mount(<ItemView id={A} />);
    await fire(imgs(root)[0]!, "error");

    expect(imgs(root)).toHaveLength(0);
    const note = root.querySelector(".thumb")!;
    expect(note.getAttribute("data-preview")).toBe("unloadable");
    expect(note.textContent).toContain(PREVIEW_MESSAGES.unloadable);
    expect(note.textContent).toContain(ORIGINAL_SAFE);
    expect(note.querySelector("[role=status]")).not.toBeNull();
    expect(root.textContent).toContain("Original herunterladen");

    const title = root.querySelector<HTMLInputElement>("label input")!;
    await act(() => {
      title.value = "Neu";
      title.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(() => {
      root.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    await settle();
    expect(calls).toContain(`PATCH /api/v1/items/${A}`);
  });

  it("explains an absent preview without implying that the original is lost", async () => {
    const cases: [string, string][] = [
      ["failed", PREVIEW_MESSAGES.failed],
      ["skipped", PREVIEW_MESSAGES.missing],
    ];
    for (const [status, message] of cases) {
      const calls = open(image(A, { thumbnail_available: false }), [run("thumbnail", status)]);
      const root = await mount(<ItemView id={A} />);
      expect(imgs(root)).toHaveLength(0);
      expect(root.querySelector(".thumb")?.textContent).toContain(message);
      expect(root.querySelector(".thumb")?.textContent).toContain(ORIGINAL_SAFE);
      expect(root.textContent).toContain("Original herunterladen");
      expect(calls.some((call) => call.includes("/thumbnail"))).toBe(false);
      unmountAll();
    }
  });

  it("looks again while the preview is being made, without disturbing what is being typed", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    let ready = false;
    const calls = stubFetch((url) => {
      if (url.pathname === `/api/v1/items/${A}`) {
        return detail(image(A, { thumbnail_available: ready, processing_status: ready ? "ready" : "processing", title: "Server" }));
      }
      if (url.pathname === `/api/v1/items/${A}/processing`) return [run("thumbnail", ready ? "succeeded" : "running")];
      return undefined;
    });
    const root = await mount(<ItemView id={A} />);
    expect(root.querySelector(".thumb")?.getAttribute("data-preview")).toBe("pending");
    expect(root.textContent).toContain(PREVIEW_MESSAGES.pending);

    const title = root.querySelector<HTMLInputElement>("label input")!;
    await act(() => {
      title.value = "Halb getippt";
      title.dispatchEvent(new Event("input", { bubbles: true }));
    });
    ready = true;
    await act(() => {
      vi.advanceTimersByTime(PREVIEW_POLL_MS);
    });
    await settle();

    expect(imgs(root).map((node) => node.getAttribute("src"))).toEqual([`/api/v1/items/${A}/thumbnail`]);
    expect(root.querySelector<HTMLInputElement>("label input")?.value).toBe("Halb getippt");
    const before = calls.length;
    await act(() => {
      vi.advanceTimersByTime(PREVIEW_POLL_MS * 3);
    });
    await settle();
    expect(calls.length).toBe(before);
  });

  it("stops looking after a bounded number of attempts", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const calls = stubFetch((url) => {
      if (url.pathname === `/api/v1/items/${A}`) return detail(image(A, { thumbnail_available: false, processing_status: "queued" }));
      if (url.pathname === `/api/v1/items/${A}/processing`) return [run("thumbnail", "pending", { started_at: null })];
      return undefined;
    });
    await mount(<ItemView id={A} />);
    const initial = calls.length;
    for (let step = 0; step < PREVIEW_POLL_LIMIT + 5; step += 1) {
      await act(() => {
      vi.advanceTimersByTime(PREVIEW_POLL_MS);
    });
      await settle();
    }
    expect(calls.length - initial).toBe(PREVIEW_POLL_LIMIT * 2);
  });

  it("leaves detail views of other kinds unchanged", async () => {
    for (const item of [
      summary({ id: A, kind: "text", thumbnail_available: true }),
      summary({ id: A, kind: "url", source_url: "https://example.org/x.png", thumbnail_available: true }),
      summary({ id: A, kind: "file", original_filename: "a.zip" }),
    ]) {
      stubFetch((url) => (url.pathname.endsWith("/processing") ? [] : url.pathname === `/api/v1/items/${A}` ? detail(item) : undefined));
      const root = await mount(<ItemView id={A} />);
      expect(root.querySelector(".detail-preview")).toBeNull();
      expect(imgs(root)).toHaveLength(0);
      unmountAll();
    }
  });
});

describe("responsive layout", () => {
  const rule = (selector: string) => {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const match = new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`).exec(styles);
    expect(match, selector).not.toBeNull();
    return match?.[1] ?? "";
  };

  it("scales feed pictures uniformly inside a fixed frame beside the text: 96 px square on phones, 168×126 from 560 px", () => {
    expect(rule(".thumb.card img")).toMatch(/object-fit:\s*contain/);
    const phone = rule(".thumb.card");
    expect(phone).toMatch(/flex:\s*0 0 96px/);
    expect(phone).toMatch(/width:\s*96px/);
    expect(phone).toMatch(/height:\s*96px/);
    // The frame must not inherit the .card container's margin and padding (it shares the class name).
    expect(phone).toMatch(/margin:\s*0/);
    expect(phone).toMatch(/padding:\s*0/);
    expect(styles).not.toMatch(/\.item\.with-thumb\s*\{[^}]*flex-direction:\s*column/);
    const wide = /@media \(min-width: 560px\) \{([\s\S]*?)\n\}/.exec(styles.slice(styles.indexOf("Image previews")))?.[1] ?? "";
    expect(wide).toMatch(/\.thumb\.card\s*\{[^}]*flex-basis:\s*168px[^}]*width:\s*168px[^}]*height:\s*126px/);
  });

  it("shows the detail picture at its own size, capped at the 512 px thumbnail limit", () => {
    const detailImg = rule(".thumb.detail img");
    expect(detailImg).toMatch(/width:\s*auto/);
    expect(detailImg).toMatch(/height:\s*auto/);
    expect(detailImg).toMatch(/max-width:\s*100%/);
    expect(detailImg).toMatch(/max-height:\s*512px/);
    expect(detailImg).not.toMatch(/min-|object-fit:\s*(cover|fill)/);
  });

  it("never fixes both dimensions on the element itself, which would distort the aspect ratio", async () => {
    stubFetch((url) => (url.pathname === "/api/v1/items" ? { items: [image(A)], next_cursor: null } : []));
    const root = await mount(<Feed route={FEED} share={null} />);
    const picture = imgs(root)[0]!;
    expect(picture.hasAttribute("width") || picture.hasAttribute("height") || picture.hasAttribute("style")).toBe(false);
  });
});
