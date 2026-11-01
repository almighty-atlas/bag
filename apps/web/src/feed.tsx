import { useEffect, useRef, useState } from "preact/hooks";

import { ApiError, api, looksLikeUrl, organize, type MatchedField, type NamedEntry } from "./api";
import { EMPTY_FILTERS, href, type Filters, type Route } from "./app";
import { feedText, type FeedItem } from "./feed-text";
import { matchReasons } from "./match-reasons";
import { Notice, type NoticeState } from "./notice";
import { PendingShares, DISCARD_CONFIRMATION } from "./pending";
import { CaptureKeys, uploadFiles, type PendingFile } from "./retry";
import type { ShareDraft } from "./share";
import { BatchGoneError, MissingFilesError, discardBatch, remainingFiles, saveBatch, type ShareBatch } from "./share-store";
import { Thumbnail, previewState, thumbnailAlt } from "./thumbnail";

type Row = FeedItem & { matched_fields?: MatchedField[] };

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

/**
 * A server failure or timeout can happen after the item was stored, so the save is unconfirmed
 * and a retry (same capture ID) is the answer. Other statuses are clear refusals.
 */
export function outcomeUnknown(status: number): boolean {
  return status >= 500 || status === 408;
}

/** Same breakpoint as the stylesheet's wide layout. */
function wideScreen(): boolean {
  return typeof matchMedia === "function" && matchMedia("(min-width: 720px)").matches;
}

/** What the capture form starts with: a pending share (stored batch) or only prefilled fields. */
export interface ShareInput {
  batch: ShareBatch | null;
  draft: ShareDraft;
}

