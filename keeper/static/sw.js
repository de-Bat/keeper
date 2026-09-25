// Keeper service worker: makes the app open and render offline.
// Library data and queued changes live in IndexedDB (see app.js); this worker only
// caches the app shell and images, and hands shared screenshots to the page.
const VERSION = "v3";
const SHELL = `keeper-shell-${VERSION}`;
const IMAGES = "keeper-images";
const SHELL_FILES = [
  "/",
  "/static/app.js",
  "/static/style.css",
  "/static/manifest.webmanifest",
  "/static/icons/apple-touch-icon.png",
  "/static/icons/icon-192.png",
  "/static/icons/favicon.png",
];
const MAX_IMAGES = 600;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key.startsWith("keeper-shell-") && key !== SHELL) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Share target (Android/desktop Chrome; iOS doesn't support it): stash files for the page.
  if (req.method === "POST" && url.origin === location.origin && url.pathname === "/share-target") {
    event.respondWith(receiveShare(req));
    return;
  }
  if (req.method !== "GET") return;

  if (url.origin === location.origin) {
    if (url.pathname.startsWith("/api/")) return;                 // app.js handles API + offline
    if (url.pathname.startsWith("/media/")) return event.respondWith(cacheFirst(req, IMAGES));
    if (req.mode === "navigate") return event.respondWith(staleWhileRevalidate(new Request("/"), SHELL));
    if (url.pathname.startsWith("/static/")) return event.respondWith(staleWhileRevalidate(req, SHELL));
    return;
  }
  // Posters and previews from TMDB, GitHub, recipe sites...
  if (req.destination === "image") event.respondWith(cacheFirst(req, IMAGES));
});

async function staleWhileRevalidate(req, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(req, { ignoreSearch: true });
  const network = fetch(req)
    .then((res) => { if (res.ok) cache.put(req, res.clone()); return res; })
    .catch(() => null);
  return cached || (await network) || new Response("Offline", { status: 503 });
}

async function cacheFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(req);
  if (cached) return cached;
  try {
    const res = await fetch(req);
    if (res.ok || res.type === "opaque") {
      await cache.put(req, res.clone());
      trim(cache);
    }
    return res;
  } catch {
    return new Response("", { status: 504 });
  }
}

async function trim(cache) {
  const keys = await cache.keys();
  for (const key of keys.slice(0, Math.max(0, keys.length - MAX_IMAGES))) await cache.delete(key);
}

async function receiveShare(req) {
  const form = await req.formData();
  const files = form.getAll("file").filter((f) => f && f.type && f.type.startsWith("image/"));
  const note = form.get("note") || "";
  const cache = await caches.open("keeper-share-inbox");
  let i = 0;
  for (const file of files) {
    await cache.put(`/share-inbox/${Date.now()}-${i++}`, new Response(file, {
      headers: { "Content-Type": file.type, "X-Keeper-Note": encodeURIComponent(note) },
    }));
  }
  return Response.redirect("/?shared=1", 303);
}
