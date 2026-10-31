import { useState } from "preact/hooks";

import { thumbnailUrl, type ProcessingRun } from "./api";

/**
 * What a card or the detail view shows for an item's preview. Only images have one, and only
 * the thumbnail endpoint ever supplies it: a re-encoded WebP, never the original (ADR-0023).
 */
export type PreviewState =
  /** Not an image: the card stays exactly as it was. */
  | "none"
  /** The endpoint refuses trashed items, so nothing is requested. */
  | "trashed"
  /** The server has a stored thumbnail to try; loading it can still fail. */
  | "try"
  | "pending"
  | "missing"
  | "failed";

interface Previewable {
  kind: string;
  thumbnail_available: boolean;
  processing_status: string;
}

/**
 * Decided from the summary alone, so a feed needs no per-card status request. The detail view
 * also passes the item's own `thumbnail` run, which tells a failure from "not yet" and "skipped".
 */
export function previewState(
  item: Previewable,
  options: { trashed?: boolean; run?: ProcessingRun | undefined } = {},
): PreviewState {
  if (item.kind !== "image") return "none";
  if (options.trashed) return "trashed";
  if (item.thumbnail_available) return "try";
  const { run } = options;
  if (run) {
    if (run.status === "pending" || run.status === "running") return "pending";
    return run.status === "failed" ? "failed" : "missing";
  }
  return item.processing_status === "queued" || item.processing_status === "processing" ? "pending" : "missing";
}

export const PREVIEW_MESSAGES = {
  pending: "Vorschau wird erstellt…",
  missing: "Keine Vorschau verfügbar",
  failed: "Vorschau konnte nicht erstellt werden",
  unloadable: "Vorschau konnte nicht geladen werden",
} as const;

/** What the person is told next to a missing preview: the original is a separate, intact object. */
export const ORIGINAL_SAFE = "Das Original ist unverändert gespeichert.";

/** A short, meaningful name for the picture: the title or file name, never the stored text. */
export function thumbnailAlt(item: { title: string | null; original_filename: string | null }): string {
  return `Vorschau: ${item.title ?? item.original_filename ?? "Bild"}`;
}

interface ThumbnailProps {
  id: string;
  alt: string;
  state: PreviewState;
  variant: "card" | "detail";
}

/**
 * The preview or a readable stand-in. A failed request swaps the image for text, so no broken
 * image icon or permanently empty frame ever appears, and it affects no other card.
 */
export function Thumbnail({ id, alt, state, variant }: ThumbnailProps) {
  const [load, setLoad] = useState<"loading" | "loaded" | "failed">("loading");
  if (state === "none" || state === "trashed") return null;

  const message =
    state === "try" ? (load === "failed" ? PREVIEW_MESSAGES.unloadable : null) : PREVIEW_MESSAGES[state];
  if (message !== null) {
    return (
      <span class={`thumb ${variant} note`} data-preview={load === "failed" ? "unloadable" : state}>
        <span class="thumb-icon" aria-hidden="true">
          🖼️
        </span>
        <span class="thumb-text" role={variant === "detail" ? "status" : undefined}>
          {message}
          {variant === "detail" && state !== "pending" && <> {ORIGINAL_SAFE}</>}
        </span>
      </span>
    );
  }
  return (
    <span class={`thumb ${variant} ${load}`} data-preview={load}>
      {load === "loading" && (
        <span class="thumb-icon" aria-hidden="true">
          🖼️
        </span>
      )}
      <img
        src={thumbnailUrl(id)}
        alt={alt}
        loading="lazy"
        decoding="async"
        onLoad={() => setLoad("loaded")}
        onError={() => setLoad("failed")}
      />
    </span>
  );
}
