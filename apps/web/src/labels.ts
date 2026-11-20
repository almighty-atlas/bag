/**
 * Words for the person, not for the server: item kinds, processing states and processor names.
 * Unknown values from a newer server are shown as sent instead of being hidden.
 */

const KIND: Record<string, string> = {
  text: "Text",
  url: "Link",
  image: "Bild",
  document: "Dokument",
  file: "Datei",
};

const ITEM_STATUS: Record<string, string> = {
  queued: "wartet auf Verarbeitung",
  processing: "wird verarbeitet",
  ready: "fertig",
  partial: "teilweise verarbeitet",
  failed: "Verarbeitung fehlgeschlagen",
};

const RUN_STATUS: Record<string, string> = {
  pending: "wartet",
  running: "läuft",
  succeeded: "erledigt",
  failed: "fehlgeschlagen",
  skipped: "übersprungen",
};

const PROCESSOR: Record<string, string> = {
  mime_detect: "Dateityp erkennen",
  text_extract: "Text auslesen",
  language: "Sprache erkennen",
  url_fetch: "Webseite abrufen",
  image_meta: "Bildgröße lesen",
  pdf_text: "PDF-Text auslesen",
  thumbnail: "Vorschaubild erstellen",
};

const LANGUAGE: Record<string, string> = { de: "Deutsch", en: "Englisch" };

const label = (table: Record<string, string>, value: string): string =>
  Object.hasOwn(table, value) ? (table[value] as string) : value;

export const kindLabel = (kind: string): string => label(KIND, kind);
export const itemStatusLabel = (status: string): string => label(ITEM_STATUS, status);
export const runStatusLabel = (status: string): string => label(RUN_STATUS, status);
export const processorLabel = (processor: string): string => label(PROCESSOR, processor);
export const languageLabel = (language: string): string => label(LANGUAGE, language);
