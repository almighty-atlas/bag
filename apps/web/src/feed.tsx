import { useEffect, useRef, useState } from "preact/hooks";

import { ApiError, api, looksLikeUrl, organize, type ItemSummary, type NamedEntry } from "./api";
import { EMPTY_FILTERS, href, type Filters, type Route } from "./app";
import { CaptureKeys, uploadFiles, type PendingFile } from "./retry";
import type { ShareDraft } from "./share";

type Row = ItemSummary & { snippet?: string };

const KIND_ICON: Record<string, string> = {
  text: "📝",
  url: "🔗",
  image: "🖼️",
  document: "📄",
  file: "📦",
};

/** Query parameters for the API: dates become timezone-aware day boundaries. */
export function filterParams(filters: Filters): Record<string, string> {
  const params: Record<string, string> = {};
  if (filters.kind) params["kind"] = filters.kind;
  if (filters.tag) params["tag"] = filters.tag;
  if (filters.collection) params["collection"] = filters.collection;
  if (filters.from) params["from"] = new Date(`${filters.from}T00:00:00`).toISOString();
  if (filters.to) {
    const end = new Date(`${filters.to}T00:00:00`);
    end.setDate(end.getDate() + 1);
    params["to"] = end.toISOString();
  }
  return params;
}

export function describe(item: ItemSummary): string {
  return item.title ?? item.original_filename ?? item.source_url ?? item.user_note ?? item.kind;
}

/** Files a POST share target stashed in the cache; uploaded once, then forgotten. */
export async function uploadSharedFiles(note: string | null, keys: CaptureKeys): Promise<number> {
  if (!("caches" in globalThis)) return 0;
  const cache = await caches.open("bag-share");
  const countResponse = await cache.match("/shared/count");
  const count = Number.parseInt((await countResponse?.text()) ?? "0", 10) || 0;
  let uploaded = 0;
  for (let index = 0; index < count; index += 1) {
    const stored = await cache.match(`/shared/${index}`);
    if (!stored) continue;
    const name = decodeURIComponent(stored.headers.get("X-Bag-Filename") ?? "shared");
    const file = new File([await stored.blob()], name, {
      type: stored.headers.get("Content-Type") ?? "application/octet-stream",
    });
    // Cached files are new objects on every read, so the slot and file details identify the payload.
    const payload = ["shared", index, name, file.size, file.type, note];
    await api.captureFile(file, note, keys.key(...payload));
    await cache.delete(`/shared/${index}`);
    keys.confirm(...payload);
    uploaded += 1;
  }
  await cache.delete("/shared/count");
  return uploaded;
}

