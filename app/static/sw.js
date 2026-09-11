const CACHE='nas-pdf-reader-v0.3.4';
const SHELL=[
  '/static/style.css?v=0.3.4',
  '/static/app.js?v=0.3.4',
  '/static/reading-cache.js?v=0.3.4',
  '/static/jszip.min.js?v=0.3.4',
  '/static/reader.js?v=0.3.4',
  '/manifest.webmanifest'
];
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL)).then(()=>self.skipWaiting())));
self.addEventListener('activate',e=>e.waitUntil(Promise.all([
  caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))),
  self.clients.claim()
])));
self.addEventListener('fetch',e=>{
  if(e.request.method!=='GET')return;
  const u=new URL(e.request.url);
  if(u.origin!==location.origin)return;
  if(e.request.mode==='navigate'||u.pathname.startsWith('/api/'))return;
  if(!u.pathname.startsWith('/static/')&&u.pathname!=='/manifest.webmanifest')return;
  e.respondWith(caches.match(e.request).then(hit=>{
    if(hit)return hit;
    return fetch(e.request).then(r=>{
      if(r&&r.ok){const x=r.clone();caches.open(CACHE).then(c=>c.put(e.request,x));}
      return r;
    });
  }));
});
