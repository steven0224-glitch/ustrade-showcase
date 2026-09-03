// 대시보드 서비스워커 — 오프라인/터널 끊김에서도 앱 셸 로드.
// 셸: 네트워크 우선 + 실패 시 캐시(온라인이면 항상 최신, 오프라인이면 캐시본).
// API(/api/*): 절대 캐시 안 함 — 실 계좌·실데이터라 항상 네트워크.
const CACHE = 'ustrade-dash-v1';
const SHELL = ['./', './index.html', './data.js', './manifest.json', './icon.svg'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()).catch(() => {}));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;                       // 제어 POST 등은 패스
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/api/')) return;                 // API = 네트워크 전용(캐시 금지)
  e.respondWith(
    fetch(e.request).then(resp => {
      const copy = resp.clone();
      caches.open(CACHE).then(c => c.put(e.request, copy)).catch(() => {});
      return resp;
    }).catch(() => caches.match(e.request).then(r => r || caches.match('./index.html')))
  );
});
