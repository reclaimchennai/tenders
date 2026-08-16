/* Saved searches — the local half of "saved", and the watch button's storage.

   A saved search is a querystring, kept in this browser exactly the way saved
   tenders are (bookmarks.js, `tn_bookmarks`). It is stored here and nowhere
   else until the user separately asks to be *notified* about it, which is the
   only thing that requires a server to know. That ordering is deliberate:

   * saving a search is useful on its own — it replays as GET /browse?<filters>
     and keeps answering as the archive grows — and must not be behind a
     notification permission prompt, which is a hostile thing to spring on
     someone for pressing a bookmark;
   * and a site used to investigate government contracts should not learn what
     its users are investigating as a side effect of them keeping a note.

   notify.js layers the push opt-in on top of this and is loaded only when the
   mirror has push configured. Everything here works without it. */
(function () {
  'use strict';

  var KEY = 'tn_searches';

  function load() {
    try {
      var v = JSON.parse(localStorage.getItem(KEY) || '[]');
      return Array.isArray(v) ? v : [];
    } catch (e) { return []; }
  }
  function save(list) {
    try { localStorage.setItem(KEY, JSON.stringify(list)); } catch (e) {}
  }
  function find(list, filters) {
    for (var i = 0; i < list.length; i++) {
      if (list[i].filters === filters) return list[i];
    }
    return null;
  }

  /* A readable name for a filter set, computed in the browser so that saving
     works with no network at all. The server has its own describe() for the
     notification title; this one only has to be good enough to recognise. */
  function label(filters) {
    var parts = [], q = null, named = [];
    filters.split('&').forEach(function (pair) {
      if (!pair) return;
      var i = pair.indexOf('=');
      var k = decodeURIComponent(pair.slice(0, i).replace(/\+/g, ' '));
      var v = decodeURIComponent(pair.slice(i + 1).replace(/\+/g, ' ')).trim();
      if (!v) return;
      if (k === 'q') q = v;
      else if (k === 'org') named.push(v.split('||').pop().trim());
      else if (k === 'category' || k === 'tender_type' || k === 'product_category') named.push(v);
      else if (k === 'pincode') named.push('PIN ' + v);
      else if (k === 'value_min' || k === 'value_max') named.push('by value');
      else if (k === 'date_from' || k === 'date_to') named.push('by date');
      else if (k === 'criteria') named.push(v.replace(/_/g, ' '));
      else if (k === 'captured' && v === 'yes') named.push('with documents');
    });
    if (q) parts.push('“' + q + '”');
    named.forEach(function (n) { if (parts.indexOf(n) === -1) parts.push(n); });
    if (!parts.length) return 'All tenders';
    /* Naming three of five filters and stopping would misdescribe the search.
       Saying how many were left out keeps the label honest, and the row is
       renameable anyway. */
    var out = parts.slice(0, 3).join(' · ');
    if (out.length > 64) out = out.slice(0, 63) + '…';
    if (parts.length > 3) out += ' +' + (parts.length - 3) + ' more';
    return out;
  }

  var store = {
    list: function () { return load(); },
    has: function (filters) { return !!find(load(), filters); },
    get: function (filters) { return find(load(), filters); },
    add: function (filters, name) {
      var list = load();
      var hit = find(list, filters);
      if (hit) return hit;
      var entry = { filters: filters, label: name || label(filters),
                    saved_at: new Date().toISOString(), notify: false };
      list.unshift(entry);
      save(list);
      return entry;
    },
    remove: function (filters) {
      save(load().filter(function (s) { return s.filters !== filters; }));
    },
    rename: function (filters, name) {
      var list = load();
      var hit = find(list, filters);
      if (hit) { hit.label = name; save(list); }
      return hit;
    },
    setNotify: function (filters, on) {
      var list = load();
      var hit = find(list, filters);
      if (hit) { hit.notify = !!on; save(list); }
      return hit;
    },
    label: label
  };

  /* Every toast on-screen for the same length of time, action button or not:
     a fixed rhythm the user learns once, rather than one that guesses how long
     each message needs. The transition it slides in with (.notify-toast CSS)
     already backs off under prefers-reduced-motion; the 3s clock does not, so
     a reduced-motion toast still appears and clears on the same schedule, just
     without the slide. */
  var TOAST_MS = 3000;
  /* base.html renders bell/bookmark/alert once into an inert <template
     id="ic-lib">, specifically so a toast built in the browser can use the
     same glyph as the rest of the page without a second copy of the SVG path
     data living as a string inside this file — icon() in _icons.html stays
     the only place an icon is drawn. Cloned rather than referenced because a
     <use> back into a <template> does not render, and a node cannot sit in
     two toasts (i.e. two rapid saves) at once. */
  function toastIcon(name) {
    var lib = document.getElementById('ic-lib');
    var src = lib && lib.content.querySelector('[data-ic="' + name + '"] svg');
    return src ? src.cloneNode(true) : null;
  }

  /* Shared toast. Lives here rather than in notify.js because saving a search
     has to be able to say so on a mirror with no push configured at all.
     `action` is what makes the notification opt-in reachable in one tap from
     the moment of saving, instead of only from a page the user has to find.
     `icon` picks 'bell' or 'bookmark' and defaults to bell, since most toasts
     (every push/notify one, in notify.js) are about it — callers that are
     purely a local save/remove pass 'bookmark' explicitly. */
  function toast(message, kind, action, icon) {
    var el = document.getElementById('notify-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'notify-toast';
      el.setAttribute('role', 'status');
      document.body.appendChild(el);
    }
    el.innerHTML = '';
    var iconEl = document.createElement('span');
    iconEl.className = 'toast-icon';
    var svg = toastIcon(icon || 'bell');
    if (svg) iconEl.appendChild(svg);
    el.appendChild(iconEl);
    var text = document.createElement('span');
    text.className = 'toast-text';
    text.textContent = message;
    el.appendChild(text);
    if (action && action.label) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'toast-action';
      btn.textContent = action.label;
      btn.addEventListener('click', function () {
        hide();
        action.run();
      });
      el.appendChild(btn);
    }
    el.className = 'notify-toast show' + (kind ? ' ' + kind : '');
    el.style.pointerEvents = action ? 'auto' : 'none';
    clearTimeout(el._t);
    function hide() { el.className = 'notify-toast'; }
    el._t = setTimeout(hide, TOAST_MS);
  }

  window.tenderSaved = { searches: store, toast: toast };

  /* ---- The alert button on /browse ------------------------------------- */
  /* Binds to the contract documented in browse.html.

     This control used to wear a bookmark and behave like one: it saved locally,
     said so, and *offered* the notification prompt in the toast rather than
     raising it. It now wears a bell, and a bell that only writes to
     localStorage would be a promise the button does not keep — so pressing it
     goes and gets the permission it needs. The local save still happens first
     and still happens for everyone, including on a mirror with push switched
     off entirely; what changed is that the delivery half is no longer deferred
     to a second tap the user has to notice. */
  document.addEventListener('tender:watch', function (ev) {
    var filters = ev.detail.filters;
    var W = window.tenderWatch;
    if (!ev.detail.watching) {
      store.remove(filters);
      if (window.tenderNotify) window.tenderNotify.unwatch(filters).catch(function () {});
      toast('Alerts off, and this search is no longer saved here.', '', null, 'bell');
      return;
    }
    var entry = store.add(filters);
    var N = window.tenderNotify;
    if (!N) {
      toast('Saved to your searches. Open Saved to replay it any time.', '', null, 'bookmark');
      return;
    }
    /* Painting is deferred until the server has accepted the watch. A ringing
       bell over a subscription that failed — permission denied, push service
       unreachable, iOS without the site installed — is the single state this
       control must never show, so the optimistic paint is cancelled here and
       done by hand on the answer. */
    ev.preventDefault();
    if (W) W.busy(true);
    var settle = function (on, message, kind) {
      if (W) { W.busy(false); W.set(on); }
      store.setNotify(filters, on);
      if (message) toast(message, kind || '', null, 'bell');
    };
    N.watch(filters, entry.label).then(function () {
      settle(true, 'Alerts on. You will be told when a new tender matches.');
    }).catch(function (err) {
      /* The search stays saved: the user asked for two things and one of them
         succeeded, and silently discarding it would lose work. */
      settle(false, err.message || 'Could not turn alerts on.', 'warn');
    });
  });

  /* The bell means "the server will push to this device", so it is painted from
     the notify flag and not from mere presence in the saved list — which is the
     bookmark's meaning, and no longer this button's. */
  function paintWatchButton() {
    if (!window.tenderWatch) return;
    var entry = store.get(window.tenderWatch.filters);
    if (entry && entry.notify) window.tenderWatch.set(true);
  }

  /* ---- The saved-search list on /bookmarks ----------------------------- */
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* A human-readable filter summary for a saved-search card, replacing the raw
     querystring. Every value stored in `filters` is either free text the user
     typed (q, pincode, tender_id, ref_number) or one of the option strings
     browse.html rendered into a <select> from the database (org, category,
     tender_type, ...) — so unlike an id-to-name lookup, nothing here needs the
     server: it is the same display transform browse.html already applies with
     the `pretty` Jinja filter, ported to JS because this card is built from
     localStorage after the page has loaded, with no template engine at hand.
     Keep this in sync with shortnames.pretty_name/_fmt_inr by hand if those
     change — it is a handful of lines, not worth a build step to share. */
  var ACRONYMS = { AAZP: 1, AEE: 1, CMDA: 1, CMWSSB: 1, DCAPS: 1, ELCOT: 1, MAWS: 1, MTPS: 1,
    NCTPS: 1, SEESMTPSII: 1, TAMPCOL: 1, TANGEDCO: 1, TANTRANSCO: 1, TANUVAS: 1, TCMPF: 1,
    TNEB: 1, TNGECL: 1, TNHB: 1, TNPL: 1, TNRDC: 1, TNSTC: 1, TTPS: 1, TWAD: 1 };
  var SHORT_WORDS = { AND: 1, FOR: 1, THE: 1, OF: 1, AT: 1, IN: 1, ON: 1, TO: 1, BY: 1 };
  function recaseWord(word) {
    if (ACRONYMS[word] || !/[AEIOU]/.test(word)) return word;
    if (word.length <= 3 && !SHORT_WORDS[word]) return word;
    return word[0] + word.slice(1).toLowerCase();
  }
  function prettyName(value) {
    if (!value) return '';
    var spaced = String(value).replace(/_+/g, ' ').replace(/\s+/g, ' ').trim();
    if (/[a-z]/.test(value)) return spaced;
    return spaced.replace(/[A-Za-z]+/g, recaseWord);
  }
  // Same short form as web/dashboard.py's _fmt_inr: ₹1.2 Cr / ₹3.4 L / ₹500.
  function fmtINR(v) {
    var n = Number(v);
    if (!n) return '₹' + v;
    if (n >= 1e7) return '₹' + (n / 1e7).toFixed(2) + ' Cr';
    if (n >= 1e5) return '₹' + (n / 1e5).toFixed(2) + ' L';
    if (n >= 1e3) return '₹' + Math.round(n / 1e3) + 'K';
    return '₹' + Math.round(n);
  }
  var MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function fmtDateShort(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
    return m ? (+m[3]) + ' ' + MONTHS_SHORT[+m[2] - 1] + ' ' + m[1] : iso;
  }
  // Same labels as browse.html's `crit_labels` — the keys are the ones
  // search.py's CRITERIA whitelists, so a stray key here is simply dropped.
  var CRIT_LABELS = { two_stage: 'Two Stage Bidding', nda: 'NDA Tenders',
    preferential: 'Preferential Bidding', gte: 'GTE', ite: 'ITE / TPS',
    fee_exempt: 'Tender Fee Exemption', emd_exempt: 'EMD Exemption',
    withdrawal: 'Withdrawal Allowed' };

  /* Reshapes a saved querystring into the fields browse.html's form would have
     submitted. Array-valued keys mirror the six repeatable filters (org,
     category, tender_type, product_category, form_of_contract, payment_mode);
     everything else is single-valued. Keys the form does not have (stray or
     future querystring params) are ignored rather than guessed at. */
  function parseFilters(filters) {
    var out = { q: '', orgs: [], category: [], tender_type: [], product_category: [],
      form_of_contract: [], payment_mode: [], criteria: [], captured: false,
      tender_id: '', ref_number: '', pincode: '', value_min: '', value_max: '',
      date_from: '', date_to: '', scope: '' };
    (filters || '').split('&').forEach(function (pair) {
      if (!pair) return;
      var i = pair.indexOf('=');
      var k = decodeURIComponent(pair.slice(0, i).replace(/\+/g, ' '));
      if (k === 'org') k = 'orgs';
      if (!(k in out)) return;
      var v = decodeURIComponent(pair.slice(i + 1).replace(/\+/g, ' ')).trim();
      if (!v) return;
      if (Array.isArray(out[k])) out[k].push(v);
      else if (k === 'captured') out.captured = v === 'yes';
      else out[k] = v;
    });
    return out;
  }

  /* The chip row itself. Organisations render as the same breadcrumb chips
     (.orgchip/.orgsep) a tender's own org path uses elsewhere on the site, so
     a saved search reads in the same visual language as the results it will
     produce; every other active filter gets one labelled chip. */
  function filterChips(filters) {
    var f = parseFilters(filters);
    var chips = [];
    if (f.q) chips.push('<span class="ss-chip ss-kw">“' + esc(f.q) + '”</span>');
    if (f.orgs.length) {
      chips.push(f.orgs.map(function (chain) {
        var parts = chain.split('||').map(function (p) { return p.trim(); }).filter(Boolean);
        return '<span class="orgpath">' + parts.map(function (p, i) {
          return '<span class="orgchip' + (i === 0 ? ' lead-chip' : '') + '">'
            + esc(prettyName(p)) + '</span>'
            + (i < parts.length - 1 ? '<span class="orgsep">›</span>' : '');
        }).join('') + '</span>';
      }).join(''));
    }
    function labeled(label, arr, fmt) {
      if (arr && arr.length) chips.push('<span class="ss-chip"><b>' + label + ':</b> '
        + esc(arr.map(fmt || prettyName).join(', ')) + '</span>');
    }
    labeled('Category', f.category);
    labeled('Type', f.tender_type);
    labeled('Product category', f.product_category);
    labeled('Form of contract', f.form_of_contract);
    labeled('Payment mode', f.payment_mode);
    labeled('Criteria', f.criteria, function (k) { return CRIT_LABELS[k] || k.replace(/_/g, ' '); });
    if (f.value_min || f.value_max) {
      var vr = f.value_min && f.value_max ? fmtINR(f.value_min) + ' – ' + fmtINR(f.value_max)
        : f.value_min ? '≥ ' + fmtINR(f.value_min) : '≤ ' + fmtINR(f.value_max);
      chips.push('<span class="ss-chip"><b>Value:</b> ' + esc(vr) + '</span>');
    }
    if (f.date_from || f.date_to) {
      var dr = f.date_from && f.date_to
        ? fmtDateShort(f.date_from) + ' – ' + fmtDateShort(f.date_to)
        : f.date_from ? 'From ' + fmtDateShort(f.date_from) : 'Until ' + fmtDateShort(f.date_to);
      chips.push('<span class="ss-chip"><b>Published:</b> ' + esc(dr) + '</span>');
    }
    if (f.pincode) chips.push('<span class="ss-chip"><b>PIN:</b> ' + esc(f.pincode) + '</span>');
    if (f.tender_id) chips.push('<span class="ss-chip"><b>Tender ID:</b> ' + esc(f.tender_id) + '</span>');
    if (f.ref_number) chips.push('<span class="ss-chip"><b>Ref no:</b> ' + esc(f.ref_number) + '</span>');
    if (f.captured) chips.push('<span class="ss-chip">Has captured documents</span>');
    if (f.scope === 'meta') chips.push('<span class="ss-chip">Tenders only</span>');
    else if (f.scope === 'docs') chips.push('<span class="ss-chip">Inside documents</span>');
    return chips.length ? chips.join('') : '<span class="ss-empty">All tenders</span>';
  }

  function renderSearches() {
    var listEl = document.getElementById('search-list');
    if (!listEl) return;
    var empty = document.getElementById('search-empty');
    var items = store.list();
    var canNotify = !!window.tenderNotify;
    if (empty) empty.style.display = items.length ? 'none' : 'block';
    listEl.innerHTML = items.map(function (s) {
      return '<li class="card savedsearch" data-filters="' + esc(s.filters) + '">'
        + '<a class="card-title" href="/browse?' + esc(s.filters) + '">' + esc(s.label) + '</a>'
        + '<div class="card-meta">'
        + '<div class="ss-filters">' + filterChips(s.filters) + '</div>'
        + (canNotify
            ? '<button class="notifybtn' + (s.notify ? ' is-on' : '') + '" data-act="notify"'
              + ' aria-pressed="' + (s.notify ? 'true' : 'false') + '">'
              + (s.notify ? 'Notifying' : 'Notify me') + '</button>'
            : '')
        + '<button class="linkbtn" data-act="rename">Rename</button>'
        + '<button class="rmbmk" data-act="remove">Remove</button>'
        + '</div></li>';
    }).join('');

    if (canNotify) {
      /* The server is the truth about what will actually be delivered — a
         subscription this browser lost, or one deleted as dead, means "Notify
         me" is off however hopeful localStorage is. */
      window.tenderNotify.state().then(function (state) {
        if (!state) return;
        var on = {};
        (state.watches || []).forEach(function (w) { on[w.filters] = true; });
        Array.prototype.forEach.call(listEl.querySelectorAll('.savedsearch'), function (li) {
          var f = li.getAttribute('data-filters');
          window.tenderNotify.canonical(f).then(function (canon) {
            var live = !!on[canon];
            store.setNotify(f, live);
            var btn = li.querySelector('[data-act="notify"]');
            if (!btn) return;
            btn.classList.toggle('is-on', live);
            btn.setAttribute('aria-pressed', live ? 'true' : 'false');
            btn.textContent = live ? 'Notifying' : 'Notify me';
          }).catch(function () {});
        });
      }).catch(function () {});
    }

    listEl.addEventListener('click', function (e) {
      var btn = e.target.closest('button[data-act]');
      if (!btn) return;
      var li = btn.closest('li');
      var filters = li.getAttribute('data-filters');
      var act = btn.getAttribute('data-act');
      if (act === 'remove') {
        store.remove(filters);
        if (window.tenderNotify) window.tenderNotify.unwatch(filters).catch(function () {});
        li.remove();
        if (empty && !store.list().length) empty.style.display = 'block';
      } else if (act === 'rename') {
        var name = window.prompt('Name this search',
                                 li.querySelector('.card-title').textContent);
        if (!name) return;
        store.rename(filters, name);
        li.querySelector('.card-title').textContent = name;
        if (window.tenderNotify) window.tenderNotify.renameByFilters(filters, name);
      } else if (act === 'notify') {
        var on = btn.getAttribute('aria-pressed') === 'true';
        var N = window.tenderNotify;
        btn.disabled = true;
        var done = function (state, message, kind) {
          btn.disabled = false;
          btn.classList.toggle('is-on', state);
          btn.setAttribute('aria-pressed', state ? 'true' : 'false');
          btn.textContent = state ? 'Notifying' : 'Notify me';
          store.setNotify(filters, state);
          if (message) toast(message, kind);
        };
        if (on) {
          N.unwatch(filters).then(function () { done(false, 'Notifications off for this search.'); })
            .catch(function (err) { done(true, err.message, 'warn'); });
        } else {
          N.watch(filters, li.querySelector('.card-title').textContent)
            .then(function () { done(true, 'Notifications on for this search.'); })
            .catch(function (err) { done(false, err.message, 'warn'); });
        }
      }
    });
  }

  function init() { paintWatchButton(); renderSearches(); }
  /* Deferred scripts run with readyState already "interactive", so a
     `=== 'loading'` check would call init() immediately — before the other
     deferred scripts on the page had executed, and this one depends on two of
     them (multiselect.js publishes window.tenderWatch, notify.js publishes
     window.tenderNotify). DOMContentLoaded is the first moment every deferred
     script has run, and it has not fired until readyState is 'complete'. */
  if (document.readyState === 'complete') { init(); }
  else { document.addEventListener('DOMContentLoaded', init); }
})();
