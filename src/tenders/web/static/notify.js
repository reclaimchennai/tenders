/* Push notifications: capability detection, the permission gesture, and the two
   controls that create subscriptions (watch a search, alert on a tender).

   Loaded on every page as window.tenderNotify. Plain ES5, no framework, no
   third-party host — same constraints as the rest of this site.

   THE SUPPORT MATRIX THIS IMPLEMENTS
   ---------------------------------
   Android / Chrome / Edge / Firefox / Samsung Internet: works in the browser
     tab and in the installed PWA. Nothing special is needed.
   iOS + iPadOS Safari 16.4+: works ONLY once the site has been added to the
     Home Screen. In a normal Safari tab PushManager does not exist at all, so
     the failure is silent and looks like a broken button — hence support()
     names that case specifically and the UI prints the Share -> Add to Home
     Screen instruction instead of failing.
   iOS below 16.4: no Web Push at any price.
   Desktop Safari 16+ / macOS: works.
   Anything without a service worker (private windows in some browsers, very old
     Android WebViews): reported as unsupported rather than left to throw. */
(function () {
  'use strict';

  var API = {
    key: '/api/push/key',
    register: '/api/push/register',
    subscribe: '/api/watch/subscribe',
    unsubscribe: '/api/watch/unsubscribe',
    rename: '/api/watch/rename',
    list: '/api/watch/list',
    alertOn: '/api/alert/subscribe',
    alertOff: '/api/alert/unsubscribe',
    forget: '/api/push/forget',
    test: '/api/push/test'
  };

  function isIOS() {
    var ua = navigator.userAgent || '';
    /* iPadOS 13+ reports itself as a Mac; maxTouchPoints is what still tells
       them apart, and it matters because the Home Screen rule applies to it. */
    return /iPad|iPhone|iPod/.test(ua) ||
      (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
  }

  function isStandalone() {
    return window.navigator.standalone === true ||
      (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches);
  }

  function platform() {
    if (isIOS()) return 'ios';
    if (/Android/.test(navigator.userAgent || '')) return 'android';
    return 'desktop';
  }

  /* Returns {ok, reason, message}. Never throws, and every not-ok case carries
     a sentence that can be shown to a person as-is. */
  function support() {
    if (!('serviceWorker' in navigator)) {
      return { ok: false, reason: 'no-sw',
        message: 'This browser cannot receive notifications (no service worker support). ' +
                 'Try Chrome or Firefox.' };
    }
    if (!('PushManager' in window)) {
      if (isIOS() && !isStandalone()) {
        return { ok: false, reason: 'ios-install',
          message: 'On iPhone and iPad, notifications only work once this site is ' +
                   'installed. Tap the Share button, then “Add to Home Screen”, and ' +
                   'open it from there. Requires iOS 16.4 or later.' };
      }
      return { ok: false, reason: 'no-push',
        message: 'This browser does not support Web Push.' };
    }
    if (!('Notification' in window)) {
      return { ok: false, reason: 'no-notification',
        message: 'This browser does not support notifications.' };
    }
    if (Notification.permission === 'denied') {
      return { ok: false, reason: 'denied',
        message: 'Notifications are blocked for this site. Turn them back on in ' +
                 'your browser’s site settings, then try again.' };
    }
    if (isIOS() && !isStandalone()) {
      return { ok: false, reason: 'ios-install',
        message: 'On iPhone and iPad, notifications only work once this site is ' +
                 'installed. Tap the Share button, then “Add to Home Screen”, and ' +
                 'open it from there.' };
    }
    return { ok: true, reason: 'ok', message: '' };
  }

  function urlBase64ToUint8Array(base64String) {
    var padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    var base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    var raw = window.atob(base64);
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; ++i) { out[i] = raw.charCodeAt(i); }
    return out;
  }

  function postJSON(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) {
          var err = new Error(data.detail || ('HTTP ' + r.status));
          err.status = r.status;
          throw err;
        }
        return data;
      });
    });
  }

  var _serverKey = null;
  function serverKey() {
    if (_serverKey) return Promise.resolve(_serverKey);
    return fetch(API.key).then(function (r) {
      if (!r.ok) throw new Error('push is not configured on this mirror');
      return r.json();
    }).then(function (cfg) {
      if (!cfg.enabled) throw new Error('push is not configured on this mirror');
      _serverKey = cfg.key;
      return _serverKey;
    });
  }

  /* The existing subscription, or null. Never prompts — safe on page load. */
  function current() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      return Promise.resolve(null);
    }
    return navigator.serviceWorker.ready
      .then(function (reg) { return reg.pushManager.getSubscription(); })
      .catch(function () { return null; });
  }

  /* Subscribe, prompting for permission if we do not have it.
     MUST be called from inside a click handler: Chrome and Safari both require
     a transient user activation for Notification.requestPermission, and a
     prompt that appears on page load is hostile anyway. */
  function enable() {
    var s = support();
    if (!s.ok) return Promise.reject(Object.assign(new Error(s.message), { reason: s.reason }));
    return navigator.serviceWorker.register('/sw.js')
      .then(function () { return navigator.serviceWorker.ready; })
      .then(function (reg) {
        return reg.pushManager.getSubscription().then(function (existing) {
          if (existing) return existing;
          return Notification.requestPermission().then(function (perm) {
            if (perm !== 'granted') {
              var err = new Error(perm === 'denied'
                ? 'You blocked notifications for this site. Turn them back on in your ' +
                  'browser’s site settings.'
                : 'Notifications were not enabled.');
              err.reason = perm;
              throw err;
            }
            return serverKey().then(function (key) {
              return reg.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: urlBase64ToUint8Array(key)
              });
            });
          });
        });
      })
      .then(function (sub) {
        return postJSON(API.register, {
          subscription: sub.toJSON(), platform: platform()
        }).then(function (data) { return { subscription: sub, server: data }; });
      });
  }

  function withSubscription(fn) {
    return current().then(function (sub) {
      if (sub) return fn(sub);
      return enable().then(function (res) { return fn(res.subscription); });
    });
  }

  var api = {
    support: support,
    platform: platform,
    isIOS: isIOS,
    isStandalone: isStandalone,
    current: current,
    enable: enable,

    /* Server-side state for this browser, or null when it has never subscribed. */
    state: function () {
      return current().then(function (sub) {
        if (!sub) return null;
        return postJSON(API.list, { endpoint: sub.endpoint })
          .catch(function (err) {
            /* 404 means the server forgot us (deleted as dead, or the DB was
               replaced). Re-register rather than showing an empty page. */
            if (err.status === 404) {
              return postJSON(API.register, { subscription: sub.toJSON(), platform: platform() });
            }
            throw err;
          });
      });
    },

    watch: function (filters, label) {
      return withSubscription(function (sub) {
        return postJSON(API.subscribe, {
          subscription: sub.toJSON(), endpoint: sub.endpoint,
          filters: filters, label: label || '', platform: platform()
        });
      });
    },
    unwatch: function (filters, id) {
      return current().then(function (sub) {
        if (!sub) return { ok: true, deleted: 0 };
        return postJSON(API.unsubscribe, { endpoint: sub.endpoint, filters: filters, id: id });
      });
    },
    rename: function (id, label) {
      return current().then(function (sub) {
        return postJSON(API.rename, { endpoint: sub.endpoint, id: id, label: label });
      });
    },
    /* Rename by filters, which is the key the saved-search list has. A no-op
       when this browser is not subscribed — the local name is the only one. */
    renameByFilters: function (filters, label) {
      return current().then(function (sub) {
        if (!sub) return null;
        return postJSON(API.rename, { endpoint: sub.endpoint, filters: filters, label: label });
      }).catch(function () { return null; });
    },
    /* The server's canonical form of a querystring. Two routes to one search
       produce different querystrings, and only the server knows they are the
       same watch. Memoised: the saved list asks once per row. */
    canonical: (function () {
      var cache = {};
      return function (filters) {
        if (cache[filters]) return Promise.resolve(cache[filters]);
        return postJSON('/api/watch/preview', { filters: filters })
          .then(function (r) { cache[filters] = r.filters; return r.filters; });
      };
    })(),
    alertOn: function (tenderId) {
      return withSubscription(function (sub) {
        return postJSON(API.alertOn, {
          subscription: sub.toJSON(), endpoint: sub.endpoint,
          tender_id: tenderId, platform: platform()
        });
      });
    },
    alertOff: function (tenderId) {
      return current().then(function (sub) {
        if (!sub) return { ok: true, deleted: 0 };
        return postJSON(API.alertOff, { endpoint: sub.endpoint, tender_id: tenderId });
      });
    },
    test: function () {
      return withSubscription(function (sub) {
        return postJSON(API.test, { endpoint: sub.endpoint });
      });
    },
    /* Unsubscribe everywhere: the browser's own subscription is torn down and
       the server row deleted, so neither side keeps the endpoint. */
    forget: function () {
      return current().then(function (sub) {
        if (!sub) return { ok: true, deleted: 0 };
        var endpoint = sub.endpoint;
        return sub.unsubscribe().catch(function () { return null; })
          .then(function () { return postJSON(API.forget, { endpoint: endpoint }); });
      });
    }
  };

  window.tenderNotify = api;

  /* Saving a search is saved.js's job and needs no permission; this is only the
     delivery half. The toast lives there too, so a mirror with push switched
     off still has one. Every toast raised from this file is about the push
     subscription, so leaving `icon` off and letting saved.js default to the
     bell is correct everywhere this passthrough is actually called. */
  function toast(message, kind, action, icon) {
    if (window.tenderSaved) window.tenderSaved.toast(message, kind, action, icon);
  }
  api.toast = toast;

  /* ------------------------------------------------------------------ */
  /* Per-tender alert toggle. Lives beside the bookmark button on a tender page
     and is deliberately a second, separate decision: bookmarking keeps a tender
     in this browser, alerting tells the server one tender id. */
  function bindAlertToggles(root) {
    var buttons = (root || document).querySelectorAll('[data-alert-tender]');
    if (!buttons.length) return;
    Array.prototype.forEach.call(buttons, function (btn) {
      if (btn._bound) return;
      btn._bound = true;
      var offLabel = btn.getAttribute('aria-label') || '';
      var onLabel = btn.getAttribute('data-label-on') || offLabel;
      btn.addEventListener('click', function () {
        var id = btn.getAttribute('data-alert-tender');
        var on = btn.getAttribute('aria-pressed') === 'true';
        var s = support();
        if (!on && !s.ok) { toast(s.message, 'warn'); return; }
        btn.disabled = true;
        /* Collapsing the label starts on the click, not on the answer: the
           permission prompt and the subscribe round-trip that follow take long
           enough that a control which did nothing at all would read as dead.
           `bell-live` is what arms the transition at all (see .bell-lbl), so a
           state restored on page load never plays it. */
        btn.classList.add('bell-live');
        if (!on) btn.classList.add('is-busy');
        var done = function (state, message) {
          btn.setAttribute('aria-pressed', state ? 'true' : 'false');
          btn.classList.toggle('is-on', state);
          btn.classList.remove('is-busy');
          /* The visible label is animating to zero width, so the button's own
             accessible name is what is left to say which state it is in. */
          btn.setAttribute('aria-label', state ? onLabel : offLabel);
          btn.disabled = false;
          if (message) toast(message, state ? '' : 'warn');
        };
        if (on) {
          api.alertOff(id).then(function () { done(false, 'Alerts off for this tender.'); })
            .catch(function (err) { done(true, err.message || 'Could not turn alerts off.'); });
        } else {
          api.alertOn(id).then(function () {
            done(true, 'You will be notified if this tender is amended, cancelled or awarded.');
          }).catch(function (err) { done(false, err.message || 'Could not turn alerts on.'); });
        }
      });
    });
    api.state().then(function (state) {
      if (!state || !state.alerts) return;
      var on = {};
      state.alerts.forEach(function (a) { on[a.tender_id] = true; });
      Array.prototype.forEach.call(buttons, function (btn) {
        if (on[btn.getAttribute('data-alert-tender')]) {
          btn.setAttribute('aria-pressed', 'true');
          btn.classList.add('is-on');
          if (btn.getAttribute('data-label-on')) {
            btn.setAttribute('aria-label', btn.getAttribute('data-label-on'));
          }
        }
      });
    }).catch(function () {});
  }
  api.bindAlertToggles = bindAlertToggles;

  /* Re-register on load when permission is already granted, so a subscription
     the push service rotated is repaired without the user doing anything. */
  function refresh() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    current().then(function (sub) {
      if (!sub) return;
      postJSON(API.register, { subscription: sub.toJSON(), platform: platform() })
        .catch(function () {});
    });
  }

  /* The device-level controls on the Saved page. Bound by id and simply absent
     everywhere else, so no page needs to know whether they exist. */
  function bindTools() {
    var statusEl = document.getElementById('notify-status');
    var enableBtn = document.getElementById('notify-enable');
    if (!enableBtn) return;

    function paintStatus() {
      var s = support();
      current().then(function (sub) {
        if (sub && s.ok) {
          statusEl.className = 'notice hidden';
          enableBtn.textContent = 'Notifications are on';
          enableBtn.disabled = true;
          return;
        }
        enableBtn.disabled = false;
        enableBtn.textContent = 'Enable notifications';
        statusEl.className = 'notice' + (s.ok ? '' : ' warn');
        statusEl.textContent = s.ok
          ? 'This device is not receiving notifications yet.'
          : s.message;
      });
    }

    enableBtn.addEventListener('click', function () {
      enable().then(function () {
        toast('Notifications enabled on this device.');
        paintStatus();
      }).catch(function (err) { toast(err.message, 'warn'); paintStatus(); });
    });
    document.getElementById('notify-test').addEventListener('click', function () {
      api.test().then(function () { toast('Sent. It should arrive in a moment.'); })
        .catch(function (err) { toast(err.message, 'warn'); });
    });
    document.getElementById('notify-forget').addEventListener('click', function () {
      if (!window.confirm('Remove this device’s push address and every search and '
          + 'tender alert attached to it? Your saved lists stay in this browser.')) return;
      api.forget().then(function () {
        toast('Turned off. Nothing about this device is left on the server.');
        paintStatus();
      }).catch(function (err) { toast(err.message, 'warn'); });
    });
    paintStatus();
  }

  /* The service worker forwards every push to open tabs. A Saved page left open
     would otherwise still be claiming "last notified: never" a second after the
     phone buzzed. */
  function bindPushEcho() {
    if (!('serviceWorker' in navigator)) return;
    navigator.serviceWorker.addEventListener('message', function (ev) {
      var d = ev.data || {};
      if (d.type !== 'tender:push') return;
      window.dispatchEvent(new CustomEvent('tender:pushed', { detail: d.payload }));
      if (document.getElementById('search-list') && window.tenderSaved) {
        toast((d.payload && d.payload.title) || 'Something you follow changed.');
      }
    });
  }

  function init() {
    bindAlertToggles(document);
    bindTools();
    bindPushEcho();
    refresh();
  }
  /* Deferred scripts run with readyState already "interactive", so a
     `=== 'loading'` check would call init() immediately — before the other
     deferred scripts on the page had executed, and this one depends on two of
     them (multiselect.js publishes window.tenderWatch, notify.js publishes
     window.tenderNotify). DOMContentLoaded is the first moment every deferred
     script has run, and it has not fired until readyState is 'complete'. */
  if (document.readyState === 'complete') { init(); }
  else { document.addEventListener('DOMContentLoaded', init); }
})();
