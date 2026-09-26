import { useEffect, useState } from "preact/hooks";

import { ApiError, api, type Session } from "./api";
import { Feed } from "./feed";
import { ItemView } from "./item";
import { Login } from "./login";
import { Tokens } from "./tokens";

/** Hash routes keep the static build free of server-side rewrites. */
export type Route =
  | { name: "feed"; query: string; trashed: boolean }
  | { name: "item"; id: string }
  | { name: "tokens" };

export function parseRoute(hash: string): Route {
  const [path = "", search = ""] = hash.replace(/^#/, "").split("?");
  const params = new URLSearchParams(search);
  const itemMatch = /^\/items\/([0-9a-f-]{36})$/.exec(path);
  if (itemMatch?.[1] !== undefined) return { name: "item", id: itemMatch[1] };
  if (path === "/tokens") return { name: "tokens" };
  return { name: "feed", query: params.get("q") ?? "", trashed: params.get("trashed") === "1" };
}

/** Text a share target handed over via /share?title=&text=&url=, or null. */
export function sharedText(pathname: string, search: string): string | null {
  if (pathname !== "/share") return null;
  const params = new URLSearchParams(search);
  const url = params.get("url")?.trim() ?? "";
  const text = params.get("text")?.trim() ?? "";
  const title = params.get("title")?.trim() ?? "";
  // Android often puts the URL into "text"; a bare URL is captured as a link.
  const link = url || (/^https?:\/\/\S+$/i.test(text) ? text : "");
  const joined = (link ? [link] : [title, text]).filter(Boolean).join("\n");
  return joined || null;
}

export function href(route: Route): string {
  if (route.name === "item") return `#/items/${route.id}`;
  if (route.name === "tokens") return "#/tokens";
  const params = new URLSearchParams();
  if (route.query) params.set("q", route.query);
  if (route.trashed) params.set("trashed", "1");
  const query = params.toString();
  return query ? `#/?${query}` : "#/";
}

export function App() {
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [route, setRoute] = useState<Route>(() => parseRoute(location.hash));
  const [shared] = useState<string | null>(() => {
    const text = sharedText(location.pathname, location.search);
    if (text !== null) history.replaceState(null, "", "/#/");
    return text;
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
          <a href={href({ name: "feed", query: "", trashed: false })}>Feed</a>
          <a href={href({ name: "feed", query: "", trashed: true })}>Papierkorb</a>
          <a href={href({ name: "tokens" })}>Token</a>
          <button type="button" class="link" onClick={signOut}>
            Abmelden ({session.username ?? session.display_name})
          </button>
        </nav>
      </header>
      <main>
        {route.name === "item" && <ItemView id={route.id} />}
        {route.name === "tokens" && <Tokens />}
        {route.name === "feed" && <Feed route={route} prefill={shared} />}
      </main>
    </>
  );
}
