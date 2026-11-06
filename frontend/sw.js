// Service Worker de CodIA
// Cachea los archivos estáticos (el "shell" de la app) para que abra
// instantáneamente y se pueda instalar. El chat en sí sigue necesitando
// internet, porque las respuestas vienen del backend + Gemini/Groq.

const CACHE_NAME = 'codia-shell-v3';
const ARCHIVOS_A_CACHEAR = [
    './',
    './index.html',
    './manifest.json',
    './icon.svg',
];

self.addEventListener('install', (evento) => {
    evento.waitUntil(
        caches.open(CACHE_NAME).then((cache) => cache.addAll(ARCHIVOS_A_CACHEAR))
    );
    self.skipWaiting();
});

self.addEventListener('activate', (evento) => {
    evento.waitUntil(
        caches.keys().then((nombres) =>
            Promise.all(
                nombres
                    .filter((nombre) => nombre !== CACHE_NAME)
                    .map((nombre) => caches.delete(nombre))
            )
        )
    );
    self.clients.claim();
});

self.addEventListener('fetch', (evento) => {
    // Nunca cacheamos las llamadas a la API: siempre deben ir a la red.
    if (evento.request.url.includes('/api/')) return;

    // Primero la red (así siempre llega la versión nueva de la app) y, si no
    // hay internet, lo que haya guardado en caché.
    evento.respondWith(
        fetch(evento.request)
            .then((respuesta) => {
                if (evento.request.method === 'GET' && respuesta.ok) {
                    const copia = respuesta.clone();
                    caches.open(CACHE_NAME).then((cache) => cache.put(evento.request, copia));
                }
                return respuesta;
            })
            .catch(() => caches.match(evento.request))
    );
});
