import { ItemMenu } from "./actions";
import type { MatchedField } from "./api";
import { href, type FeedRoute } from "./app";
import { feedText, type FeedItem } from "./feed-text";
import { itemStatusLabel, kindLabel } from "./labels";
import { matchReasons } from "./match-reasons";
import { Thumbnail, previewState, thumbnailAlt } from "./thumbnail";

export type Row = FeedItem & { matched_fields?: MatchedField[]; rank?: number; pinned_at: string | null };

export const KIND_ICON: Record<string, string> = {
  text: "📝",
  url: "🔗",
  image: "🖼️",
  document: "📄",
  file: "📦",
};

/**
 * One entry of the feed, search results or Merkliste: a link to the item, and beside it the
 * "⋯" actions (or, in the trash, the restore button). Tag links filter `route`.
 */
export function ItemRow({
  row,
  route,
  trashed,
  onRestore,
  onPinned,
}: {
  row: Row;
  route: FeedRoute;
  trashed: boolean;
  onRestore?: () => void;
  onPinned?: (pinnedAt: string | null) => void;
}) {
  const text = feedText(row);
  const reasons = matchReasons(row.matched_fields, row.rank);
  const preview = previewState(row, { trashed: trashed || row.deleted_at !== null });
  const withThumb = preview !== "none" && preview !== "trashed";
  return (
    <li class={trashed ? "item-row" : "item-row with-menu"}>
      {trashed && onRestore && (
        <button type="button" class="restore" onClick={onRestore}>
          Wiederherstellen
        </button>
      )}
      <a href={trashed ? undefined : href({ name: "item", id: row.id })} class={withThumb ? "item with-thumb" : "item"}>
        {withThumb ? (
          <Thumbnail key={row.id} id={row.id} alt={thumbnailAlt(row)} state={preview} variant="card" />
        ) : (
          <span class="icon" aria-hidden="true">
            {KIND_ICON[row.kind] ?? "❔"}
          </span>
        )}
        <span class="body">
          <span class={text.headline === row.excerpt ? "title clamp" : "title"}>{text.headline}</span>
          {text.details.map((line) => (
            <span class={line === row.excerpt ? "snippet clamp" : "snippet"} key={line}>
              {line}
            </span>
          ))}
          {reasons && <span class="reasons">{reasons}</span>}
          <span class="meta">
            {row.pinned_at && (
              <span class="pinned">
                <span aria-hidden="true">★</span> gemerkt ·{" "}
              </span>
            )}
            {new Date(row.captured_at).toLocaleString()} · {kindLabel(row.kind)}
            {row.processing_status !== "ready" && <> · {itemStatusLabel(row.processing_status)}</>}
            {row.tags.map((tag) => (
              <a
                class="tag"
                key={tag}
                href={href({ ...route, filters: { ...route.filters, tag } })}
                onClick={(e) => e.stopPropagation()}
              >
                {tag}
              </a>
            ))}
          </span>
        </span>
      </a>
      {!trashed && <ItemMenu item={row} label={text.headline} {...(onPinned ? { onPinned } : {})} />}
    </li>
  );
}
