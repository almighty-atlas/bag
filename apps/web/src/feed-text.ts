import type { ItemSummary } from "./api";

export type FeedItem = ItemSummary & { snippet?: string };

export interface FeedText {
  /** Primary line of a feed card. */
  headline: string;
  /** Secondary lines: whatever else is known and not already shown as the headline. */
  details: string[];
}

const same = (a: string, b: string): boolean => a.split(/\s+/).join(" ").trim() === b.split(/\s+/).join(" ").trim();

/**
 * Title wins, then (text items only) the stored text, then file name, link and note. A note or
 * excerpt that is not the headline stays visible below it, so adding a title or note never hides
 * the other. Search results show the server's match snippet instead of the static lines.
 */
export function feedText(item: FeedItem): FeedText {
  const excerpt = item.kind === "text" ? item.excerpt : null;
  const headline = item.title ?? excerpt ?? item.original_filename ?? item.source_url ?? item.user_note ?? item.kind;
  if (item.snippet !== undefined) return { headline, details: [item.snippet] };
  const details: string[] = [];
  for (const line of [item.user_note, excerpt]) {
    if (line && !same(line, headline) && !details.some((shown) => same(shown, line))) details.push(line);
  }
  return { headline, details };
}
