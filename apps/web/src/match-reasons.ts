import type { MatchedField } from "./api";

const LABELS: Record<MatchedField, string> = {
  title: "Titel",
  filename: "Dateiname",
  tag: "Tag",
  note: "Notiz",
  url: "Link",
  content: "Inhalt",
};

/**
 * The subtle "found in" line of a search result; null when the server named no field.
 * Whole-word results rank 1 and above, partial ones (start or part of a word) below 1
 * (ADR-0025), so a rank below 1 is shown as a partial hit.
 */
export function matchReasons(fields: readonly MatchedField[] | undefined, rank?: number): string | null {
  const labels = (fields ?? []).flatMap((field) => LABELS[field] ?? []);
  if (labels.length === 0) return null;
  const prefix = rank !== undefined && rank < 1 ? "Teiltreffer in" : "Gefunden in";
  return `${prefix}: ${labels.join(", ")}`;
}
