/* Cache only the public application shell. Never cache API requests, tokens or live state. */
const CACHE = 'dcs-copilot-shell-v26';
self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    const shell = await fetch('/', {cache:'reload'});
    if (!shell.ok) throw new Error('App shell unavailable');
    const html = await shell.clone().text();
    const assets = [...html.matchAll(/(?:src|href)="(\/assets\/[^"?]+)"/g)].map(match => match[1]);
    await cache.put('/', shell);
    await cache.addAll([...new Set(['/icon.svg', '/manifest.webmanifest', ...assets])]);
  })());
  self.skipWaiting();
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('dcs-copilot-shell-') && key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;
  const publicAsset = url.pathname.startsWith('/assets/') || ['/', '/index.html', '/icon.svg', '/manifest.webmanifest'].includes(url.pathname);
  if (!publicAsset) return;
  event.respondWith(fetch(event.request).then(response => {
    if (response.ok) { const copy = response.clone(); caches.open(CACHE).then(cache => cache.put(event.request, copy)); }
    return response;
  }).catch(() => caches.match(event.request).then(cached => cached || (event.request.mode === 'navigate' ? caches.match('/') : Response.error()))));
});
