import { useEffect, useRef, useState } from "preact/hooks";

import { api, contentUrl } from "./api";
import { copyText } from "./clipboard";
import { Notice, type NoticeState } from "./notice";

/** What the actions need of an item; feed rows and item details both have it. */
export interface ActionTarget {
  id: string;
  kind: string;
  title: string | null;
  source_url: string | null;
  original_filename: string | null;
  /** The text of a text item when already loaded (item detail); the feed loads it on demand. */
  content?: string | null;
  /** Merkliste state (ADR-0028); absent where pinning is not offered. */
  pinned_at?: string | null;
}

export const hasOriginalFile = (item: Pick<ActionTarget, "kind">): boolean =>
  item.kind !== "text" && item.kind !== "url";

/** A link back to the item in this Bag; only useful to someone signed in to it. */
export function itemLink(id: string): string {
  return `${location.origin}${location.pathname}#/items/${id}`;
}

/** What "copy" puts on the clipboard: the text, the saved link, or the item's Bag link. */
export function copyLabel(item: Pick<ActionTarget, "kind">): string {
  if (item.kind === "text") return "Text kopieren";
  if (item.kind === "url") return "Link kopieren";
  return "Link zum Eintrag kopieren";
}

function textOf(item: ActionTarget): () => Promise<string> {
  return async () => {
    if (typeof item.content === "string") return item.content;
    const detail = await api.item(item.id);
    return detail.content ?? "";
  };
}

export function copySource(item: ActionTarget): string | (() => Promise<string>) {
  if (item.kind === "text") return typeof item.content === "string" ? item.content : textOf(item);
  if (item.kind === "url" && item.source_url) return item.source_url;
  return itemLink(item.id);
}

/** Whether the system share sheet is available at all (mostly phones and Safari). */
export function canShare(): boolean {
  return typeof navigator !== "undefined" && typeof navigator.share === "function";
}

/** Whether files can go through the share sheet; asked with an empty probe file. */
export function canShareFiles(): boolean {
  if (!canShare() || typeof navigator.canShare !== "function") return false;
  try {
    return navigator.canShare({ files: [new File([], "probe.txt", { type: "text/plain" })] });
  } catch {
    return false;
  }
}

/** The share sheet payload: text or link directly; files are fetched as the original. */
export async function sharePayload(item: ActionTarget): Promise<ShareData> {
  const title = item.title ?? item.original_filename ?? undefined;
  const base: ShareData = title ? { title } : {};
  if (item.kind === "text") return { ...base, text: await textOf(item)() };
  if (item.kind === "url" && item.source_url) return { ...base, url: item.source_url };
  const response = await fetch(contentUrl(item.id), { credentials: "same-origin" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const blob = await response.blob();
  const file = new File([blob], item.original_filename ?? "Datei", { type: blob.type });
  return { ...base, files: [file] };
}

const isAbort = (error: unknown) => error instanceof DOMException && error.name === "AbortError";

/**
 * Open, copy, share and download for one item. `onDone` lets a menu close after an action;
 * the outcome stays visible next to the actions.
 */
export function ItemActions({
  item,
  onDone,
  onPinned,
}: {
  item: ActionTarget;
  onDone?: () => void;
  /** Called with the server's new pin state; without it no Merken button is shown. */
  onPinned?: (pinnedAt: string | null) => void;
}) {
  const [status, setStatus] = useState<NoticeState | null>(null);
  const [busy, setBusy] = useState(false);
  const shareable = canShare() && (item.kind === "text" || item.kind === "url" || canShareFiles());
  const pinned = Boolean(item.pinned_at);

  // Only the server's answer changes the state shown; a failed request leaves it as it was.
  const togglePin = async () => {
    setBusy(true);
    try {
      const updated = pinned ? await api.unpin(item.id) : await api.pin(item.id);
      onPinned?.(updated.pinned_at);
      setStatus({ tone: "success", text: updated.pinned_at ? "Gemerkt." : "Von der Merkliste genommen." });
    } catch {
      setStatus({ tone: "error", text: pinned ? "Nicht von der Merkliste genommen." : "Nicht gemerkt. Bitte erneut versuchen." });
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    setBusy(true);
    try {
      await copyText(copySource(item));
      setStatus({ tone: "success", text: "In die Zwischenablage kopiert." });
    } catch {
      setStatus({ tone: "error", text: "Kopieren nicht möglich: der Browser erlaubt keinen Zugriff auf die Zwischenablage." });
    } finally {
      setBusy(false);
      onDone?.();
    }
  };

  const share = async () => {
    setBusy(true);
    try {
      await navigator.share(await sharePayload(item));
      setStatus(null);
    } catch (error: unknown) {
      // Closing the share sheet is a choice, not an error.
      if (!isAbort(error)) {
        setStatus({
          tone: "error",
          text: hasOriginalFile(item)
            ? "Teilen nicht möglich. Bitte herunterladen und dann teilen."
            : "Teilen nicht möglich. Bitte kopieren und einfügen.",
        });
      }
    } finally {
      setBusy(false);
      onDone?.();
    }
  };

  return (
    <div class="item-actions">
      <div class="actions-list">
        {onPinned && (
          <button type="button" class="pin" aria-pressed={pinned} disabled={busy} onClick={() => void togglePin()}>
            <span aria-hidden="true">{pinned ? "★" : "☆"}</span> {pinned ? "Gemerkt" : "Merken"}
          </button>
        )}
        {item.source_url && (
          <a class="button" href={item.source_url} target="_blank" rel="noopener noreferrer nofollow" onClick={onDone}>
            Link öffnen
          </a>
        )}
        {hasOriginalFile(item) && (
          <a class="button" href={contentUrl(item.id)} download={item.original_filename ?? undefined} onClick={onDone}>
            Original herunterladen
          </a>
        )}
        <button type="button" disabled={busy} onClick={() => void copy()}>
          {copyLabel(item)}
        </button>
        {shareable && (
          <button type="button" disabled={busy} onClick={() => void share()}>
            Teilen…
          </button>
        )}
      </div>
      {status && <Notice tone={status.tone}>{status.text}</Notice>}
    </div>
  );
}

/**
 * The feed's compact "⋯" menu. A details element keeps it usable without scripts, by keyboard
 * and by touch; Escape and a tap outside close it.
 */
export function ItemMenu({
  item,
  label,
  onPinned,
}: {
  item: ActionTarget;
  label: string;
  onPinned?: (pinnedAt: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const menu = useRef<HTMLDetailsElement>(null);

  useEffect(() => {
    if (!open) return;
    const outside = (event: Event) => {
      if (menu.current && !menu.current.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      menu.current?.querySelector("summary")?.focus();
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  return (
    <details
      ref={menu}
      class="row-actions"
      open={open}
      onToggle={(event) => setOpen((event.currentTarget as HTMLDetailsElement).open)}
    >
      <summary aria-label={`Aktionen für ${label}`} title="Aktionen">
        <span aria-hidden="true">⋯</span>
      </summary>
      {/* Rendered only while open: a closed row carries no extra links or buttons. */}
      {open && (
        <div class="actions-menu">
          <ItemActions item={item} {...(onPinned ? { onPinned } : {})} />
        </div>
      )}
    </details>
  );
}
