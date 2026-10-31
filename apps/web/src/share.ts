import { looksLikeUrl } from "./api";

/** Raw Web Share Target fields; browsers may omit any of them or repeat one. */
export interface SharePayload {
  url: string[];
  title: string[];
  text: string[];
}

/** What the capture form starts with: the main content plus a prefilled note. */
export interface ShareDraft {
  content: string;
  note: string;
}

export const SHARE_FIELDS = ["url", "title", "text"] as const;

export function sharePayload(params: URLSearchParams): SharePayload {
  return { url: params.getAll("url"), title: params.getAll("title"), text: params.getAll("text") };
}

const URL_TOKEN = /https?:\/\/[^\s<>"\\]+/gi;

/** URLs inside free text, without sentence punctuation or an unbalanced closing bracket. */
function urlsIn(value: string): string[] {
  return (value.match(URL_TOKEN) ?? [])
    .map((token) => {
      let url = token.replace(/[.,;:!?'"»”’]+$/, "");
      for (const [open, close] of [["(", ")"], ["[", "]"]] as const) {
        while (url.endsWith(close) && url.split(open).length < url.split(close).length) {
          url = url.slice(0, -1);
        }
      }
      return url;
    })
    .filter(looksLikeUrl);
}

const unique = (values: string[]): string[] => [...new Set(values)];

/**
 * A shared link becomes the URL capture even when the browser adds a title or
 * text; everything else moves into the note so nothing is silently dropped.
 * Several different links in free text are ambiguous and stay a text capture.
 */
export function shareDraft(payload: SharePayload): ShareDraft {
  const clean = (values: string[]) => values.map((value) => value.trim()).filter(Boolean);
  const fields = [...clean(payload.url), ...clean(payload.title), ...clean(payload.text)];
  let link = clean(payload.url).find(looksLikeUrl) ?? null;
  if (link === null) {
    const found = unique([...clean(payload.title), ...clean(payload.text)].flatMap(urlsIn));
    if (found.length === 1) link = found[0] ?? null;
  }
  if (link === null) return { content: unique(fields).join("\n"), note: "" };
  const target = link;
  const rest = fields.map((value) =>
    value
      .split(target)
      .map((part) => part.trim())
      .filter(Boolean)
      .join(" "),
  );
  return { content: target, note: unique(rest.filter(Boolean)).join("\n") };
}
