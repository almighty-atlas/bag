import { useEffect, useState } from "preact/hooks";

import { ApiError, api, type Session } from "./api";
import { Feed } from "./feed";
import { ItemView } from "./item";
import { Login } from "./login";

/** Hash routes keep the static build free of server-side rewrites. */
export type Route = { name: "feed"; query: string; trashed: boolean } | { name: "item"; id: string };

export function parseRoute(hash: string): Route {
  const [path = "", search = ""] = hash.replace(/^#/, "").split("?");
  const params = new URLSearchParams(search);
  const itemMatch = /^\/items\/([0-9a-f-]{36})$/.exec(path);
  if (itemMatch?.[1] !== undefined) return { name: "item", id: itemMatch[1] };
  return { name: "feed", query: params.get("q") ?? "", trashed: params.get("trashed") === "1" };
}

export function href(route: Route): string {
  if (route.name === "item") return `#/items/${route.id}`;
  const params = new URLSearchParams();
  if (route.query) params.set("q", route.query);
  if (route.trashed) params.set("trashed", "1");
  const query = params.toString();
  return query ? `#/?${query}` : "#/";
}

export function App() {
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [route, setRoute] = useState<Route>(() => parseRoute(location.hash));

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
          <a href={href({ name: "feed", query: "", trashed: false })}>Feed</a>
          <a href={href({ name: "feed", query: "", trashed: true })}>Papierkorb</a>
          <button type="button" class="link" onClick={signOut}>
            Abmelden ({session.username ?? session.display_name})
          </button>
        </nav>
      </header>
      <main>{route.name === "item" ? <ItemView id={route.id} /> : <Feed route={route} />}</main>
    </>
  );
}
