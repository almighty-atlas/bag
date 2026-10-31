import { useEffect, useState } from "preact/hooks";

import { href } from "./app";
import { describeBatch, discardBatch, listBatches, remainingFiles, type ShareBatch } from "./share-store";

export const DISCARD_CONFIRMATION =
  "Diesen geteilten Inhalt endgültig verwerfen? Er wurde noch nicht in der Tasche gespeichert.";

/** Shared content that reached this device but is not saved yet; nothing leaves the list unasked. */
export function PendingShares({ exclude }: { exclude: string | null }) {
  const [batches, setBatches] = useState<(ShareBatch & { open: number })[]>([]);

  const refresh = async () => {
    const listed = await listBatches();
    const open = await Promise.all(listed.map(async (batch) => (await remainingFiles(batch)).length));
    setBatches(listed.map((batch, index) => ({ ...batch, open: open[index] ?? 0 })));
  };

  useEffect(() => {
    void refresh().catch(() => setBatches([]));
  }, []);

  const discard = async (batch: ShareBatch) => {
    if (!confirm(DISCARD_CONFIRMATION)) return;
    await discardBatch(batch);
    await refresh();
  };

  const shown = batches.filter((batch) => batch.id !== exclude);
  if (shown.length === 0) return null;
  return (
    <section class="card pending" aria-label="Offene geteilte Inhalte">
      <h2>Noch nicht gespeichert ({shown.length})</h2>
      <ul>
        {shown.map((batch) => (
          <li key={batch.id}>
            <span>
              {describeBatch(batch)}
              {batch.open > 0 && <> · {batch.open} Datei(en)</>}
              <span class="muted"> · {new Date(batch.created_at).toLocaleString()}</span>
            </span>{" "}
            <a class="button" href={href({ name: "share", batch: batch.id, payload: { url: [], title: [], text: [] } })}>
              Öffnen und speichern
            </a>{" "}
            <button type="button" class="link" onClick={() => void discard(batch)}>
              Verwerfen
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
