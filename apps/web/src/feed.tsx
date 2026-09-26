import { useEffect, useRef, useState } from "preact/hooks";

import { ApiError, api, looksLikeUrl, type ItemSummary, type SearchResult } from "./api";
import { href, type Route } from "./app";

type Row = ItemSummary & { snippet?: string };

const KIND_ICON: Record<string, string> = {
  text: "📝",
  url: "🔗",
  image: "🖼️",
  document: "📄",
  file: "📦",
};

export function describe(item: ItemSummary): string {
  return item.title ?? item.original_filename ?? item.source_url ?? item.user_note ?? item.kind;
}

export function Feed({ route }: { route: Route & { name: "feed" } }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [next, setNext] = useState<string | number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState(route.query);
  const [loading, setLoading] = useState(false);

  const load = async (append: boolean) => {
    setLoading(true);
    setError(null);
    try {
      const base: Record<string, string> = { limit: "20" };
      if (route.trashed) base["trashed"] = "true";
      if (route.query) {
        if (append && typeof next === "number") base["offset"] = String(next);
        const page = await api.search({ ...base, q: route.query });
        setRows((old) => (append ? [...old, ...page.results] : page.results));
        setNext(page.next_offset);
      } else {
        if (append && typeof next === "string") base["cursor"] = next;
        const page = await api.items(base);
        setRows((old) => (append ? [...old, ...page.items] : page.items));
        setNext(page.next_cursor);
      }
    } catch (failure: unknown) {
      setError(failure instanceof ApiError ? failure.message : "Laden fehlgeschlagen.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setQuery(route.query);
    void load(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [route.query, route.trashed]);

  const submitSearch = (event: Event) => {
    event.preventDefault();
    location.hash = href({ name: "feed", query: query.trim(), trashed: route.trashed });
  };

  return (
    <>
      {!route.trashed && <Capture onSaved={() => void load(false)} />}
      <form class="search" onSubmit={submitSearch} role="search">
        <input
          type="search"
          placeholder={route.trashed ? "Im Papierkorb suchen…" : "Suchen… (Tasche OR \"local RAG\")"}
          value={query}
          onInput={(e) => setQuery((e.target as HTMLInputElement).value)}
        />
        <button type="submit">Suchen</button>
      </form>
      {error && <p class="error">{error}</p>}
      {rows.length === 0 && !loading && (
        <p class="hint">{route.trashed ? "Der Papierkorb ist leer." : "Noch nichts in der Tasche."}</p>
      )}
      <ul class="items">
        {rows.map((row) => (
          <li key={row.id} class="item-row">
            {route.trashed && (
              <button
                type="button"
                class="restore"
                onClick={() => void api.restore(row.id).then(() => load(false))}
              >
                Wiederherstellen
              </button>
            )}
            <a href={route.trashed ? undefined : href({ name: "item", id: row.id })} class="item">
              <span class="icon" aria-hidden="true">
                {KIND_ICON[row.kind] ?? "❔"}
              </span>
              <span class="body">
                <span class="title">{describe(row)}</span>
                {row.snippet !== undefined && <span class="snippet">{row.snippet}</span>}
                {row.snippet === undefined && row.user_note && row.title && (
                  <span class="snippet">{row.user_note}</span>
                )}
                <span class="meta">
                  {new Date(row.captured_at).toLocaleString()} · {row.kind}
                  {row.processing_status !== "ready" && <> · {row.processing_status}</>}
                  {row.tags.map((tag) => (
                    <span class="tag" key={tag}>
                      {tag}
                    </span>
                  ))}
                </span>
              </span>
            </a>
          </li>
        ))}
      </ul>
      {next !== null && (
        <button type="button" onClick={() => void load(true)} disabled={loading}>
          Mehr laden
        </button>
      )}
    </>
  );
}

function Capture({ onSaved }: { onSaved: () => void }) {
  const [text, setText] = useState("");
  const [note, setNote] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const finish = (duplicate: string | null) => {
    setStatus(duplicate ? "✓ Gespeichert – war schon in der Tasche" : "✓ Gespeichert");
    setText("");
    setNote("");
    onSaved();
  };

  const fail = (failure: unknown) => {
    setStatus(failure instanceof ApiError ? `Fehler: ${failure.message}` : "Speichern fehlgeschlagen.");
  };

  const save = async (event: Event) => {
    event.preventDefault();
    const content = text.trim();
    if (!content) return;
    setBusy(true);
    try {
      const userNote = note.trim() || null;
      const result = looksLikeUrl(content)
        ? await api.captureUrl(content, userNote)
        : await api.captureText(text, userNote);
      finish(result.duplicate_of);
    } catch (failure: unknown) {
      fail(failure);
    } finally {
      setBusy(false);
    }
  };

  const upload = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setBusy(true);
    try {
      for (const file of Array.from(files)) {
        await api.captureFile(file, note.trim() || null);
      }
      finish(null);
    } catch (failure: unknown) {
      fail(failure);
    } finally {
      setBusy(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  return (
    <form
      class="card capture"
      onSubmit={save}
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        void upload(e.dataTransfer?.files ?? null);
      }}
    >
      <textarea
        placeholder="Text, Link oder Gedanken hier ablegen… (Dateien per Drag & Drop)"
        value={text}
        rows={3}
        onInput={(e) => setText((e.target as HTMLTextAreaElement).value)}
      />
      <div class="row">
        <input
          placeholder="Ein Gedanke dazu (optional)"
          value={note}
          onInput={(e) => setNote((e.target as HTMLInputElement).value)}
        />
        <input
          ref={fileInput}
          type="file"
          multiple
          onChange={(e) => void upload((e.target as HTMLInputElement).files)}
          aria-label="Dateien hochladen"
        />
        <button type="submit" disabled={busy || !text.trim()}>
          In die Tasche
        </button>
      </div>
      {status && <p class="status" role="status">{status}</p>}
    </form>
  );
}
