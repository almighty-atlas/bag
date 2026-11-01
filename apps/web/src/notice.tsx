import type { ComponentChildren } from "preact";

/**
 * How a message must be read. `success` is only for outcomes the server confirmed; anything the
 * person still has to act on or cannot be sure about is `warning`, a definite failure is `error`.
 */
export type Tone = "success" | "warning" | "error";

export interface NoticeState {
  tone: Tone;
  text: string;
}

const MARK: Record<Tone, { glyph: string; label: string }> = {
  success: { glyph: "✓", label: "Erfolg" },
  warning: { glyph: "⚠", label: "Warnung" },
  error: { glyph: "✕", label: "Fehler" },
};

/** Color is never the only cue: each tone has its own glyph and a spoken label. Errors interrupt, the rest wait. */
export function Notice({ tone, children }: { tone: Tone; children: ComponentChildren }) {
  const mark = MARK[tone];
  return (
    <p class={`notice ${tone}`} role={tone === "error" ? "alert" : "status"}>
      <span class="notice-mark" aria-hidden="true">
        {mark.glyph}
      </span>
      <span class="sr-only">{mark.label}: </span>
      <span class="notice-text">{children}</span>
    </p>
  );
}
