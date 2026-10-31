import { useEffect, useState } from "preact/hooks";

import { ApiError, api, type Session } from "./api";
import { Feed } from "./feed";
import { ItemView } from "./item";
import { Login } from "./login";
import { Organize } from "./organize";
import { SHARE_FIELDS, shareDraft, sharePayload, type SharePayload } from "./share";
import { batchDraft, loadBatch, type ShareBatch } from "./share-store";
import { Tokens } from "./tokens";

export interface Filters {
  kind: string;
  tag: string;
  collection: string;
  from: string; // YYYY-MM-DD or empty
  to: string;
}

export const EMPTY_FILTERS: Filters = { kind: "", tag: "", collection: "", from: "", to: "" };

/** Hash routes keep the static build free of server-side rewrites. */
export type Route =
  | { name: "feed"; query: string; trashed: boolean; filters: Filters }
  | { name: "item"; id: string }
  | { name: "organize" }
  | { name: "tokens" }
  // A stored pending share (batch), or the legacy GET form that carries its fields in the URL.
  | { name: "share"; batch: string | null; payload: SharePayload };

export type FeedRoute = Extract<Route, { name: "feed" }>;

export const FEED: FeedRoute = { name: "feed", query: "", trashed: false, filters: EMPTY_FILTERS };

const DATE = /^\d{4}-\d{2}-\d{2}$/;
const BATCH_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function parseRoute(hash: string): Route {
  const [path = "", search = ""] = hash.replace(/^#/, "").split("?");
  const params = new URLSearchParams(search);
  const itemMatch = /^\/items\/([0-9a-f-]{36})$/.exec(path);
  if (itemMatch?.[1] !== undefined) return { name: "item", id: itemMatch[1] };
  if (path === "/tokens") return { name: "tokens" };
  if (path === "/organize") return { name: "organize" };
  if (path === "/share") {
    const batch = params.get("batch") ?? "";
    return { name: "share", batch: BATCH_ID.test(batch) ? batch : null, payload: sharePayload(params) };
  }
  const date = (key: string) => {
    const value = params.get(key) ?? "";
    return DATE.test(value) ? value : "";
  };
  return {
    name: "feed",
    query: params.get("q") ?? "",
    trashed: params.get("trashed") === "1",
    filters: {
      kind: params.get("kind") ?? "",
      tag: params.get("tag") ?? "",
      collection: params.get("collection") ?? "",
      from: date("from"),
      to: date("to"),
    },
  };
}

/** Hash route for a GET share (/share?title=&text=&url=), so it follows the POST path; else null. */
export function sharedRoute(pathname: string, search: string): string | null {
  if (pathname !== "/share") return null;
  return href({ name: "share", batch: null, payload: sharePayload(new URLSearchParams(search)) });
}

export function href(route: Route): string {
  if (route.name === "item") return `#/items/${route.id}`;
  if (route.name === "tokens") return "#/tokens";
  if (route.name === "organize") return "#/organize";
  if (route.name === "share") {
    const params = new URLSearchParams();
    if (route.batch) params.set("batch", route.batch);
    else {
      for (const name of SHARE_FIELDS) {
        for (const value of route.payload[name]) params.append(name, value);
      }
    }
    return `#/share?${params}`;
  }
  const params = new URLSearchParams();
  if (route.query) params.set("q", route.query);
  if (route.trashed) params.set("trashed", "1");
  for (const [key, value] of Object.entries(route.filters)) {
    if (value) params.set(key, value);
  }
  const query = params.toString();
  return query ? `#/?${query}` : "#/";
}

export function App() {
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [route, setRoute] = useState<Route>(() => {
    const shared = sharedRoute(location.pathname, location.search);
    if (shared !== null) history.replaceState(null, "", `/${shared}`);
    return parseRoute(location.hash);
  });

  useEffect(() => {
    const onHash = () => setRoute(parseRoute(location.hash));
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    api
      .session()
      .then(setSession)
      .catch((error: unknown) => {
        if (error instanceof ApiError && error.status === 401) setSession(null);
        else setSession(null);
      });
  }, []);

  if (session === undefined) return <main class="centered">Lade…</main>;
  if (session === null) return <Login onLogin={setSession} />;

  const signOut = () =>
    api.logout().finally(() => {
      setSession(null);
    });

  return (
    <>
      <header class="topbar">
        <a href="#/" class="brand">
          <span aria-hidden="true">👝</span> Bag of Holding
        </a>
        <nav>
          <a href={href(FEED)}>Feed</a>
          <a href={href({ name: "organize" })}>Tags</a>
          <a href={href({ ...FEED, trashed: true })}>Papierkorb</a>
          <a href={href({ name: "tokens" })}>Token</a>
          <button type="button" class="link" onClick={signOut}>
            Abmelden ({session.username ?? session.display_name})
          </button>
        </nav>
      </header>
      <main>
        {route.name === "item" && <ItemView id={route.id} />}
        {route.name === "tokens" && <Tokens />}
        {route.name === "organize" && <Organize />}
        {route.name === "feed" && <Feed route={route} share={null} />}
        {route.name === "share" && <SharePage batch={route.batch} payload={route.payload} />}
      </main>
    </>
  );
}

/** Opens the capture form on a pending share; an unknown or already finished batch says so. */
function SharePage({ batch, payload }: { batch: string | null; payload: SharePayload }) {
  const [loaded, setLoaded] = useState<ShareBatch | null | undefined>(batch ? undefined : null);
  useEffect(() => {
    if (!batch) return;
    setLoaded(undefined);
    loadBatch(batch)
      .then(setLoaded)
      .catch(() => setLoaded(null));
  }, [batch]);
  if (loaded === undefined) return <p>Lade…</p>;
  if (batch === null) return <Feed route={FEED} share={{ batch: null, draft: shareDraft(payload) }} />;
  if (loaded === null) {
    return (
      <>
        <p class="hint">Dieser geteilte Inhalt ist nicht mehr offen: gespeichert oder verworfen.</p>
        <Feed route={FEED} share={null} />
      </>
    );
  }
  return <Feed key={loaded.id} route={FEED} share={{ batch: loaded, draft: batchDraft(loaded) }} />;
}
