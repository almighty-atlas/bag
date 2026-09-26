/* Offline app shell. API responses are never cached: the server is the memory. */
const VERSION = "bag-shell-v1";
const SHELL = ["/", "/index.html", "/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(VERSION).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== VERSION).map((key) => caches.delete(key)))),
  );
  self.clients.claim();
});

const SHARE_CACHE = "bag-share";

/* Web Share Target (POST): stash the payload, then let the app upload it with the session. */
async function stashShare(request) {
  const form = await request.formData();
  const cache = await caches.open(SHARE_CACHE);
  const files = form.getAll("files").filter((entry) => entry instanceof File);
  await Promise.all(
    files.map((file, index) =>
      cache.put(
        `/shared/${index}`,
        new Response(file, {
          headers: {
            "Content-Type": file.type || "application/octet-stream",
            "X-Bag-Filename": encodeURIComponent(file.name || "shared"),
          },
        }),
      ),
    ),
  );
  await cache.put("/shared/count", new Response(String(files.length)));
  const text = [form.get("url"), form.get("title"), form.get("text")]
    .filter((part) => typeof part === "string" && part.trim())
    .join("\n");
  const params = new URLSearchParams();
  if (text) params.set("text", text);
  if (files.length) params.set("files", String(files.length));
  return Response.redirect(`/#/share?${params}`, 303);
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method === "POST" && url.origin === location.origin && url.pathname === "/share") {
    event.respondWith(stashShare(event.request));
    return;
  }
  if (event.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) {
    return;
  }
  if (event.request.mode === "navigate") {
    // Network first so deployments show up; fall back to the cached shell offline.
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          const copy = response.clone();
          caches.open(VERSION).then((cache) => cache.put("/index.html", copy));
          return response;
        })
        .catch(() => caches.match("/index.html")),
    );
    return;
  }
  if (url.pathname.startsWith("/assets/")) {
    // Hashed file names: cache first, forever.
    event.respondWith(
      caches.match(event.request).then(
        (hit) =>
          hit ??
          fetch(event.request).then((response) => {
            const copy = response.clone();
            caches.open(VERSION).then((cache) => cache.put(event.request, copy));
            return response;
          }),
      ),
    );
  }
});
