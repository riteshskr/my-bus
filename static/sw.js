// sw.js - Service Worker for Background GPS
self.addEventListener('install', (event) => {
    console.log('Service Worker: Installed');
    self.skipWaiting(); // Activate immediately
});

self.addEventListener('activate', (event) => {
    console.log('Service Worker: Activated');
    event.waitUntil(clients.claim());
});

// Background Sync for GPS data
self.addEventListener('sync', (event) => {
    if (event.tag === 'gps-sync') {
        console.log('Service Worker: GPS Sync triggered');
        event.waitUntil(syncGPSData());
    }
});

// Periodic background updates
self.addEventListener('periodicsync', (event) => {
    if (event.tag === 'gps-update') {
        console.log('Service Worker: Periodic GPS update');
        event.waitUntil(updateGPS());
    }
});

// Handle fetch events
self.addEventListener('fetch', (event) => {
    // Cache GPS requests for offline use
    if (event.request.url.includes('/driver_gps')) {
        event.respondWith(
            fetch(event.request)
                .then(response => {
                    // Clone response to cache
                    const clone = response.clone();
                    caches.open('gps-cache').then(cache => {
                        cache.put(event.request, clone);
                    });
                    return response;
                })
                .catch(() => {
                    // If offline, return cached response
                    return caches.match(event.request);
                })
        );
    }
});

// Sync GPS data when online
async function syncGPSData() {
    const cache = await caches.open('gps-cache');
    const requests = await cache.keys();
    
    for (const request of requests) {
        try {
            const response = await cache.match(request);
            if (response) {
                const data = await response.json();
                
                // Try to send to server
                await fetch('/api/gps-background', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                });
                
                // Remove from cache if successful
                await cache.delete(request);
                console.log('Service Worker: GPS data synced');
            }
        } catch (err) {
            console.log('Service Worker: Sync failed, keeping in cache');
        }
    }
}

// Periodic GPS update
async function updateGPS() {
    // Get last known location from cache
    const cache = await caches.open('gps-cache');
    const lastRequest = await cache.keys().then(keys => keys[keys.length - 1]);
    
    if (lastRequest) {
        const response = await cache.match(lastRequest);
        if (response) {
            const data = await response.json();
            
            // Update timestamp and send
            data.timestamp = new Date().toISOString();
            data.background = true;
            
            try {
                await fetch('/api/gps-periodic', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                });
            } catch (err) {
                console.log('Service Worker: Periodic update failed');
            }
        }
    }
}

// Handle push notifications for background
self.addEventListener('push', (event) => {
    const data = event.data ? event.data.json() : {};
    
    const options = {
        body: data.body || 'GPS is running in background',
        icon: '/icon.png',
        badge: '/badge.png',
        tag: 'gps-notification',
        requireInteraction: true,
        actions: [
            { action: 'open', title: 'Open App' },
            { action: 'close', title: 'Close' }
        ]
    };
    
    event.waitUntil(
        self.registration.showNotification('GPS Tracker', options)
    );
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    
    if (event.action === 'open') {
        event.waitUntil(
            clients.openWindow('/driver/1')
        );
    }
});