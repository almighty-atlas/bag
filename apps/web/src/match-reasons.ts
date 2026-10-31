import type { MatchedField } from "./api";

const LABELS: Record<MatchedField, string> = {
  title: "Titel",
  filename: "Dateiname",
  tag: "Tag",
  note: "Notiz",
  url: "Link",
  content: "Inhalt",
};

/** The subtle "found in" line of a search result; null when the server named no field. */
export function matchReasons(fields: readonly MatchedField[] | undefined): string | null {
  const labels = (fields ?? []).flatMap((field) => LABELS[field] ?? []);
  return labels.length > 0 ? `Gefunden in: ${labels.join(", ")}` : null;
}
