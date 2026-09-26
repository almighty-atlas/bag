import { useEffect, useState } from "preact/hooks";

import { ApiError, api, contentUrl, type ItemDetail, type ProcessingRun } from "./api";

export function ItemView({ id }: { id: string }) {
  const [item, setItem] = useState<ItemDetail | null>(null);
  const [runs, setRuns] = useState<ProcessingRun[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [title, setTitle] = useState("");
  const [tags, setTags] = useState("");
  const [collections, setCollections] = useState("");
  const [language, setLanguage] = useState("");
  const [saving, setSaving] = useState(false);

  const load = async () => {
    try {
      const [detail, processing] = await Promise.all([api.item(id), api.processing(id)]);
      setItem(detail);
      setRuns(processing);
      setNote(detail.user_note ?? "");
      setTitle(detail.title ?? "");
      setTags(detail.tags.join(", "));
      setCollections(detail.collections.join(", "));
      setLanguage(detail.language ?? "");
    } catch (failure: unknown) {
      setError(
        failure instanceof ApiError && failure.status === 404
          ? "Dieses Element gibt es nicht oder es liegt im Papierkorb."
          : "Laden fehlgeschlagen.",
      );
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  if (error) return <p class="error">{error}</p>;
  if (!item) return <p>Lade…</p>;

  const save = async (event: Event) => {
    event.preventDefault();
    setSaving(true);
    try {
      const names = (value: string) =>
        value
          .split(",")
          .map((name) => name.trim())
          .filter(Boolean);
      const patch: Parameters<typeof api.update>[1] = {
        title: title.trim() || null,
        user_note: note.trim() || null,
        tags: names(tags),
        collections: names(collections),
      };
      // Only send the language when the user changed it: a value marks a user choice.
      if (language !== (item.language ?? "")) {
        patch.language = language === "de" || language === "en" ? language : null;
      }
      const updated = await api.update(id, patch);
      setItem(updated);
      setLanguage(updated.language ?? "");
    } catch (failure: unknown) {
      setError(failure instanceof ApiError ? failure.message : "Speichern fehlgeschlagen.");
    } finally {
      setSaving(false);
    }
  };

  const trash = async () => {
    await api.trash(id);
    location.hash = "#/";
  };

  const hasOriginalFile = item.kind !== "text" && item.kind !== "url";

  return (
    <article class="detail">
      <p class="meta">
        {item.kind} · erfasst {new Date(item.captured_at).toLocaleString()} · Status {item.processing_status}
        {item.language && <> · Sprache {item.language}</>}
      </p>
      <form class="card" onSubmit={save}>
        <label>
          Titel
          <input value={title} onInput={(e) => setTitle((e.target as HTMLInputElement).value)} />
        </label>
        <label>
          Gedanke
          <textarea rows={2} value={note} onInput={(e) => setNote((e.target as HTMLTextAreaElement).value)} />
        </label>
        <label>
          Tags (durch Komma getrennt)
          <input value={tags} onInput={(e) => setTags((e.target as HTMLInputElement).value)} />
        </label>
        <label>
          Sammlungen (durch Komma getrennt)
          <input
            value={collections}
            onInput={(e) => setCollections((e.target as HTMLInputElement).value)}
          />
        </label>
        <label>
          Sprache für die Suche
          <select value={language} onChange={(e) => setLanguage((e.target as HTMLSelectElement).value)}>
            <option value="">automatisch erkennen</option>
            <option value="de">Deutsch</option>
            <option value="en">Englisch</option>
          </select>
        </label>
        <div class="row">
          <button type="submit" disabled={saving}>
            Speichern
          </button>
          {hasOriginalFile && (
            <a class="button" href={contentUrl(id)} download={item.original_filename ?? undefined}>
              Original herunterladen
            </a>
          )}
          {item.source_url && (
            <a class="button" href={item.source_url} target="_blank" rel="noopener noreferrer nofollow">
              Link öffnen
            </a>
          )}
          <button type="button" class="danger" onClick={() => void trash()}>
            In den Papierkorb
          </button>
        </div>
      </form>
      {item.content && item.kind === "text" && (
        <section class="card">
          <h2>Original</h2>
          <pre class="original">{item.content}</pre>
        </section>
      )}
      {item.extracted_text && item.kind !== "text" && (
        <section class="card">
          <h2>Extrahierter Text</h2>
          <pre class="original">{item.extracted_text}</pre>
        </section>
      )}
      <section class="card">
        <h2>Verarbeitung</h2>
        <table>
          <tbody>
            {runs.map((run) => (
              <tr key={run.processor}>
                <td>{run.processor}</td>
                <td class={`status-${run.status}`}>{run.status}</td>
                <td>{run.attempts}×</td>
                <td class="muted">{run.last_error ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <button type="button" class="link" onClick={() => void api.reprocess(id).then(setRuns)}>
          Erneut verarbeiten
        </button>
      </section>
    </article>
  );
}
