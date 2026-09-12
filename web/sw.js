const CACHE = "btc-dept-v2";
const SHELL = ["/", "/static/app.js", "/static/style.css", "/static/icon.svg", "/manifest.json"];
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/api/")) return; // данные всегда с сервера
  e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request)));
});
