/* Infinite scroll + scroll/state persistence for the search results.
   - Loads more tender cards as the sentinel nears the viewport.
   - Caches the loaded HTML + scroll position in sessionStorage keyed by the
     query, so returning to the page (e.g. after opening a tender) restores
     exactly where the user was. */
(function () {
  var list = document.getElementById('results-list');
  if (!list) return;
  var sentinel = document.getElementById('sentinel');
  var loadmore = document.getElementById('loadmore');
  var endmsg = document.getElementById('scroll-end');

  var qs = list.dataset.qs || '';
  var next = parseInt(list.dataset.next || '2', 10);
  var size = parseInt(list.dataset.size || '25', 10);
  var total = parseInt(list.dataset.total || '0', 10);
  var loading = false, done = false;
  var KEY = 'browse:' + location.search;

  function countCards() { return list.querySelectorAll('.card').length; }
  function finish() {
    done = true;
    if (loadmore) loadmore.classList.add('hidden');
    if (endmsg && countCards() > 0) endmsg.classList.remove('hidden');
  }

  function save() {
    try {
      sessionStorage.setItem(KEY, JSON.stringify({
        html: list.innerHTML, next: next, done: done,
        y: window.scrollY, t: Date.now()
      }));
    } catch (e) { /* quota — ignore */ }
  }

  // Restore prior session for this exact query (back-navigation).
  function restore() {
    var raw;
    try { raw = sessionStorage.getItem(KEY); } catch (e) { return false; }
    if (!raw) return false;
    try {
      var s = JSON.parse(raw);
      if (!s || !s.html) return false;
      list.innerHTML = s.html;
      next = s.next || next;
      done = !!s.done;
      if (done) finish();
      requestAnimationFrame(function () { window.scrollTo(0, s.y || 0); });
      return true;
    } catch (e) { return false; }
  }

  function load() {
    if (loading || done) return;
    if (countCards() >= total) { finish(); return; }
    loading = true;
    if (loadmore) loadmore.classList.remove('hidden');
    var url = '/browse?' + qs + '&page=' + next + '&partial=1';
    fetch(url, { headers: { 'X-Requested-With': 'fetch' } })
      .then(function (r) { return r.ok ? r.text() : ''; })
      .then(function (html) {
        var tmp = document.createElement('tbody');
        tmp.innerHTML = html.trim();
        var added = tmp.querySelectorAll('.card').length;
        while (tmp.firstChild) list.appendChild(tmp.firstChild);
        next += 1;
        loading = false;
        if (loadmore) loadmore.classList.add('hidden');
        if (added < size || countCards() >= total) finish();
        save();
      })
      .catch(function () { loading = false; if (loadmore) loadmore.classList.add('hidden'); });
  }

  restore();

  if ('IntersectionObserver' in window && sentinel) {
    var io = new IntersectionObserver(function (entries) {
      if (entries[0].isIntersecting) load();
    }, { rootMargin: '600px 0px' });
    io.observe(sentinel);
  }

  // Persist scroll position (throttled) and on leaving the page.
  var t;
  window.addEventListener('scroll', function () {
    clearTimeout(t);
    t = setTimeout(save, 250);
  }, { passive: true });
  window.addEventListener('pagehide', save);
})();
