import swSource from "../public/sw.js?raw";

export const ORIGIN = "https://bag.test";
const absolute = (key: string | Request) => new URL(typeof key === "string" ? key : key.url, ORIGIN).href;

interface Stored {
  status: number;
  headers: [string, string][];
  body: ArrayBuffer;
}

/** In-memory CacheStorage: put consumes the body like the real one, match returns a fresh Response. */
export class FakeCaches {
  readonly stores = new Map<string, Map<string, Stored>>();
  /** Hooks to make writes fail or interleave in tests. */
  failPut: ((name: string, key: string) => boolean) | null = null;

  async open(name: string) {
    const store = this.stores.get(name) ?? new Map<string, Stored>();
    this.stores.set(name, store);
    return {
      put: async (key: string | Request, response: Response) => {
        const url = absolute(key);
        const body = await response.arrayBuffer();
        // Yield so concurrent writers interleave like separate service worker events.
        await new Promise((resolve) => setTimeout(resolve, 0));
        if (this.failPut?.(name, new URL(url).pathname)) throw new Error("QuotaExceededError");
        store.set(url, { status: response.status, headers: [...response.headers], body });
      },
      match: async (key: string | Request) => {
        const hit = store.get(absolute(key));
        return hit && new Response(hit.body.slice(0), { status: hit.status, headers: hit.headers });
      },
      keys: async () => [...store.keys()].map((url) => new Request(url)),
      delete: async (key: string | Request) => store.delete(absolute(key)),
      addAll: async (keys: string[]) => {
        for (const key of keys) store.set(absolute(key), { status: 200, headers: [], body: new ArrayBuffer(0) });
      },
    };
  }

  async keys(): Promise<string[]> {
    return [...this.stores.keys()];
  }

  async delete(name: string): Promise<boolean> {
    return this.stores.delete(name);
  }

  paths(name: string): string[] {
    return [...(this.stores.get(name)?.keys() ?? [])].map((url) => new URL(url).pathname).sort();
  }
}

class TestResponse extends Response {
  // Browsers resolve a relative redirect against the worker's origin; Node needs it absolute.
  static override redirect(url: string | URL, status?: number): Response {
    return Response.redirect(new URL(url, ORIGIN).href, status);
  }
}

type Listener = (event: unknown) => void;

/** Runs the real public/sw.js against a CacheStorage; each call is a fresh worker instance. */
export function startWorker(caches: FakeCaches) {
  const listeners: Record<string, Listener> = {};
  const self = {
    addEventListener: (name: string, listener: Listener) => (listeners[name] = listener),
    skipWaiting: () => undefined,
    clients: { claim: () => undefined },
  };
  new Function("self", "caches", "location", "Response", swSource)(self, caches, new URL(ORIGIN), TestResponse);

  const run = async (name: string): Promise<void> => {
    let waited: Promise<unknown> = Promise.resolve();
    listeners[name]?.({ waitUntil: (promise: Promise<unknown>) => (waited = promise) });
    await waited;
  };

  return {
    install: () => run("install"),
    activate: () => run("activate"),
    /** A multipart POST /share; resolves to the redirect, rejects if the worker's response fails. */
    async post(fields: [string, string | File][]) {
      const form = new FormData();
      for (const [name, value] of fields) form.append(name, value);
      let responded: Promise<Response> | undefined;
      listeners["fetch"]?.({
        request: new Request(`${ORIGIN}/share`, { method: "POST", body: form }),
        respondWith: (value: Promise<Response>) => (responded = value),
      });
      if (responded === undefined) throw new Error("service worker did not handle POST /share");
      const response = await responded;
      const location = new URL(response.headers.get("Location") ?? "");
      return { status: response.status, path: location.pathname, hash: location.hash };
    },
  };
}

export type Outcome = "ok" | "lost" | "offline";

/** Mirrors the server contract: a known key replays the first item, whatever the payload. */
export class FakeServer {
  readonly items: unknown[] = [];
  readonly byKey = new Map<string, { payload: unknown; id: number }>();
  readonly requests: string[] = [];
  private readonly outcomes: Outcome[] = [];

  next(...outcomes: Outcome[]): void {
    this.outcomes.push(...outcomes);
  }

  async capture(payload: unknown, key: string): Promise<{ id: number; duplicate_of: null }> {
    this.requests.push(key);
    const outcome = this.outcomes.shift() ?? "ok";
    if (outcome === "offline") throw new TypeError("Failed to fetch");
    let stored = this.byKey.get(key);
    if (stored === undefined) {
      stored = { payload, id: this.items.push(payload) };
      this.byKey.set(key, stored);
    }
    // Committed, but the response never reaches the client.
    if (outcome === "lost") throw new TypeError("Failed to fetch");
    return { id: stored.id, duplicate_of: null };
  }
}
