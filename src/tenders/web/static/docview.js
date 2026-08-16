/* In-page document viewer.

   Two things this replaces, both of which failed on real phones:
   - navigating to a /view page, which loses the tender you were reading;
   - <iframe src="…pdf">, which mobile Safari and Chrome routinely refuse to
     render, handing the file to the download manager instead. Nothing on the
     server can override that, so PDFs are decoded here and painted onto
     canvases with a self-hosted PDF.js.

   Progressive enhancement: every trigger is a real <a href="/view/<id>">, so
   with JS off (or if PDF.js fails to load) the standalone page still works. */
(function () {
  var PDFJS = '/static/pdf.min.js';
  var WORKER = '/static/pdf.worker.min.js';

  /* The viewer's own controls, in the same pixel language as the rest of the
     site. These are duplicated from templates/_icons.html rather than shared,
     because this toolbar is assembled as a string in JS and Jinja never sees
     it; keep the two in step by hand if either changes. Until now this was the
     one place the site broke character — the pager used the literal characters
     "‹" and "›" and the close and download buttons carried stroked
     Feather-style glyphs, so the moment a reader actually opened a document
     the interface stopped looking like the archive it belongs to. */
  var PX = {
    prev: '<svg class="ic ic-px" viewBox="0 0 8 8" fill="currentColor" shape-rendering="crispEdges" aria-hidden="true"><path d="M5 0h1v1H5zM4 1h2v1H4zM3 2h3v1H3zM2 3h4v1H2zM2 4h4v1H2zM3 5h3v1H3zM4 6h2v1H4zM5 7h1v1H5z"/></svg>',
    next: '<svg class="ic ic-px" viewBox="0 0 8 8" fill="currentColor" shape-rendering="crispEdges" aria-hidden="true"><path d="M2 0h1v1H2zM2 1h2v1H2zM2 2h3v1H2zM2 3h4v1H2zM2 4h4v1H2zM2 5h3v1H2zM2 6h2v1H2zM2 7h1v1H2z"/></svg>',
    close: '<svg class="ic ic-px" viewBox="0 0 8 8" fill="currentColor" shape-rendering="crispEdges" aria-hidden="true"><path d="M0 0h2v1H0zM6 0h2v1H6zM1 1h2v1H1zM5 1h2v1H5zM2 2h2v1H2zM4 2h2v1H4zM3 3h2v1H3zM3 4h2v1H3zM2 5h2v1H2zM4 5h2v1H4zM1 6h2v1H1zM5 6h2v1H5zM0 7h2v1H0zM6 7h2v1H6z"/></svg>',
    /* An arrow-and-tray, not the Noun Project page-with-fold glyph used
       elsewhere (see _icons.html's `download` vs `tray`). That one is itself a
       rectangular page silhouette, so inside this toolbar's bordered .dv-btn
       square it read as a second, smaller button nested in the first — unlike
       `close` beside it, which is a bare X with no outline of its own. */
    download: '<svg class="ic ic-px" viewBox="0 0 8 8" fill="currentColor" shape-rendering="crispEdges" aria-hidden="true"><path d="M3 0h2v1H3zM3 1h2v1H3zM3 2h2v1H3zM1 3h6v1H1zM2 4h4v1H2zM3 5h2v1H3zM0 7h8v1H0z"/></svg>'
  };
  /* Beyond 2x the extra pixels are invisible and the memory cost is not. */
  var MAX_DPR = 2;
  var MAX_CSS_WIDTH = 1400;
  /* Canvases are the expensive part; keep only a window of them alive. */
  var KEEP_PAGES = 10;

  var pdfjsReady = null;

  function loadPdfjs() {
    /* 377 KB — only fetched once someone actually opens a PDF. */
    if (pdfjsReady) return pdfjsReady;
    pdfjsReady = new Promise(function (resolve, reject) {
      if (window.pdfjsLib) return resolve(window.pdfjsLib);
      var s = document.createElement('script');
      s.src = PDFJS;
      s.onload = function () {
        if (!window.pdfjsLib) return reject(new Error('pdfjsLib missing'));
        window.pdfjsLib.GlobalWorkerOptions.workerSrc = WORKER;
        resolve(window.pdfjsLib);
      };
      s.onerror = function () { reject(new Error('pdf.js failed to load')); };
      document.head.appendChild(s);
    });
    return pdfjsReady;
  }

  /* ---------- spreadsheet zoom ----------

     A BoQ is ~60 columns and about 7,000px wide once the empty ones are
     dropped, so on a phone it is a thing you pan around one screenful at a
     time and there is no way to see its shape — which column is the priced
     one, how many line items there are, where the totals sit. Zoom answers
     that, and it deliberately goes far below readable: at 20% the text is a
     grey texture and the *structure* is the whole point.

     Implemented as a CSS transform rather than by shrinking the font, because
     a font-size change re-runs table layout — column widths redistribute, rows
     re-wrap, and the sheet you were looking at is not the sheet you get back.
     A transform scales the finished layout, so zooming is purely optical and
     the sheet keeps its proportions. The cost is that a transform does not
     affect layout size, so the scroller would not know the content had shrunk;
     .dv-zoomer is sized explicitly to the scaled dimensions to fix that. */

  /* 4% is not a typo. A de-sparsified BoQ is still ~7,000px wide and a phone
     viewport is ~372px, so seeing the whole sheet at once *requires* about 5%
     — a floor of 20% would have made "fit width" unable to fit, which is the
     one thing it is for. At this scale the text is deliberately a grey
     texture; the shape of the sheet is the information. */
  var ZOOM_MIN = 0.04, ZOOM_MAX = 2, ZOOM_STEP = 0.8;  /* step is multiplicative */

  function applyZoom(pane, z) {
    var zoomer = pane.querySelector('.dv-zoomer');
    var table = pane.querySelector('.dv-table');
    var level = pane.querySelector('.dv-zlevel');
    if (!zoomer || !table) return;
    z = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z));
    pane.dataset.zoom = z;
    /* Measure unscaled, or each zoom would compound the last one's scale. */
    zoomer.style.transform = 'none';
    zoomer.style.width = '';
    zoomer.style.height = '';
    var w = table.offsetWidth, h = table.offsetHeight;
    zoomer.style.transform = 'scale(' + z + ')';
    zoomer.style.transformOrigin = '0 0';
    zoomer.style.width = (w * z) + 'px';
    zoomer.style.height = (h * z) + 'px';
    if (level) level.textContent = Math.round(z * 100) + '%';
  }

  function fitWidth(pane) {
    var wrap = pane.querySelector('.dv-tablewrap');
    var table = pane.querySelector('.dv-table');
    if (!wrap || !table) return;
    var zoomer = pane.querySelector('.dv-zoomer');
    zoomer.style.transform = 'none'; zoomer.style.width = ''; zoomer.style.height = '';
    var natural = table.offsetWidth;
    /* -2px so the last column's border is inside the frame rather than
       triggering a 1px horizontal scrollbar that makes "fit" look broken. */
    applyZoom(pane, natural ? (wrap.clientWidth - 2) / natural : 1);
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('.dv-zbtn') : null;
    if (!btn) return;
    var pane = btn.closest('.dv-sheetpane');
    if (!pane) return;
    var how = btn.dataset.zoom;
    if (how === 'fit') return fitWidth(pane);
    var cur = parseFloat(pane.dataset.zoom || '1') || 1;
    applyZoom(pane, how === 'out' ? cur * ZOOM_STEP : cur / ZOOM_STEP);
  });

  /* ---------- spreadsheet tabs ---------- */

  /* Delegated from the document so re-injecting the modal body can never stack
     duplicate handlers. */
  document.addEventListener('click', function (e) {
    var tab = e.target.closest ? e.target.closest('.dv-tab') : null;
    if (!tab) return;
    var sheet = tab.closest('.dv-sheet');
    sheet.querySelectorAll('.dv-tab').forEach(function (t) {
      var on = t === tab;
      t.classList.toggle('on', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    sheet.querySelectorAll('.dv-sheetpane').forEach(function (p) {
      p.classList.toggle('on', p.dataset.pane === tab.dataset.sheet);
    });
  });

  /* ---------- PDF ---------- */

  function wirePdf(root, scrollHost, indicator) {
    var host = root.querySelector('.dv-pdf');
    if (!host) return;
    var pagesEl = host.querySelector('.dv-pages');
    var statusEl = host.querySelector('.dv-status');
    var url = host.dataset.pdf;
    var doc = null, live = [], current = 1, visible = {};

    function fail(msg) {
      statusEl.innerHTML = msg + ' <a href="' + url.split('?')[0] + '">Download the file</a> instead.';
      statusEl.hidden = false;
    }

    function setCurrent(n) {
      if (n === current) return;
      current = n;
      if (indicator) indicator.update(current, doc ? doc.numPages : 0);
    }

    function recount() {
      var seen = Object.keys(visible).map(Number).sort(function (a, b) { return a - b; });
      if (seen.length) setCurrent(seen[0]);
    }

    function blank(entry) {
      /* Freeze the measured height first, or dropping the canvas would collapse
         the page and yank the scroll position out from under the reader. */
      entry.el.style.height = entry.el.offsetHeight + 'px';
      entry.el.innerHTML = '';
      entry.el.dataset.state = '';
      live.splice(live.indexOf(entry), 1);
    }

    /* Drop canvases far from the reader so a 300-page tender doesn't grow until
       the tab is killed. Anything on screen is off limits — an IntersectionObserver
       won't fire again for an element that never stopped intersecting, so a
       blanked visible page would stay blank. */
    function trim() {
      while (live.length > KEEP_PAGES) {
        var far = null;
        live.forEach(function (p) {
          if (visible[p.n] || Math.abs(p.n - current) <= 2) return;
          if (!far || Math.abs(p.n - current) > Math.abs(far.n - current)) far = p;
        });
        if (!far) break;
        blank(far);
      }
    }

    /* Rotating a phone changes the CSS width under every canvas; re-render what
       is on screen so it is sharp again rather than resampled. */
    var lastWidth = 0, resizeTimer = null;
    function onResize() {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        if (!doc || !document.contains(pagesEl)) return;
        var w = pagesEl.clientWidth;
        if (!w || Math.abs(w - lastWidth) < 40) return;
        lastWidth = w;
        live.slice().forEach(blank);
        Object.keys(visible).forEach(function (n) {
          var el = pagesEl.querySelector('.dv-page[data-page="' + n + '"]');
          if (el) render(el);
        });
      }, 250);
    }
    window.addEventListener('resize', onResize);

    function render(el) {
      if (el.dataset.state) return;
      el.dataset.state = 'busy';
      var n = +el.dataset.page;
      doc.getPage(n).then(function (page) {
        var dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);
        var base = page.getViewport({ scale: 1 });
        var cssW = Math.min(Math.max(pagesEl.clientWidth, 240), MAX_CSS_WIDTH);
        var vp = page.getViewport({ scale: (cssW / base.width) * dpr });
        var canvas = document.createElement('canvas');
        canvas.width = Math.floor(vp.width);
        canvas.height = Math.floor(vp.height);
        canvas.style.width = '100%';
        canvas.style.aspectRatio = vp.width + ' / ' + vp.height;
        canvas.setAttribute('role', 'img');
        canvas.setAttribute('aria-label', 'Page ' + n);
        el.style.height = '';
        el.innerHTML = '';
        el.appendChild(canvas);
        live.push({ n: n, el: el });
        return page.render({
          canvasContext: canvas.getContext('2d', { alpha: false }), viewport: vp
        }).promise.then(function () {
          el.dataset.state = 'done';
          trim();
        });
      }).catch(function () { el.dataset.state = ''; });
    }

    loadPdfjs().then(function (lib) {
      return lib.getDocument({ url: url, disableAutoFetch: false }).promise;
    }).then(function (d) {
      doc = d;
      host._doc = d;
      lastWidth = pagesEl.clientWidth;
      statusEl.hidden = true;
      return doc.getPage(1).then(function (p) {
        var vp = p.getViewport({ scale: 1 });
        var ratio = vp.height / vp.width;
        for (var n = 1; n <= doc.numPages; n++) {
          var el = document.createElement('div');
          el.className = 'dv-page';
          el.dataset.page = n;
          /* Placeholders assume page 1's shape; each corrects itself on render. */
          el.style.height = Math.round(pagesEl.clientWidth * ratio) + 'px';
          pagesEl.appendChild(el);
        }
        if (indicator) indicator.update(1, doc.numPages);
        var io = new IntersectionObserver(function (entries) {
          entries.forEach(function (en) {
            var n = +en.target.dataset.page;
            if (en.isIntersecting) { visible[n] = 1; render(en.target); }
            else { delete visible[n]; }
          });
          recount();
        }, { root: scrollHost || null, rootMargin: '400px 0px' });
        pagesEl.querySelectorAll('.dv-page').forEach(function (el) { io.observe(el); });
        if (indicator) {
          indicator.go = function (delta) {
            var n = Math.min(Math.max(current + delta, 1), doc.numPages);
            var el = pagesEl.querySelector('.dv-page[data-page="' + n + '"]');
            if (el) { el.scrollIntoView({ block: 'start', behavior: 'smooth' }); setCurrent(n); }
          };
          indicator.enable();
        }
      });
    }).catch(function () {
      fail('This PDF could not be displayed in your browser.');
    });
  }

  /* A PDF holds a worker and its font/image caches; dropping the markup alone
     would leak both for as long as the tab lives. */
  function release(root) {
    root.querySelectorAll('.dv-pdf').forEach(function (host) {
      if (host._doc) { try { host._doc.destroy(); } catch (e) { /* already gone */ } }
      host._doc = null;
    });
  }

  /* ---------- standalone /view page ---------- */

  var page = document.querySelector('.viewer-body');
  if (page) {
    var bar = document.createElement('div');
    bar.className = 'dv-pagebar dv-pagebar-solo';
    bar.hidden = true;
    var pageInd = pageIndicator(bar);
    page.parentNode.insertBefore(bar, page);
    wirePdf(page, null, pageInd);
  }

  /* A shared prev / "3 / 48" / next control, driven by wirePdf. */
  function pageIndicator(el) {
    el.innerHTML =
      '<button type="button" class="dv-nav" data-step="-1" aria-label="Previous page">' + PX.prev + '</button>' +
      '<span class="dv-count" aria-live="polite">—</span>' +
      '<button type="button" class="dv-nav" data-step="1" aria-label="Next page">' + PX.next + '</button>';
    var count = el.querySelector('.dv-count');
    var ind = {
      el: el,
      go: function () {},
      enable: function () { el.hidden = false; },
      reset: function () { el.hidden = true; count.textContent = '—'; },
      update: function (n, total) { count.textContent = n + ' / ' + total; }
    };
    el.addEventListener('click', function (e) {
      var b = e.target.closest ? e.target.closest('.dv-nav') : null;
      if (b) ind.go(+b.dataset.step);
    });
    return ind;
  }

  /* ---------- modal ---------- */

  var modal = null, dialog, titleEl, bodyEl, dlEl, modalInd, lastFocus = null, pushed = false;

  function build() {
    modal = document.createElement('div');
    modal.className = 'dv-modal';
    modal.hidden = true;
    modal.innerHTML =
      '<div class="dv-backdrop" data-close></div>' +
      '<div class="dv-dialog" role="dialog" aria-modal="true" aria-labelledby="dv-title">' +
        '<header class="dv-head">' +
          '<h2 class="dv-name" id="dv-title"></h2>' +
          '<div class="dv-tools">' +
            '<div class="dv-pagebar" hidden></div>' +
            '<a class="dv-btn dv-dl" href="#" download title="Download" aria-label="Download">' +
              PX.download + '</a>' +
            '<button type="button" class="dv-btn dv-close" data-close title="Close" aria-label="Close">' +
              PX.close + '</button>' +
          '</div>' +
        '</header>' +
        '<div class="dv-body" tabindex="-1"></div>' +
      '</div>';
    document.body.appendChild(modal);
    dialog = modal.querySelector('.dv-dialog');
    titleEl = modal.querySelector('.dv-name');
    bodyEl = modal.querySelector('.dv-body');
    dlEl = modal.querySelector('.dv-dl');
    modalInd = pageIndicator(modal.querySelector('.dv-pagebar'));

    modal.addEventListener('click', function (e) {
      if (e.target.closest && e.target.closest('[data-close]')) close();
    });
    modal.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { e.preventDefault(); close(); }
      else if (e.key === 'Tab') trapFocus(e);
    });
  }

  function focusable() {
    return Array.prototype.filter.call(
      dialog.querySelectorAll('a[href],button:not([disabled]),[tabindex="0"]'),
      function (el) { return el.offsetParent !== null; });
  }

  function trapFocus(e) {
    var items = focusable();
    if (!items.length) return;
    var first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  function open(link) {
    if (!modal) build();
    lastFocus = document.activeElement;
    titleEl.textContent = link.dataset.name || 'Document';
    dlEl.href = link.dataset.download || '#';
    modalInd.reset();
    bodyEl.innerHTML = '<p class="dv-status">Loading…</p>';
    bodyEl.scrollTop = 0;
    modal.hidden = false;
    document.documentElement.classList.add('dv-open');
    bodyEl.focus();
    /* Warm the decoder while the fragment is still in flight. */
    if (link.dataset.kind === 'pdf') loadPdfjs().catch(function () {});

    fetch(link.href + (link.href.indexOf('?') < 0 ? '?' : '&') + 'partial=1',
          { headers: { 'X-Requested-With': 'fetch' } })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then(function (html) {
        bodyEl.innerHTML = html;
        wirePdf(bodyEl, bodyEl, modalInd);
      })
      .catch(function () {
        bodyEl.innerHTML = '<p class="empty">This preview could not be loaded. ' +
          '<a href="' + link.href + '">Open it on its own page</a>.</p>';
      });

    /* A phone's back gesture should dismiss the overlay, not leave the tender. */
    try { history.pushState({ dv: 1 }, ''); pushed = true; } catch (e) { pushed = false; }
  }

  function close() {
    if (!modal || modal.hidden) return;
    modal.hidden = true;
    release(bodyEl);
    bodyEl.innerHTML = '';
    document.documentElement.classList.remove('dv-open');
    if (lastFocus && lastFocus.focus) lastFocus.focus();
    lastFocus = null;
    if (pushed) { pushed = false; history.back(); }
  }

  /* Backstop for Esc: the dialog's own handler only fires while focus is
     inside it, and a stray click on the page can move focus out. */
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && modal && !modal.hidden) close();
  });

  window.addEventListener('popstate', function () {
    if (modal && !modal.hidden) { pushed = false; close(); }
  });

  document.addEventListener('click', function (e) {
    if (e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var link = e.target.closest ? e.target.closest('a[data-view]') : null;
    if (!link) return;
    e.preventDefault();
    open(link);
  });
})();
