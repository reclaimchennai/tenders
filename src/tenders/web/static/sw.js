// Service worker — makes the app installable, and receives push notifications.
// Network pass-through: caches nothing, so deploys go live immediately.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => { /* pass through to network */ });

/* A push whose payload cannot be read still has to raise a notification.
   Every browser that implements Web Push enforces "userVisibleOnly": a push
   handler that ends without showing something gets one warning and then has its
   subscription revoked by the browser. So the parse failure path is not a
   nicety, it is what keeps the subscription alive. */
function readPayload(event) {
  try {
    return event.data ? event.data.json() : {};
  } catch (e) {
    return {};
  }
}

self.addEventListener('push', function (event) {
  var d = readPayload(event);
  var title = d.title || 'TN Tenders Mirror';
  var options = {
    body: d.body || 'Something you are watching has changed.',
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    /* The URL travels on the notification itself rather than in a variable the
       worker would have to still be alive to remember: a service worker is
       killed between the push and the tap, and often is. */
    data: { url: d.url || '/bookmarks' },
    /* The tag decides what stacks and what replaces, and the server picks it
       per *subject* rather than per sender: `tender-<id>` for a new match, so
       three new tenders arrive as three notifications the reader can dismiss
       and open one at a time, and a shared `watch-<id>` / `alerts` only on the
       summaries, where a phone that was off for an hour should wake to the
       current count and not to a pile of superseded ones. */
    tag: d.tag || 'tenders',
    renotify: !!d.tag,
    timestamp: Date.now()
  };
  /* Any tab that happens to be open is told as well, so the Saved page updates
     its "last notified" line while the user is looking at it instead of going
     stale behind a notification they just received. It is also the only way to
     observe that a push arrived without a notification tray, which is what
     makes this path testable in a headless browser. */
  event.waitUntil(Promise.all([
    self.registration.showNotification(title, options),
    self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then(function (list) {
        list.forEach(function (client) {
          try { client.postMessage({ type: 'tender:push', payload: d }); } catch (e) {}
        });
      })
  ]));
});

self.addEventListener('notificationclick', function (event) {
  var url = (event.notification.data && event.notification.data.url) || '/bookmarks';
  event.notification.close();
  /* Focus an existing tab on the same origin and steer it, rather than opening
     a duplicate. On Android the app is usually already running behind the
     notification shade, and openWindow there would leave two copies of the PWA. */
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then(function (list) {
        for (var i = 0; i < list.length; i++) {
          var client = list[i];
          if (new URL(client.url).origin === self.location.origin && 'focus' in client) {
            if ('navigate' in client) { return client.navigate(url).then(function (c) { return c.focus(); }); }
            return client.focus();
          }
        }
        return self.clients.openWindow(url);
      })
  );
});

/* The push service can rotate a subscription out from under us. Without this the
   old endpoint sits in the database 410-ing forever and the user silently stops
   receiving anything they asked for. */
self.addEventListener('pushsubscriptionchange', function (event) {
  event.waitUntil(
    fetch('/api/push/key')
      .then(function (r) { return r.json(); })
      .then(function (cfg) {
        if (!cfg.enabled) return null;
        return self.registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: cfg.key
        });
      })
      .then(function (sub) {
        if (!sub) return null;
        var body = { subscription: sub.toJSON() };
        if (event.oldSubscription) { body.replaces = event.oldSubscription.endpoint; }
        return fetch('/api/push/register', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
      })
      .catch(function () { /* next page load re-registers */ })
  );
});