export function Feed({
  route,
  prefill,
  sharedFiles,
}: {
  route: Route & { name: "feed" };
  prefill: ShareDraft | null;
  sharedFiles: number;
}) {
  const [rows, setRows] = useState<Row[]>([]);
  const [next, setNext] = useState<string | number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState(route.query);
  const [loading, setLoading] = useState(false);
  const [names, setNames] = useState<{ tags: NamedEntry[]; collections: NamedEntry[] }>({ tags: [], collections: [] });
  const searchInput = useRef<HTMLInputElement>(null);

  const load = async (append: boolean) => {
    setLoading(true);
    setError(null);
    try {
      const base: Record<string, string> = { limit: "20", ...filterParams(route.filters) };
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
  }, [route.query, route.trashed, href(route)]);

  useEffect(() => {
    Promise.all([organize.list("tag"), organize.list("collection")])
      .then(([tags, collections]) => setNames({ tags, collections }))
      .catch(() => setNames({ tags: [], collections: [] }));
    // "/" focuses the search box unless the user is already typing somewhere.
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (event.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(target?.tagName ?? "")) {
        event.preventDefault();
        searchInput.current?.focus();
      }
    };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, []);

  const go = (changes: Partial<Route & { name: "feed" }>) => {
    location.hash = href({ ...route, ...changes });
  };

  const submitSearch = (event: Event) => {
    event.preventDefault();
    go({ query: query.trim() });
  };

  const setFilter = (key: keyof Filters, value: string) => go({ filters: { ...route.filters, [key]: value } });
  const active = Object.values(route.filters).some(Boolean);

  return (
    <>
      {!route.trashed && (
        <Capture onSaved={() => void load(false)} initial={prefill} sharedFiles={sharedFiles} />
      )}
      <form class="search" onSubmit={submitSearch} role="search">
        <input
          ref={searchInput}
          type="search"
          placeholder={route.trashed ? "Im Papierkorb suchen…" : "Suchen… (Tasche OR \"local RAG\")"}
          value={query}
          onInput={(e) => setQuery((e.target as HTMLInputElement).value)}
        />
        <button type="submit">Suchen</button>
      </form>
      <div class="filters">
        <select value={route.filters.kind} onChange={(e) => setFilter("kind", (e.target as HTMLSelectElement).value)}>
          <option value="">alle Arten</option>
          {["text", "url", "image", "document", "file"].map((kind) => (
            <option value={kind} key={kind}>
              {KIND_ICON[kind]} {kind}
            </option>
          ))}
        </select>
        <select value={route.filters.tag} onChange={(e) => setFilter("tag", (e.target as HTMLSelectElement).value)}>
          <option value="">alle Tags</option>
          {names.tags.map((tag) => (
            <option value={tag.name} key={tag.id}>
              {tag.name} ({tag.item_count})
            </option>
          ))}
        </select>
        <select
          value={route.filters.collection}
          onChange={(e) => setFilter("collection", (e.target as HTMLSelectElement).value)}
        >
          <option value="">alle Sammlungen</option>
          {names.collections.map((collection) => (
            <option value={collection.name} key={collection.id}>
              {collection.name} ({collection.item_count})
            </option>
          ))}
        </select>
        <input type="date" value={route.filters.from} onChange={(e) => setFilter("from", (e.target as HTMLInputElement).value)} aria-label="ab" />
        <input type="date" value={route.filters.to} onChange={(e) => setFilter("to", (e.target as HTMLInputElement).value)} aria-label="bis" />
        {active && (
          <button type="button" class="link" onClick={() => go({ filters: EMPTY_FILTERS })}>
            Filter löschen
          </button>
        )}
      </div>
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

function Capture({
  onSaved,
  initial,
  sharedFiles,
}: {
  onSaved: () => void;
  initial: ShareDraft | null;
  sharedFiles: number;
}) {
  const [text, setText] = useState(initial?.content ?? "");
  const [pendingShare, setPendingShare] = useState(sharedFiles);
  const [note, setNote] = useState(initial?.note ?? "");
  // Shared titles and texts may span lines, which a single-line input would drop.
  const [multilineNote] = useState(() => Boolean(initial?.note.includes("\n")));
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pendingFiles, setPendingFiles] = useState<PendingFile[]>([]);
  const [keys] = useState(() => new CaptureKeys());
  const fileInput = useRef<HTMLInputElement>(null);

  const finish = (duplicate: string | null) => {
    setStatus(duplicate ? "✓ Gespeichert – war schon in der Tasche" : "✓ Gespeichert");
    setText("");
    setNote("");
    onSaved();
  };

  const fail = (failure: unknown, saved = 0) => {
    const reason = failure instanceof ApiError ? `Fehler: ${failure.message}` : "Speichern nicht bestätigt.";
    setStatus(
      `${saved ? `${saved} Datei(en) gespeichert. ` : ""}${reason} Unverändert erneut speichern legt kein Duplikat an; geänderte Eingaben werden als neue Erfassung gespeichert.`,
    );
  };

  const save = async (event: Event) => {
    event.preventDefault();
    const content = text.trim();
    if (!content && !pendingShare) return;
    setBusy(true);
    try {
      const userNote = note.trim() || null;
      let duplicate: string | null = null;
      const isUrl = looksLikeUrl(content);
      const payload = isUrl ? ["url", content, userNote] : ["text", text, userNote];
      if (content) {
        const key = keys.key(...payload);
        const result = isUrl ? await api.captureUrl(content, userNote, key) : await api.captureText(text, userNote, key);
        duplicate = result.duplicate_of;
      }
      if (pendingShare) {
        await uploadSharedFiles(userNote, keys);
        setPendingShare(0);
        history.replaceState(null, "", "/#/");
      }
      // Confirmed only after the whole action: retrying after a failed shared upload replays the text.
      if (content) keys.confirm(...payload);
      finish(duplicate);
    } catch (failure: unknown) {
      fail(failure);
    } finally {
      setBusy(false);
    }
  };

  const sendFiles = async (files: PendingFile[]) => {
    setBusy(true);
    try {
      const batch = await uploadFiles(files, keys, (file, fileNote, key) => api.captureFile(file, fileNote, key));
      // Replace the attempted entries by those still unconfirmed; other pending files stay listed.
      setPendingFiles((current) => [...current.filter((entry) => !files.includes(entry)), ...batch.pending]);
      if (batch.pending.length === 0) finish(null);
      else {
        if (batch.saved) onSaved();
        fail(batch.error, batch.saved);
      }
    } finally {
      setBusy(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  const upload = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    const fileNote = note.trim() || null;
    await sendFiles(Array.from(files, (file) => ({ file, note: fileNote })));
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
        {multilineNote ? (
          <textarea
            placeholder="Ein Gedanke dazu (optional)"
            value={note}
            rows={3}
            onInput={(e) => setNote((e.target as HTMLTextAreaElement).value)}
          />
        ) : (
          <input
            placeholder="Ein Gedanke dazu (optional)"
            value={note}
            onInput={(e) => setNote((e.target as HTMLInputElement).value)}
          />
        )}
        <input
          ref={fileInput}
          type="file"
          multiple
          onChange={(e) => void upload((e.target as HTMLInputElement).files)}
          aria-label="Dateien hochladen"
        />
        <button type="submit" disabled={busy || (!text.trim() && !pendingShare)}>
          In die Tasche
        </button>
      </div>
      {pendingShare > 0 && (
        <p class="hint">
          {pendingShare} geteilte Datei(en) warten. Optional einen Gedanken ergänzen, dann „In die Tasche“.
        </p>
      )}
      {pendingFiles.length > 0 && (
        <p class="hint">
          {pendingFiles.length} Datei(en) nicht bestätigt: {pendingFiles.map(({ file }) => file.name).join(", ")}.{" "}
          <button type="button" disabled={busy} onClick={() => void sendFiles(pendingFiles)}>
            Erneut versuchen
          </button>{" "}
          <button type="button" class="link" disabled={busy} onClick={() => setPendingFiles([])}>
            Verwerfen
          </button>
        </p>
      )}
      {status && <p class="status" role="status">{status}</p>}
    </form>
  );
}
