/* Bookmarks (stored locally) + share helpers.
   Works on the tender page (toggle + copy link) and the /bookmarks page (list). */
(function () {
  var KEY = 'tn_bookmarks';
  function load() {
    try { return JSON.parse(localStorage.getItem(KEY) || '[]'); } catch (e) { return []; }
  }
  function save(list) {
    try { localStorage.setItem(KEY, JSON.stringify(list)); } catch (e) {}
  }
  function has(id) { return load().some(function (b) { return b.id === id; }); }

  // The site's date rules, restated for the one page rendered in the browser.
  // Bookmarks are stored on the device and can sit there for months, so
  // "Closes in 5 days" has to be computed when the list is opened, not when the
  // tender was saved. Day counts are taken between IST calendar dates so a
  // deadline tonight and one tomorrow morning are not both "in 0 days"; the
  // server does the same (see web/dates.py).
  var MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
    'August', 'September', 'October', 'November', 'December'];
  var IST_OFFSET_MS = 5.5 * 3600 * 1000;
  function istParts(stamp) {
    var ms = Date.parse(/[Z+]|-\d\d:\d\d$/.test(stamp) ? stamp : stamp + '+05:30');
    if (isNaN(ms)) return null;
    var d = new Date(ms + IST_OFFSET_MS);   // shifted so UTC getters read IST
    return { ms: ms, d: d };
  }
  function fmtDate(p) {
    return ('0' + p.d.getUTCDate()).slice(-2) + '-' + MONTHS[p.d.getUTCMonth()]
      + '-' + p.d.getUTCFullYear();
  }
  function elapsed(days) {
    if (days < 60) return days + ' days';
    if (days < 550) return Math.round(days / 30.44) + ' months';
    var y = Math.round(days / 365.25);
    return y + (y === 1 ? ' year' : ' years');
  }
  function closingLine(stamp) {
    if (!stamp) return '';
    var p = istParts(stamp);
    if (!p) return '';
    var now = new Date(Date.now() + IST_OFFSET_MS);
    var day = Date.UTC(p.d.getUTCFullYear(), p.d.getUTCMonth(), p.d.getUTCDate());
    var today = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
    var days = Math.round((day - today) / 86400000);
    var rel;
    if (p.ms < Date.now()) {
      rel = days === 0 ? 'Closed today' : days === -1 ? 'Closed yesterday'
        : 'Closed ' + elapsed(-days) + ' ago';
    } else {
      rel = days === 0 ? 'Closes today' : days === 1 ? 'Closes tomorrow'
        : 'Closes in ' + elapsed(days);
    }
    return rel + ' · ' + fmtDate(p);
  }
  function add(b) { var l = load(); if (!has(b.id)) { l.unshift(b); save(l); } }
  function remove(id) { save(load().filter(function (b) { return b.id !== id; })); }

  // ---- Share row (tender or document): bookmark toggle + copy link ----
  var row = document.querySelector('.sharerow');
  if (row) {
    var id = row.dataset.id, name = row.dataset.name, url = row.dataset.url,
        closing = row.dataset.closing || '';
    var btn = document.getElementById('bookmark-btn');
    function paint() {
      if (!btn) return;
      var on = has(id);
      btn.classList.toggle('saved', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      btn.title = on ? 'Bookmarked' : 'Bookmark';
    }
    if (btn) {
      paint();
      btn.addEventListener('click', function () {
        if (has(id)) remove(id);
        else add({ id: id, name: name, url: url, closing: closing });
        paint();
      });
    }
    var copy = document.getElementById('sh-copy');
    if (copy) copy.addEventListener('click', function () {
      var link = copy.dataset.copy || (location.origin + url);
      navigator.clipboard && navigator.clipboard.writeText(link).then(function () {
        copy.classList.add('copied');
        setTimeout(function () { copy.classList.remove('copied'); }, 1400);
      });
    });
  }

  // ---- Bookmarks page: render the saved list ----
  var listEl = document.getElementById('bookmark-list');
  if (listEl) {
    var items = load();
    var empty = document.getElementById('bookmark-empty');
    if (!items.length) { if (empty) empty.style.display = 'block'; return; }
    if (empty) empty.style.display = 'none';
    // The alert toggle is rendered per row but is a separate opt-in from the
    // bookmark itself: the bookmark is already in this browser, and pressing
    // this is what tells the server one tender id. It is only offered when the
    // mirror actually has push configured (notify.js is not loaded otherwise).
    var canAlert = !!window.tenderNotify;
    listEl.innerHTML = items.map(function (b) {
      var nm = (b.name || b.id).replace(/[<>&]/g, '');
      // Absent on bookmarks saved before closing dates were stored; those
      // simply show no deadline rather than a wrong one.
      var when = closingLine(b.closing);
      return '<li class="card"><a class="card-title" href="' + b.url + '">' + nm + '</a>'
        + '<div class="card-meta">'
        + (when ? '<span>' + when + '</span>' : '')
        + '<span class="tid">' + b.id + '</span>'
        + (canAlert ? '<button class="alertbtn mini" data-alert-tender="' + b.id
            + '" aria-pressed="false" title="Alert me if this tender changes">'
            + '<span>Alert me</span></button>' : '')
        + '<button class="rmbmk" data-id="' + b.id + '">Remove</button></div></li>';
    }).join('');
    if (canAlert) window.tenderNotify.bindAlertToggles(listEl);
    listEl.addEventListener('click', function (e) {
      var t = e.target;
      if (t.classList.contains('rmbmk')) {
        remove(t.dataset.id);
        var li = t.closest('li'); if (li) li.remove();
        if (!load().length && empty) empty.style.display = 'block';
      }
    });
  }
})();
