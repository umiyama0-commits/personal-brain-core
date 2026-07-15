// Personal Brain service worker
// Build timestamp injected by server via /static/sw.js?v=...
const CACHE_VERSION = self.location.search || 'v0';
const CACHE_NAME = 'brain-' + CACHE_VERSION;

// Static assets to precache
const STATIC_ASSETS = [
  '/static/manifest.json',
  '/static/common.js',
  '/static/icon-192.png',
  '/static/icon-512.png',
];

self.addEventListener('install', e => {
  // Precache assets, but don't fail whole install if some are missing
  e.waitUntil(
    caches.open(CACHE_NAME).then(c =>
      Promise.all(STATIC_ASSETS.map(a => c.add(a).catch(() => {})))
    )
  );
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  // Delete all old caches
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const { request } = e;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  // Never cache API responses (freshness matters)
  if (url.pathname.startsWith('/api/')) return;

  // Network-first for HTML (so new deploys pick up quickly)
  if (request.mode === 'navigate' || url.pathname.endsWith('.html') ||
      url.pathname === '/chat' || url.pathname === '/dashboard') {
    e.respondWith(
      fetch(request).then(resp => {
        if (resp && resp.ok) {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then(c => c.put(request, clone));
        }
        return resp;
      }).catch(() => caches.match(request))
    );
    return;
  }

  // Cache-first for static assets (they're versioned)
  e.respondWith(
    caches.match(request).then(cached => {
      if (cached) return cached;
      return fetch(request).then(resp => {
        if (resp && resp.ok && resp.status < 400) {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then(c => c.put(request, clone));
        }
        return resp;
      });
    })
  );
});

// Allow page to trigger skip-waiting via postMessage
self.addEventListener('message', e => {
  if (e.data === 'skip-waiting') self.skipWaiting();
});
