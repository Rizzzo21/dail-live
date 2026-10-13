/* DAiL mobile PWA service worker — offline-first app shell.
 *
 * Strategy
 *   - App shell (HTML/CSS/JS/icons/manifest): cache-first, refreshed in the
 *     background (stale-while-revalidate) so the shell loads offline.
 *   - API reads (GET /world/bounties, /ledger/...): network-first with a
 *     cached fallback, so the UI still shows the last known board offline.
 *   - Writes (POST/PATCH): never cached; queued for background sync retry
 *     when the connection returns.
 */
const VERSION = "dail-pwa-v1";
const SHELL = [
  "/static/pwa/", "/static/pwa/index.html", "/static/pwa/styles.css", "/static/pwa/app.js",
  "/static/pwa/manifest.webmanifest",
  "/static/pwa/icons/icon-192.png", "/static/pwa/icons/icon-512.png", "/static/pwa/icons/maskable-512.png",
];
const API_CACHE = VERSION + "-api";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== VERSION && k !== API_CACHE)
        .map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Writes: pass through; if the network is down, queue for background sync.
  if (req.method !== "GET") {
    event.respondWith(
      fetch(req).catch(async () => {
        const body = await req.clone().text().catch(() => "");
        await queueWrite({ url: req.url, method: req.method, body,
                           headers: [...req.headers] });
        if ("sync" in self.registration) {
          await self.registration.sync.register("dail-write-queue");
        }
        return new Response(JSON.stringify({ queued: true, offline: true }),
                            { status: 202, headers: { "Content-Type": "application/json" } });
      })
    );
    return;
  }

  // API GETs: network-first, then cache.
  if (url.pathname.startsWith("/world/") || url.pathname.startsWith("/ledger/")) {
    event.respondWith(
      fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(API_CACHE).then((c) => c.put(req, copy));
        return res;
      }).catch(() => caches.match(req))
    );
    return;
  }

  // App shell: stale-while-revalidate.
  event.respondWith(
    caches.match(req).then((cached) => {
      const network = fetch(req).then((res) => {
        if (res && res.status === 200) {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put(req, copy));
        }
        return res;
      }).catch(() => cached);
      return cached || network;
    })
  );
});

// ---- offline write queue -------------------------------------------------- //
const DB = "dail-writes";

function idb() {
  return new Promise((resolve, reject) => {
    const r = indexedDB.open(DB, 1);
    r.onupgradeneeded = () => r.result.createObjectStore("q", { autoIncrement: true });
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}

async function queueWrite(item) {
  const db = await idb();
  const tx = db.transaction("q", "readwrite");
  tx.objectStore("q").add(item);
  return new Promise((res) => (tx.oncomplete = res));
}

async function drainWrites() {
  const db = await idb();
  const tx = db.transaction("q", "readwrite");
  const store = tx.objectStore("q");
  const all = await new Promise((res) => {
    const r = store.getAll();
    r.onsuccess = () => res(r.result || []);
  });
  for (const w of all) {
    try {
      await fetch(w.url, { method: w.method, body: w.body || undefined,
                           headers: w.headers ? new Headers(w.headers) : undefined });
      await new Promise((res) => {
        const t = db.transaction("q", "readwrite");
        t.objectStore("q").delete(w[0] === undefined ? w.key : undefined);
        t.oncomplete = res;
      });
    } catch (e) { /* keep it queued */ }
  }
}

// ---- push notifications for bounty events on my claims ------------------- //
self.addEventListener("push", (event) => {
  let data = { title: "DAiL", body: "Bounty event on one of your claims." };
  try { data = event.data ? event.data.json() : data; } catch (e) {}
  event.waitUntil(self.registration.showNotification(data.title, {
    body: data.body, icon: "/static/pwa/icons/icon-192.png", badge: "/static/pwa/icons/icon-192.png",
    data: data.url || "/",
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(clients.openWindow(event.notification.data || "/"));
});

self.addEventListener("sync", (event) => {
  if (event.tag === "dail-write-queue") event.waitUntil(drainWrites());
});
