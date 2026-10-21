/**
 * PortCare – service worker (installable app + push notifications)
 * - Network first for the app shell (a new deploy is live immediately);
 *   successful responses are cached so the shell still opens offline.
 * - /api/ calls are never touched (always live, always authenticated).
 * - push: shows the notification sent by the server (app/push.py):
 *   JSON {title, body, url, tag}; tapping it opens / focuses the app.
 */
const CACHE = "portcare-shell-v1";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key !== CACHE && key !== "portcare-meta") await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/") || url.pathname === "/healthz") return;
  const key = url.origin + url.pathname;
  event.respondWith((async () => {
    try {
      const res = await fetch(req);
      if (res.ok && res.type === "basic") {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(key, copy)).catch(() => {});
      }
      return res;
    } catch (err) {
      const hit = await caches.match(key);
      if (hit) return hit;
      if (req.mode === "navigate") {
        const shell = await caches.match(new URL("./", self.registration.scope).href);
        if (shell) return shell;
      }
      throw err;
    }
  })());
});

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = { body: event.data ? event.data.text() : "" }; }
  const mark = caches.open("portcare-meta")
    .then((c) => c.put("/__last_push", new Response(String(Date.now())))).catch(() => {});
  event.waitUntil(mark.then(() => self.registration.showNotification(data.title || "PortCare", {
    body: data.body || "",
    icon: "icons/icon-192.png",
    badge: "icons/badge-96.png",
    tag: data.tag || undefined,
    renotify: !!data.tag,
    data: { url: data.url || "./" },
  })));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL(event.notification.data && event.notification.data.url || "./", self.registration.scope).href;
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of wins) {
      if (w.url.startsWith(self.registration.scope)) {
        await w.focus();
        if ("navigate" in w) return w.navigate(target);
        return;
      }
    }
    return self.clients.openWindow(target);
  })());
});
