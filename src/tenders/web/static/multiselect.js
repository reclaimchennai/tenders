/* Tag-chip multi-select for the advanced-search filters. Vanilla, no deps.

   The <select multiple> in the markup stays the field: this script hides it and
   drives it, so the form submits ?org=A&org=B in both directions and a browser
   that never runs this file still has a working (if plain) control. Nothing here
   creates a hidden input, because two sources of truth for one filter is how a
   chip and a query end up disagreeing.

   Two shapes, one control. On a pointer device it is an inline dropdown under
   the field. On a phone it is a bottom sheet, because the inline dropdown could
   not be made to work there: 147 departments do not fit under a field that is
   itself two thirds of the way down a long form, so the list opened below the
   fold and reaching it meant scrolling the page — which dismissed it. A sheet is
   anchored to the bottom of the viewport instead, so the list and its Done
   button are on screen by construction and the page never has to move.

   Also carries the watch-these-filters button, whose contract is documented in
   browse.html beside the markup it belongs to. */
(function () {
  'use strict';

  /* Sheet or dropdown. Width alone is the wrong test — a narrow desktop window
     is still a mouse — and touch alone is the wrong test too, since a touch
     laptop has the room for a dropdown. Both matter, so both are asked. */
  var SHEET_MQ = window.matchMedia('(max-width: 680px), (pointer: coarse) and (max-width: 1024px)');

  /* How far a finger may travel and still have meant "tap". Below this a touch
     is a press; above it the user is scrolling and nothing must be selected.
     10px is about the wobble of a stationary thumb on a 3x screen. */
  var SLOP = 10;

  /* Every built control registers its close() here. Two menus open at once is
     not a state the user ever asked for — on a 390px screen the second one is
     drawn straight over the first — so opening one shuts the rest. */
  var openers = [];
  function closeAllExcept(keep) {
    openers.forEach(function (o) { if (o !== keep) o.close(); });
  }

  /* One dim layer for every sheet. Only one sheet can be open at a time, so a
     single node is enough; it is kept in the DOM and toggled rather than
     created and destroyed, so the fade has something to transition. */
  var backdrop = null;
  function backdropOn(on) {
    if (!backdrop) {
      if (!on) return;
      backdrop = document.createElement('div');
      backdrop.className = 'ms-backdrop';
      // See the popup's own mousedown handler: a tap that opens a sheet is
      // followed by a compat mousedown wherever the new layout put that point,
      // and letting it land moves focus to <body>.
      backdrop.addEventListener('mousedown', function (e) { e.preventDefault(); });
      document.body.appendChild(backdrop);
      void backdrop.offsetWidth;   // land the initial style before .show
    }
    backdrop.classList.toggle('show', !!on);
  }

  /* Dismissal is delegated to one document-level pointerdown rather than each
     control listening for itself, and pointerdown is the whole point of the
     fix: the previous version listened for `mousedown`, which a phone only
     synthesises for elements the browser considers clickable. Tapping the page
     background — the obvious way to dismiss anything on touch — produced no
     mousedown at all, so the menu could not be closed by tapping outside it,
     and with 147 departments in the list it covered the rest of the form.
     pointerdown fires for mouse, touch and pen alike.

     pointerdown and not click, because a picked option calls preventDefault to
     keep focus in the input; a click-based dismissal would never see the tap. */
  var DISMISS = window.PointerEvent ? ['pointerdown'] : ['mousedown', 'touchstart'];
  DISMISS.forEach(function (type) {
    document.addEventListener(type, function (e) {
      openers.forEach(function (o) { if (!o.box.contains(e.target)) o.close(); });
    }, true);
  });

  /* Fire `run` only for a press and release that stayed in one place.

     This is the fix for the report that scrolling the list "selects everything
     I scroll past". A touch that drags across a row still ends as a click on
     that row, so neither `click` nor a bare `pointerup` can tell a pick from a
     pan — only the distance travelled can. pointercancel is watched as well
     because it is the browser saying, at the earliest possible moment, that it
     has taken the gesture over as a scroll. */
  function tappable(el, run) {
    var id = null, sx = 0, sy = 0, live = false;
    el.addEventListener('pointerdown', function (e) {
      if (e.button > 0) { live = false; return; }
      id = e.pointerId; sx = e.clientX; sy = e.clientY; live = true;
    });
    el.addEventListener('pointermove', function (e) {
      if (!live || e.pointerId !== id) return;
      if (Math.abs(e.clientX - sx) > SLOP || Math.abs(e.clientY - sy) > SLOP) live = false;
    });
    el.addEventListener('pointercancel', function () { live = false; });
    el.addEventListener('pointerup', function (e) {
      var ok = live && e.pointerId === id;
      live = false;
      if (ok) run(e);
    });
  }

  // Long lists (147 departments) are filtered on every keystroke, so the match
  // runs against a lowercased copy taken once rather than per comparison.
  function build(box) {
    var sel = box.querySelector('select[multiple]');
    if (!sel) return;
    var hint = box.querySelector('.ms-hint');
    if (hint) hint.remove();
    sel.hidden = true;
    sel.setAttribute('aria-hidden', 'true');
    sel.tabIndex = -1;

    var id = sel.id || ('ms' + Math.random().toString(36).slice(2));
    var listId = id + '-list';
    var label = box.querySelector('label');
    var labelText = label ? label.textContent.trim() : 'Filter';
    var placeholder = box.dataset.placeholder || 'Any';

    var field = document.createElement('div');
    field.className = 'ms-field';

    /* The closed field on a phone is a button, not the text input.
       Swiping the page over a text input focuses it and raises the keyboard —
       reported as "scrolling past a field opens its menu" — and a button cannot
       do that. The input still exists; in sheet mode it lives in the sheet's
       own header, where it is reached deliberately. */
    var trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'ms-trigger';
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.setAttribute('aria-controls', listId);
    var trigText = document.createElement('span');
    trigText.className = 'ms-triglabel';
    trigger.appendChild(trigText);

    var input = document.createElement('input');
    input.type = 'text';
    input.className = 'ms-input';
    input.autocomplete = 'off';
    input.setAttribute('autocapitalize', 'none');
    input.setAttribute('autocorrect', 'off');
    // "Done" rather than "Go": Enter here never submits the search — see keydown.
    input.setAttribute('enterkeyhint', 'done');
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('aria-controls', listId);
    input.setAttribute('aria-autocomplete', 'list');
    input.id = id + '-input';
    if (label) label.htmlFor = input.id;

    var menu = document.createElement('ul');
    menu.className = 'ms-menu';
    menu.id = listId;
    menu.setAttribute('role', 'listbox');
    menu.setAttribute('aria-multiselectable', 'true');

    var chips = document.createElement('div');
    chips.className = 'ms-chips';

    var items = [];
    Array.prototype.forEach.call(sel.options, function (opt, i) {
      var li = document.createElement('li');
      li.className = 'ms-opt';
      li.setAttribute('role', 'option');
      li.id = id + '-o' + i;
      li.textContent = opt.textContent;
      li.dataset.value = opt.value;
      menu.appendChild(li);
      items.push({ li: li, opt: opt, hay: opt.textContent.toLowerCase(), shown: true });
    });

    var empty = document.createElement('li');
    empty.className = 'ms-none';
    empty.textContent = 'No match';
    empty.hidden = true;
    menu.appendChild(empty);

    /* A visible way out. These are deliberately *multi*-selects — choosing five
       departments is a normal thing to do here — so picking an option does not
       close the menu; closing on every pick would mean reopening it four times.
       That is only defensible if dismissal is always available and obvious,
       which is what this bar is: it is always on screen at the bottom of the
       popup, it says how many are chosen, and it is reachable by thumb without
       leaving the control. Outside-tap and Escape still work; this is the
       affordance that tells a first-time user that they do.

       It is a sibling of the scrolling list and not the last row *inside* it.
       Inside, made sticky, it sat on top of whichever option happened to be at
       the bottom of the viewport and swallowed taps meant for that option —
       which on a 147-row list is one unreachable department at every scroll
       position. */
    var pop = document.createElement('div');
    pop.className = 'ms-pop';
    /* Sheet header: names the filter and holds the search box. Empty and
       display:none in dropdown mode, where the field itself is the search box. */
    var head = document.createElement('div');
    head.className = 'ms-head';
    var title = document.createElement('span');
    title.className = 'ms-title';
    title.textContent = labelText;
    head.appendChild(title);
    var done = document.createElement('div');
    done.className = 'ms-done';
    var count = document.createElement('span');
    count.className = 'ms-count';
    var doneBtn = document.createElement('button');
    doneBtn.type = 'button';
    doneBtn.className = 'ms-donebtn';
    doneBtn.textContent = 'Done';
    done.appendChild(count);
    done.appendChild(doneBtn);
    pop.appendChild(head);
    pop.appendChild(menu);
    pop.appendChild(done);
    pop.hidden = true;

    field.appendChild(trigger);
    field.appendChild(input);
    field.appendChild(pop);
    box.appendChild(field);
    box.appendChild(chips);

    var active = -1;
    var opens = 0;

    function sheet() { return SHEET_MQ.matches; }

    /* Where the search box lives depends on the shape: the field itself in a
       dropdown, the sheet's header in a sheet. One input either way — a second
       one would be a second place the typed query could disagree with itself. */
    function applyMode() {
      var s = sheet();
      box.classList.toggle('ms-sheet', s);
      trigger.setAttribute('aria-label', labelText);
      if (s) { if (input.parentNode !== head) head.appendChild(input); }
      else if (input.parentNode !== field) field.insertBefore(input, pop);
    }

    function renderChips() {
      chips.textContent = '';
      var n = 0;
      items.forEach(function (it) {
        it.li.setAttribute('aria-selected', it.opt.selected ? 'true' : 'false');
        it.li.classList.toggle('is-on', it.opt.selected);
        if (!it.opt.selected) return;
        n++;
        var chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'ms-chip';
        chip.dataset.value = it.opt.value;
        // The full name is on the element itself, because the label below is
        // allowed to run out of room: "Chennai Metropolitan Development
        // Authority" and "Chennai Metropolitan Transport Authority" are real,
        // adjacent values here, and a chip a user cannot resolve is worse than
        // one they have to hover.
        chip.title = it.li.textContent;
        chip.setAttribute('aria-label', 'Remove filter ' + it.li.textContent);
        // Wrapped in its own element so the label can be the thing that
        // truncates while the × keeps its size and position on every chip.
        var name = document.createElement('span');
        name.className = 'ms-label';
        name.textContent = it.li.textContent;
        chip.appendChild(name);
        var x = document.createElement('span');
        x.className = 'ms-x';
        x.setAttribute('aria-hidden', 'true');
        x.textContent = '×';
        chip.appendChild(x);
        chips.appendChild(chip);
      });
      // The placeholder doubles as the "nothing chosen" state, so it has to say
      // what no selection means rather than what to type.
      input.placeholder = n ? 'Add another…' : placeholder;
      trigText.textContent = n ? n + ' selected' : placeholder;
      trigger.classList.toggle('is-empty', !n);
      // Selecting does not close the menu, so the count is the only feedback a
      // thumb over the list gets that a tap landed.
      count.textContent = n ? n + ' selected' : 'Select any number';
    }

    function filter() {
      var q = input.value.trim().toLowerCase();
      var any = false;
      items.forEach(function (it) {
        it.shown = !q || it.hay.indexOf(q) !== -1;
        it.li.hidden = !it.shown;
        if (it.shown) any = true;
      });
      empty.hidden = any;
      setActive(-1);
    }

    function visible() {
      return items.filter(function (it) { return it.shown; });
    }

    function setActive(i) {
      items.forEach(function (it) { it.li.classList.remove('is-active'); });
      active = i;
      var vis = visible();
      if (i < 0 || i >= vis.length) {
        input.removeAttribute('aria-activedescendant');
        return;
      }
      var li = vis[i].li;
      li.classList.add('is-active');
      input.setAttribute('aria-activedescendant', li.id);
      var top = li.offsetTop, bottom = top + li.offsetHeight;
      if (top < menu.scrollTop) menu.scrollTop = top;
      else if (bottom > menu.scrollTop + menu.clientHeight) {
        menu.scrollTop = bottom - menu.clientHeight;
      }
    }

    function open() {
      if (!pop.hidden) return;
      closeAllExcept(entry);
      applyMode();
      pop.hidden = false;
      opens++;
      input.setAttribute('aria-expanded', 'true');
      trigger.setAttribute('aria-expanded', 'true');
      box.classList.add('is-open');
      if (!sheet()) { input.focus(); return; }
      /* The page must not move under an open sheet: scrolling it was how the
         old dropdown got dismissed halfway through choosing something. */
      document.documentElement.classList.add('ms-open');
      backdropOn(true);
      /* Keyboard on the first open only. The first time this control is opened
         the user came to type a department name and a keyboard they have to ask
         for is a keyboard in the way; every open after that they already know
         the list and came to scroll it, and a keyboard that eats half of a
         390x844 screen unasked is the complaint this whole rewrite is about.
         Tapping the search box brings it back whenever it is actually wanted. */
      if (opens === 1) input.focus();
    }

    function close() {
      if (pop.hidden) { setActive(-1); return; }
      pop.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      trigger.setAttribute('aria-expanded', 'false');
      box.classList.remove('is-open');
      document.documentElement.classList.remove('ms-open');
      backdropOn(false);
      input.blur();
      setActive(-1);
    }

    /* Scrolling means the user has stopped typing — that is the stated rule, and
       it is also the only way to get the soft keyboard back off a phone without
       a dedicated button. touchmove rather than the list's scroll event, because
       scroll also fires for the programmatic scrolling in setActive(), which
       would yank the keyboard away mid-word on a arrow-key user. */
    function dropKeyboard() {
      if (document.activeElement === input) input.blur();
    }
    pop.addEventListener('touchmove', dropKeyboard, { passive: true });
    /* Only for the dropdown shape. A sheet freezes the page, so the only scroll
       that can happen under an open one is the browser settling the layout at
       the moment it opens — which would take the keyboard away in the same
       frame it was asked for. */
    window.addEventListener('scroll', function () {
      if (!pop.hidden && !sheet()) dropKeyboard();
    }, { passive: true });

    function toggle(it) {
      it.opt.selected = !it.opt.selected;
      renderChips();
      // Cleared rather than left standing: after picking "Chennai Corporation"
      // the next thing wanted is the whole list again, not the six entries that
      // still match the word already typed.
      input.value = '';
      filter();
    }

    var entry = { box: box, close: close };
    openers.push(entry);

    /* Focus stays where the script put it, for two different reasons at once.

       With a mouse, cancelling mousedown is what keeps the caret in the search
       box so typing can carry on after a pick. (The old code cancelled
       *pointerdown* for this, which on touch also cancels the browser's own
       panning of the list — that is what made the list unscrollable and turned
       every swipe into a selection.)

       On touch, the compat mouse events a tap generates are dispatched at the
       touch point *after* the sheet has opened, so they land on whatever the new
       layout drew there — usually a non-focusable option row, which sends focus
       to <body> and takes the keyboard away in the frame it was asked for. */
    pop.addEventListener('mousedown', function (e) {
      if (e.target !== input) e.preventDefault();
    });

    tappable(done, close);
    tappable(menu, function (e) {
      var li = e.target.closest('.ms-opt');
      if (!li) return;
      var it = items.filter(function (x) { return x.li === li; })[0];
      if (it) toggle(it);
      if (e.pointerType === 'mouse') input.focus();
    });

    tappable(chips, function (e) {
      var chip = e.target.closest('.ms-chip');
      if (!chip) return;
      var it = items.filter(function (x) { return x.opt.value === chip.dataset.value; })[0];
      if (it) { it.opt.selected = false; renderChips(); }
    });

    tappable(trigger, function () {
      if (pop.hidden) open(); else close();
    });
    /* A phone synthesises mousedown/mouseup/click *after* pointerup, and a
       button takes focus on mousedown — which pulled focus straight back off
       the search box the open above had just put it on, so the keyboard never
       appeared on the first open. Cancelling the compat mousedown leaves
       keyboard focus (Tab) alone and costs nothing else. */
    trigger.addEventListener('mousedown', function (e) { e.preventDefault(); });

    input.addEventListener('focus', open);
    // Focus alone is not enough to reopen. Dismissing by tapping the page
    // background leaves the input focused on every browser that does not blur
    // on a tap into nothing, so the *next* tap on the field fired no focus
    // event and the menu stayed shut — the control looked dead until the user
    // typed. A guarded tap reopens it whatever the focus happens to be, and
    // being guarded is what stops a swipe over the field from doing the same.
    tappable(input, open);
    input.addEventListener('input', function () { open(); filter(); });

    input.addEventListener('keydown', function (e) {
      var vis = visible();
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        open();
        if (!vis.length) return;
        var next = e.key === 'ArrowDown' ? active + 1 : active - 1;
        if (next < 0) next = vis.length - 1;
        if (next >= vis.length) next = 0;
        setActive(next);
      } else if (e.key === 'Enter') {
        if (!pop.hidden && active >= 0 && vis[active]) {
          // Only swallow the Enter that picked something. With the menu shut,
          // Enter must still submit the form — that is the whole point of the
          // key on a search page.
          e.preventDefault();
          toggle(vis[active]);
        } else if (!pop.hidden && sheet()) {
          // In a sheet the search box is inside a modal layer, so the phone
          // keyboard's Enter must put the keyboard away, never post the form
          // out from under the sheet the user is still filling in.
          e.preventDefault();
          if (vis.length === 1) toggle(vis[0]); else input.blur();
        }
      } else if (e.key === 'Escape') {
        if (!pop.hidden) { e.preventDefault(); close(); }
      } else if (e.key === 'Backspace' && !input.value) {
        var on = items.filter(function (x) { return x.opt.selected; });
        if (on.length) { on[on.length - 1].opt.selected = false; renderChips(); }
      }
    });

    // Outside taps are handled once for every control at the top of the file.
    // This is only the keyboard case: tabbing away. It requires a real
    // relatedTarget, because a touch that moves focus to nothing reports null
    // and is already the document listener's business — and so does the blur()
    // this control does itself when the user starts scrolling.
    box.addEventListener('focusout', function (e) {
      if (e.relatedTarget && !box.contains(e.relatedTarget)) close();
    });

    // Rotating a phone or dragging a desktop window across the breakpoint
    // changes which shape is correct; reopening in the new one is honest, and
    // re-laying out an open sheet into a dropdown mid-gesture is not.
    var onMode = function () { close(); applyMode(); };
    if (SHEET_MQ.addEventListener) SHEET_MQ.addEventListener('change', onMode);
    else if (SHEET_MQ.addListener) SHEET_MQ.addListener(onMode);

    box.classList.add('ms-ready');
    applyMode();
    renderChips();
    filter();
  }

  Array.prototype.forEach.call(document.querySelectorAll('[data-ms]'), build);

  /* Alert-me-about-this-search button. Contract documented in browse.html. */
  var watch = document.querySelector('.watchbtn[data-watch-filters]');
  if (watch) {
    var offLabel = watch.getAttribute('aria-label') || '';
    var onLabel = watch.getAttribute('data-label-on') || offLabel;
    var paint = function (on) {
      watch.setAttribute('aria-pressed', on ? 'true' : 'false');
      watch.classList.toggle('is-watching', on);
      /* The label collapses into the bell in the on state, so the accessible
         name has to carry the state instead of the text. */
      watch.setAttribute('aria-label', on ? onLabel : offLabel);
    };
    /* Held between the click and the subscribe round-trip's answer. It runs the
       same collapse the on state ends in, so the wait is covered by an
       animation rather than by a frozen button — and if the answer is "no" the
       class comes off and the label comes back. */
    var busy = function (on) {
      if (on) watch.classList.add('bell-live');
      watch.classList.toggle('is-busy', on);
      watch.disabled = !!on;
    };
    window.tenderWatch = {
      button: watch,
      filters: watch.dataset.watchFilters,
      set: paint,
      busy: busy,
      isWatching: function () { return watch.getAttribute('aria-pressed') === 'true'; }
    };
    watch.addEventListener('click', function () {
      var want = watch.getAttribute('aria-pressed') !== 'true';
      watch.classList.add('bell-live');
      var ev = new CustomEvent('tender:watch', {
        bubbles: true, cancelable: true,
        detail: { filters: watch.dataset.watchFilters, watching: want }
      });
      if (watch.dispatchEvent(ev)) paint(want);
    });
  }
})();
