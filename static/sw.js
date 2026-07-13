const CACHE_VERSION = "brownberries-pwa-v1";
const STATIC_CACHE = `static-${CACHE_VERSION}`;
const APP_SHELL = [
  "/",
  "/static/css/style.css",
  "/static/images/pwa-icon-192.png",
  "/static/images/pwa-icon-512.png",
  "/static/images/pwa-icon-512-maskable.png",
  "/static/images/pwa-icon-180.png",
  "/static/images/cafe-logo.png",
  "/static/manifest.webmanifest"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => cache.addAll(APP_SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== STATIC_CACHE)
          .map((key) => caches.delete(key))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/socket.io")) return;

  const isStaticAsset =
    url.pathname.startsWith("/static/") ||
    url.pathname.endsWith(".png") ||
    url.pathname.endsWith(".jpg") ||
    url.pathname.endsWith(".jpeg") ||
    url.pathname.endsWith(".webp") ||
    url.pathname.endsWith(".wav") ||
    url.pathname.endsWith(".css") ||
    url.pathname.endsWith(".js");

  if (isStaticAsset) {
    event.respondWith(
      caches.match(request).then((cached) => {
        const networkFetch = fetch(request)
          .then((response) => {
            if (response && response.status === 200) {
              const cloned = response.clone();
              caches.open(STATIC_CACHE).then((cache) => cache.put(request, cloned));
            }
            return response;
          })
          .catch(() => cached);
        return cached || networkFetch;
      })
    );
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => response)
        .catch(() => caches.match("/"))
    );
  }
});
