import { useEffect, useState } from "preact/hooks";

import { ApiError, organize, type NamedEntry, type NamedKind } from "./api";
import { EMPTY_FILTERS, FEED, href } from "./app";

const LABEL: Record<NamedKind, string> = { tag: "Tags", collection: "Sammlungen" };

/** Browse, rename and delete tags and collections; names link to the filtered feed. */
export function Organize() {
  return (
    <>
      <NamedList kind="tag" />
      <NamedList kind="collection" />
    </>
  );
}

function NamedList({ kind }: { kind: NamedKind }) {
  const [rows, setRows] = useState<NamedEntry[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = () =>
    organize
      .list(kind)
      .then(setRows)
      .catch(() => setError("Laden fehlgeschlagen."));

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind]);

  const rename = async (row: NamedEntry) => {
    const name = prompt(`Neuer Name für „${row.name}“:`, row.name)?.trim();
    if (!name || name === row.name) return;
    setError(null);
    try {
      await organize.rename(kind, row.id, name);
      await load();
    } catch (failure: unknown) {
      setError(
        failure instanceof ApiError && failure.status === 409
          ? "Diesen Namen gibt es schon."
          : "Umbenennen fehlgeschlagen.",
      );
    }
  };

  const remove = async (row: NamedEntry) => {
    if (!confirm(`„${row.name}“ löschen? Die Elemente bleiben erhalten, nur die Zuordnung verschwindet.`)) return;
    await organize.remove(kind, row.id);
    await load();
  };

  return (
    <section>
      <h1>{LABEL[kind]}</h1>
      {error && <p class="error">{error}</p>}
      {rows.length === 0 && <p class="hint">Noch keine {LABEL[kind]}. Sie entstehen beim Bearbeiten eines Elements.</p>}
      <table>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <td>
                <a href={href({ ...FEED, filters: { ...EMPTY_FILTERS, [kind]: row.name } })}>{row.name}</a>
              </td>
              <td class="muted">{row.item_count} Elemente</td>
              <td>
                <button type="button" class="link" onClick={() => void rename(row)}>
                  umbenennen
                </button>
              </td>
              <td>
                <button type="button" class="link" onClick={() => void remove(row)}>
                  löschen
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
