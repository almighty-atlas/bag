import { useEffect, useState } from "preact/hooks";

import { ApiError, api, contentUrl, snapshotUrl, type ItemDetail, type ProcessingRun } from "./api";
import { Notice } from "./notice";
import { Thumbnail, previewState, thumbnailAlt } from "./thumbnail";

/** A preview that is still being made is looked up again a few times, then left to a reload. */
export const PREVIEW_POLL_MS = 4000;
export const PREVIEW_POLL_LIMIT = 15;

/** Snapshot HTML with a policy that blocks every network load and script before the page's own head. */
export function sandboxedDocument(html: string): string {
  const policy =
    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:">';
  const base = '<base target="_blank">';
  const headMatch = /<head[^>]*>/i.exec(html);
  if (headMatch) {
    const at = headMatch.index + headMatch[0].length;
    return html.slice(0, at) + policy + base + html.slice(at);
  }
  return policy + base + html;
}

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
  const [preview, setPreview] = useState<string | null>(null);

  const loadPreview = async () => {
    if (preview !== null) {
      setPreview(null);
      return;
    }
    try {
      const response = await fetch(snapshotUrl(id), { credentials: "same-origin" });
      if (!response.ok) throw new Error(String(response.status));
      setPreview(sandboxedDocument(await response.text()));
    } catch {
      setError("Snapshot konnte nicht geladen werden.");
    }
  };

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

  const imageState = item ? previewState(item, { run: runs.find((run) => run.processor === "thumbnail") }) : "none";

  useEffect(() => {
    if (imageState !== "pending") return;
    let current = true;
    let polls = 0;
    const timer = setInterval(() => {
      polls += 1;
      if (polls >= PREVIEW_POLL_LIMIT) clearInterval(timer);
      // Only the preview's inputs change: the edit form keeps whatever is being typed.
      Promise.all([api.item(id), api.processing(id)])
        .then(([detail, processing]) => {
          if (!current) return;
          setRuns(processing);
          setItem((old) =>
            old && { ...old, thumbnail_available: detail.thumbnail_available, processing_status: detail.processing_status },
          );
        })
        .catch(() => undefined);
    }, PREVIEW_POLL_MS);
    return () => {
      current = false;
      clearInterval(timer);
    };
  }, [imageState, id]);

  if (error) return <Notice tone="error">{error}</Notice>;
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
      {imageState !== "none" && (
        <div class="detail-preview">
          <Thumbnail key={item.id} id={item.id} alt={thumbnailAlt(item)} state={imageState} variant="detail" />
        </div>
      )}
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
          {item.kind === "url" && item.mime_type && (
            <a class="button" href={snapshotUrl(id)} download>
              Snapshot herunterladen
            </a>
          )}
          {item.kind === "url" && item.mime_type === "text/html" && (
            <button type="button" onClick={() => void loadPreview()}>
              {preview === null ? "Vorschau anzeigen" : "Vorschau schließen"}
            </button>
          )}
          <button type="button" class="danger" onClick={() => void trash()}>
            In den Papierkorb
          </button>
        </div>
      </form>
      {preview !== null && (
        <section class="card">
          <h2>Snapshot (ohne Skripte, ohne Netzwerk)</h2>
          {/* No allow-* flags: scripts, forms, same-origin access and navigation are all off. */}
          <iframe class="preview" sandbox="" srcdoc={preview} title="Gespeicherte Seite" />
        </section>
      )}
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
        <table class="stack" role="table">
          <tbody role="rowgroup">
            {runs.map((run) => (
              <tr key={run.processor} role="row">
                <td role="cell">{run.processor}</td>
                <td class={`status-${run.status}`} role="cell">
                  {run.status}
                </td>
                <td role="cell">{run.attempts}×</td>
                <td class="muted" role="cell">
                  {run.last_error ?? ""}
                </td>
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