export function Feed({ route, share }: { route: Route & { name: "feed" }; share: ShareInput | null }) {
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
  const activeCount = Object.values(route.filters).filter(Boolean).length;
  const [filtersOpen, setFiltersOpen] = useState(() => activeCount > 0 || wideScreen());
  // A filter set from elsewhere (tag link, other route) opens the section; collapsing it by hand
  // stays possible until the filters change again.
  const filtersKey = JSON.stringify(route.filters);
  useEffect(() => {
    if (activeCount > 0) setFiltersOpen(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filtersKey]);

  return (
    <>
      {!route.trashed && <PendingShares exclude={share?.batch?.id ?? null} />}
      {!route.trashed && <Capture onSaved={() => void load(false)} share={share} />}
      <form class="search" onSubmit={submitSearch} role="search">
        <input
          ref={searchInput}
          type="search"
          aria-label="Suchen"
          placeholder={route.trashed ? "Im Papierkorb suchen…" : "Suchen…"}
          value={query}
          onInput={(e) => setQuery((e.target as HTMLInputElement).value)}
        />
        <button type="submit">Suchen</button>
      </form>
      {/* Open on wide screens and whenever a filter is set; a phone keeps capture and search above the fold. */}
      <details class="filters" open={filtersOpen} onToggle={(e) => setFiltersOpen((e.currentTarget as HTMLDetailsElement).open)}>
        <summary>
          Filter{activeCount > 0 && <span class="badge">{activeCount} aktiv</span>}
        </summary>
        <div class="filter-grid">
          <label>
            Art
            <select value={route.filters.kind} onChange={(e) => setFilter("kind", (e.target as HTMLSelectElement).value)}>
              <option value="">alle Arten</option>
              {["text", "url", "image", "document", "file"].map((kind) => (
                <option value={kind} key={kind}>
                  {KIND_ICON[kind]} {kind}
                </option>
              ))}
            </select>
          </label>
          <label>
            Tag
            <select value={route.filters.tag} onChange={(e) => setFilter("tag", (e.target as HTMLSelectElement).value)}>
              <option value="">alle Tags</option>
              {names.tags.map((tag) => (
                <option value={tag.name} key={tag.id}>
                  {tag.name} ({tag.item_count})
                </option>
              ))}
            </select>
          </label>
          <label>
            Sammlung
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
          </label>
          <label>
            Erfasst von
            <input type="date" value={route.filters.from} onChange={(e) => setFilter("from", (e.target as HTMLInputElement).value)} />
          </label>
          <label>
            Erfasst bis
            <input type="date" value={route.filters.to} onChange={(e) => setFilter("to", (e.target as HTMLInputElement).value)} />
          </label>
          {activeCount > 0 && (
            <button type="button" class="link clear" onClick={() => go({ filters: EMPTY_FILTERS })}>
              Filter löschen
            </button>
          )}
          <p class="hint search-tip">Suchtipp: <code>Tasche OR "local RAG"</code> findet beide Begriffe; Anführungszeichen suchen eine Wortfolge.</p>
        </div>
      </details>
      {error && <Notice tone="error">{error}</Notice>}
      {rows.length === 0 && !loading && (
        <p class="hint">{route.trashed ? "Der Papierkorb ist leer." : "Noch nichts in der Tasche."}</p>
      )}
      <ul class="items">
        {rows.map((row) => {
          const text = feedText(row);
          const reasons = matchReasons(row.matched_fields);
          const preview = previewState(row, { trashed: route.trashed || row.deleted_at !== null });
          const withThumb = preview !== "none" && preview !== "trashed";
          return (
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
              <a
                href={route.trashed ? undefined : href({ name: "item", id: row.id })}
                class={withThumb ? "item with-thumb" : "item"}
              >
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
          );
        })}
      </ul>
      {next !== null && (
        <button type="button" onClick={() => void load(true)} disabled={loading}>
          Mehr laden
        </button>
      )}
    </>
  );
}

function Capture({ onSaved, share }: { onSaved: () => void; share: ShareInput | null }) {
  const batch = share?.batch ?? null;
  const [text, setText] = useState(share?.draft.content ?? "");
  const [pendingShare, setPendingShare] = useState(batch?.files.length ?? 0);
  const [note, setNote] = useState(share?.draft.note ?? "");
  // Shared titles and texts may span lines, which a single-line input would drop.
  const [multilineNote] = useState(() => Boolean(share?.draft.note.includes("\n")));
  const [status, setStatus] = useState<NoticeState | null>(null);
  const [busy, setBusy] = useState(false);
  const [pendingFiles, setPendingFiles] = useState<PendingFile[]>([]);
  const [keys] = useState(() => new CaptureKeys());
  const fileInput = useRef<HTMLInputElement>(null);

  const finish = (duplicate: string | null) => {
    setStatus({ tone: "success", text: duplicate ? "Gespeichert – war schon in der Tasche" : "Gespeichert" });
    setText("");
    setNote("");
    onSaved();
  };

  // Only a server-confirmed save is a success; an unanswered request is a warning, a refusal an error.
  const fail = (failure: unknown, saved = 0) => {
    if (failure instanceof BatchGoneError) {
      setStatus({
        tone: "warning",
        text: "Dieser geteilte Inhalt ist nicht mehr offen: bereits gespeichert oder verworfen.",
      });
      return;
    }
    if (failure instanceof MissingFilesError) {
      setStatus({
        tone: "error",
        text: `${failure.names.join(", ")}: nicht mehr auf diesem Gerät vorhanden und nie gespeichert. Bitte erneut teilen; der geteilte Inhalt bleibt bis zum Verwerfen offen.`,
      });
      return;
    }
    const refused = failure instanceof ApiError && !outcomeUnknown(failure.status);
    // The notice itself already says "Fehler" or "Warnung"; the text only names the cause.
    const reason =
      failure instanceof ApiError && refused
        ? `Nicht gespeichert: ${failure.message.replace(/[.\s]+$/, "")}.`
        : failure instanceof ApiError
          ? `Speichern nicht bestätigt (HTTP ${failure.status}).`
          : "Speichern nicht bestätigt.";
    setStatus({
      tone: refused ? "error" : "warning",
      text: `${saved ? `${saved} Datei(en) gespeichert. ` : ""}${reason} Unverändert erneut speichern legt kein Duplikat an; geänderte Eingaben werden als neue Erfassung gespeichert.`,
    });
  };

  const save = async (event: Event) => {
    event.preventDefault();
    const content = text.trim();
    if (!content && !pendingShare) return;
    setBusy(true);
    try {
      const userNote = note.trim() || null;
      if (batch) {
        // The batch keeps its content and unconfirmed files in the cache until all of it is saved.
        const result = await saveBatch(batch, text, userNote, {
          text: api.captureText,
          url: api.captureUrl,
          file: api.captureFile,
        });
        setPendingShare(0);
        history.replaceState(null, "", "/#/");
        finish(result.duplicate);
        return;
      }
      const isUrl = looksLikeUrl(content);
      const payload = isUrl ? ["url", content, userNote] : ["text", text, userNote];
      const key = keys.key(...payload);
      const result = isUrl ? await api.captureUrl(content, userNote, key) : await api.captureText(text, userNote, key);
      keys.confirm(...payload);
      finish(result.duplicate_of);
    } catch (failure: unknown) {
      if (batch) setPendingShare((await remainingFiles(batch)).length);
      fail(failure);
    } finally {
      setBusy(false);
    }
  };

  const discardShare = async () => {
    if (!batch || !confirm(DISCARD_CONFIRMATION)) return;
    await discardBatch(batch);
    location.hash = "#/";
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
        aria-label="Text, Link oder Gedanken"
        placeholder="Text, Link oder Gedanken hier ablegen… (Dateien per Drag & Drop)"
        value={text}
        rows={3}
        onInput={(e) => setText((e.target as HTMLTextAreaElement).value)}
      />
      {multilineNote ? (
        <textarea
          aria-label="Ein Gedanke dazu (optional)"
          placeholder="Ein Gedanke dazu (optional)"
          value={note}
          rows={3}
          onInput={(e) => setNote((e.target as HTMLTextAreaElement).value)}
        />
      ) : (
        <input
          aria-label="Ein Gedanke dazu (optional)"
          placeholder="Ein Gedanke dazu (optional)"
          value={note}
          onInput={(e) => setNote((e.target as HTMLInputElement).value)}
        />
      )}
      <div class="row actions">
        {/* The native control is hidden but stays focusable; the label is the visible, touch-sized button. */}
        <label class="button file-button">
          <input
            ref={fileInput}
            type="file"
            multiple
            class="sr-only"
            onChange={(e) => void upload((e.target as HTMLInputElement).files)}
          />
          <span aria-hidden="true">📎</span> Dateien hochladen
        </label>
        <button type="submit" disabled={busy || (!text.trim() && !pendingShare)}>
          In die Tasche
        </button>
      </div>
      {batch && (
        <p class="hint">
          Geteilter Inhalt, noch nicht gespeichert
          {pendingShare > 0 && <>: {pendingShare} Datei(en) warten</>}. Optional einen Gedanken ergänzen, dann „In die
          Tasche“.{" "}
          <button type="button" class="link" disabled={busy} onClick={() => void discardShare()}>
            Verwerfen
          </button>
        </p>
      )}
      {pendingFiles.length > 0 && (
        <Notice tone="warning">
          {pendingFiles.length} Datei(en) nicht bestätigt: {pendingFiles.map(({ file }) => file.name).join(", ")}.{" "}
          <span class="notice-actions">
            <button type="button" disabled={busy} onClick={() => void sendFiles(pendingFiles)}>
              Erneut versuchen
            </button>
            <button type="button" class="link" disabled={busy} onClick={() => setPendingFiles([])}>
              Verwerfen
            </button>
          </span>
        </Notice>
      )}
      {status && <Notice tone={status.tone}>{status.text}</Notice>}
    </form>
  );
}
