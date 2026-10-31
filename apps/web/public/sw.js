/* Offline app shell. API responses are never cached: the server is the memory. */
const VERSION = "bag-shell-v1";
const SHELL = ["/", "/index.html", "/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(VERSION).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  // Only stale shell caches go; SHARE_CACHE holds unsaved user data and is never swept.
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== VERSION && key !== SHARE_CACHE).map((key) => caches.delete(key)))),
  );
  self.clients.claim();
});

/* Pending shares live in their own cache and must survive app updates (see activate). */
const SHARE_CACHE = "bag-share";

/*
 * Web Share Target (POST): store the whole share as one batch under /shared/<batch>/,
 * then let the app save it with the session. File entries are written first and the
 * meta entry last: a batch without meta was never acknowledged. If storing fails, the
 * partly written batch is removed and the error surfaces instead of a false redirect.
 */
async function stashShare(request) {
  const form = await request.formData();
  const fields = { url: [], title: [], text: [] };
  for (const name of Object.keys(fields)) {
    for (const value of form.getAll(name)) {
      if (typeof value === "string" && value.trim()) fields[name].push(value);
    }
  }
  const files = form.getAll("files").filter((entry) => entry instanceof File);
  if (!files.length && !fields.url.length && !fields.title.length && !fields.text.length) {
    return Response.redirect("/#/", 303);
  }
  const batchId = crypto.randomUUID();
  const stored = files.map((file) => ({
    id: crypto.randomUUID(),
    name: file.name || "shared",
    type: file.type || "application/octet-stream",
    size: file.size,
  }));
  const cache = await caches.open(SHARE_CACHE);
  const written = [];
  try {
    for (const [index, file] of files.entries()) {
      const path = `/shared/${batchId}/${stored[index].id}`;
      await cache.put(path, new Response(file, { headers: { "Content-Type": stored[index].type } }));
      written.push(path);
    }
    const meta = { id: batchId, created_at: new Date().toISOString(), ...fields, files: stored };
    const metaPath = `/shared/${batchId}/meta`;
    written.push(metaPath);
    await cache.put(metaPath, new Response(JSON.stringify(meta), { headers: { "Content-Type": "application/json" } }));
  } catch (error) {
    await Promise.all(written.map((path) => cache.delete(path).catch(() => false)));
    throw error;
  }
  return Response.redirect(`/#/share?batch=${batchId}`, 303);
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
