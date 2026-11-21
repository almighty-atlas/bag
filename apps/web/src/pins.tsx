import { useEffect, useState } from "preact/hooks";

import { ApiError, api } from "./api";
import { FEED } from "./app";
import { ItemRow, type Row } from "./item-row";
import { Notice, type NoticeState } from "./notice";

export const PINS_PAGE = 50;

/**
 * The Merkliste (ADR-0028): pinned items, newest pin first, from the server, so every device
 * shows the same list. Unpinning removes the row and offers to undo it.
 */
export function Pins() {
  const [rows, setRows] = useState<Row[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [undo, setUndo] = useState<{ row: Row } | null>(null);
  const [status, setStatus] = useState<NoticeState | null>(null);

  const load = async (offset: number) => {
    setLoading(true);
    setError(null);
    try {
      const page = await api.pins({ limit: String(PINS_PAGE), offset: String(offset) });
      setRows((old) => (offset > 0 ? [...old, ...page.items] : page.items));
      setNext(page.next_offset);
    } catch (failure: unknown) {
      setError(failure instanceof ApiError ? failure.message : "Laden fehlgeschlagen.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load(0);
  }, []);

  const changed = (row: Row, pinnedAt: string | null) => {
    if (pinnedAt !== null) {
      setRows((old) => old.map((entry) => (entry.id === row.id ? { ...entry, pinned_at: pinnedAt } : entry)));
      return;
    }
    setRows((old) => old.filter((entry) => entry.id !== row.id));
    setUndo({ row });
    setStatus(null);
  };

  const restorePin = async () => {
    if (!undo) return;
    try {
      const updated = await api.pin(undo.row.id);
      // Pinned again now, so it belongs at the top of the list.
      setRows((old) => [{ ...undo.row, pinned_at: updated.pinned_at }, ...old.filter((r) => r.id !== undo.row.id)]);
      setUndo(null);
    } catch {
      setStatus({ tone: "error", text: "Nicht wieder gemerkt. Bitte erneut versuchen." });
    }
  };

  return (
    <section aria-labelledby="pins-heading">
      <h1 id="pins-heading">Merkliste</h1>
      <p class="hint">
        Gemerkte Einträge, zuletzt gemerkt zuerst, auf allen Geräten gleich. Merken ändert nichts am Eintrag selbst.
      </p>
      {error && <Notice tone="error">{error}</Notice>}
      {undo && (
        <Notice tone="success">
          Von der Merkliste genommen.{" "}
          <span class="notice-actions">
            <button type="button" class="link" onClick={() => void restorePin()}>
              Rückgängig
            </button>
          </span>
        </Notice>
      )}
      {status && <Notice tone={status.tone}>{status.text}</Notice>}
      {rows.length === 0 && !loading && !error && (
        <p class="hint">Noch nichts gemerkt. Im Menü „⋯“ eines Eintrags oder in seiner Ansicht auf „Merken“ tippen.</p>
      )}
      <ul class="items">
        {rows.map((row) => (
          <ItemRow
            key={row.id}
            row={row}
            route={FEED}
            trashed={false}
            onPinned={(pinnedAt) => changed(row, pinnedAt)}
          />
        ))}
      </ul>
      {loading && <p>Lade…</p>}
      {next !== null && !loading && (
        // Continue after the rows shown: unpinning here removed some, so `next` would skip as many.
        <button type="button" onClick={() => void load(rows.length)}>
          Mehr laden
        </button>
      )}
    </section>
  );
}
