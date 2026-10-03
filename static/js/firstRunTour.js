// static/js/firstRunTour.js
// Unified first-run guided tour for Aurix.
//
// Takes precedence cleanly over the other tour helpers (tourHints.js,
// tourAutoplay.js, and the manual /tour-* slash tours) instead of fighting
// them:
//   - While running it sets `document.body.classList.add('tour-active')`,
//     the convention tourHints.js and the slash tours check before showing
//     anything of their own.
//   - It also sets `sessionStorage['aurix-tour-active'] = '1'` so any other
//     UI helper can defer while the tour is up.
//   - It never auto-starts while a modal, the command palette, or another
//     tour is visibly active: it polls for a quiet moment for ~10s, then
//     gives up silently for this session (without marking the tour done).
//   - All DOM ids/classes are `ftour-`-prefixed, so nothing collides with
//     the existing `#tour-tooltip` / `.tour-halo` elements.
//
// Fully self-contained: builds its own overlay DOM, injects its own
// <style>, self-initializes on DOMContentLoaded. No other hooks.

(function () {
  'use strict';

  var COMPLETED_KEY = 'aurix-tour-completed';
  var ACTIVE_KEY = 'aurix-tour-active';
  var START_DELAY_MS = 1500;
  var QUIET_POLL_MS = 500;
  var QUIET_POLLS_MAX = 20; // ~10s of polling for a quiet moment

  var STEPS = [
    {
      id: 'welcome',
      title: 'Welcome to Aurix',
      body: 'Your self-hosted AI workspace. Here is a 30-second tour — skip any time.',
      target: null,
    },
    {
      id: 'chat',
      title: 'Chat is home',
      body: 'Ask anything here. Type / to pull up power commands — sessions, memories, toggles and more.',
      target: '#message',
    },
    {
      id: 'palette',
      title: 'Jump anywhere',
      body: 'Press Ctrl+K (Cmd+K on Mac) to open the command palette and hop between tools instantly.',
      target: '#command-palette, .command-palette',
    },
    {
      id: 'sessions',
      title: 'Your sessions',
      body: 'Every conversation lives in the sidebar. Switch, star, or fork past chats any time.',
      target: '#sidebar',
    },
    {
      id: 'settings',
      title: 'Make it yours',
      body: 'Models, providers, and appearance live under Settings. The Command Center keeps an eye on everything.',
      target: '#rail-settings, #user-bar-settings',
    },
  ];

  var _inited = false;
  var _running = false;
  var _addedBodyClass = false;
  var _idx = 0;
  var _steps = [];
  var _overlay = null;
  var _spot = null;
  var _tip = null;
  var _raf = 0;

  function _lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function _lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage unavailable */ } }
  function _ssSet(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* storage unavailable */ } }
  function _ssDel(k) { try { sessionStorage.removeItem(k); } catch (e) { /* storage unavailable */ } }

  function _isVisible(el) {
    if (!el || !(el instanceof HTMLElement)) return false;
    if (el.classList.contains('hidden')) return false;
    if (el.style && el.style.display === 'none') return false;
    var r = el.getBoundingClientRect();
    return r.width > 2 && r.height > 2;
  }

  function _resolveTarget(step) {
    if (!step.target) return null;
    try {
      var el = document.querySelector(step.target);
      return _isVisible(el) ? el : null;
    } catch (e) {
      return null;
    }
  }

  // True when starting the tour right now would fight something else.
  function _appBusy() {
    try {
      if (document.body.classList.contains('tour-active')) return true;
      if (document.getElementById('tour-tooltip')) return true;
      if (document.querySelector('.tour-halo')) return true;
      var mods = document.querySelectorAll('.modal');
      for (var i = 0; i < mods.length; i++) {
        if (_isVisible(mods[i])) return true;
      }
      var pal = document.querySelector('#command-palette, .command-palette');
      if (pal && _isVisible(pal)) return true;
      return false;
    } catch (e) {
      return false;
    }
  }

  function _injectCss() {
    if (document.getElementById('ftour-style')) return;
    var css = [
      '#ftour-overlay{position:fixed;inset:0;z-index:99990;pointer-events:none;}',
      '#ftour-spot{position:fixed;display:none;border-radius:12px;pointer-events:none;',
      ' box-shadow:0 0 0 9999px rgba(4,6,10,0.62),0 0 0 2px rgba(125,211,252,0.9),0 0 26px rgba(125,211,252,0.30);',
      ' transition:left .25s ease,top .25s ease,width .25s ease,height .25s ease;}',
      '#ftour-tip{position:fixed;display:none;pointer-events:auto;z-index:99991;',
      ' max-width:min(340px,calc(100vw - 24px));',
      ' background:var(--panel,#141922);color:var(--fg,#e8eef6);',
      ' border:1px solid var(--border,rgba(125,211,252,0.28));border-radius:14px;',
      ' padding:16px 16px 12px;box-shadow:0 12px 40px rgba(0,0,0,0.5);',
      ' font-size:14px;line-height:1.45;}',
      '.ftour-title{margin:0 0 6px;font-size:16px;font-weight:600;}',
      '.ftour-body{margin:0;}',
      '.ftour-actions{display:flex;gap:8px;margin-top:12px;align-items:center;}',
      '.ftour-btn{cursor:pointer;border-radius:8px;padding:7px 14px;font-size:13px;border:1px solid transparent;}',
      '.ftour-next{background:#2f81f7;border-color:#2f81f7;color:#fff;}',
      '.ftour-back{background:transparent;color:inherit;border-color:rgba(140,140,140,0.45);}',
      '.ftour-back:disabled{cursor:default;}',
      '.ftour-skip{background:none;border:none;color:#8b949e;margin-left:auto;cursor:pointer;font-size:13px;padding:7px 4px;}',
      '.ftour-progress{font-size:12px;color:#8b949e;margin-top:10px;}',
      '@media (prefers-reduced-motion:reduce){#ftour-spot{transition:none !important;}}',
    ].join('\n');
    var st = document.createElement('style');
    st.id = 'ftour-style';
    st.textContent = css;
    document.head.appendChild(st);
  }

  function _build() {
    _overlay = document.createElement('div');
    _overlay.id = 'ftour-overlay';
    _overlay.setAttribute('aria-hidden', 'true');
    _spot = document.createElement('div');
    _spot.id = 'ftour-spot';
    _tip = document.createElement('div');
    _tip.id = 'ftour-tip';
    _tip.setAttribute('role', 'dialog');
    _overlay.appendChild(_spot);
    _overlay.appendChild(_tip);
    document.body.appendChild(_overlay);
  }

  function _placeTip(targetRect) {
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    // Measure while invisible so the card never flashes in the wrong spot.
    _tip.style.visibility = 'hidden';
    _tip.style.display = 'block';
    var tw = _tip.offsetWidth || 280;
    var th = _tip.offsetHeight || 120;
    var left, top;
    if (!targetRect) {
      left = Math.max(12, (vw - tw) / 2);
      top = Math.max(12, (vh - th) / 2);
    } else {
      var cx = targetRect.left + targetRect.width / 2;
      left = Math.round(cx - tw / 2);
      left = Math.max(12, Math.min(left, vw - tw - 12));
      var below = targetRect.bottom + 14;
      var above = targetRect.top - th - 14;
      if (below + th <= vh - 12) {
        top = below;
      } else if (above >= 12) {
        top = above;
      } else {
        // No clean room either side (small screens): prefer below, clamped.
        top = Math.max(12, Math.min(below, vh - th - 12));
      }
    }
    _tip.style.left = left + 'px';
    _tip.style.top = top + 'px';
    _tip.style.visibility = 'visible';
  }

  function _positionCurrent() {
    if (!_running || !_tip) return;
    var step = _steps[_idx];
    if (!step) { _end(true); return; }
    var el = _resolveTarget(step);
    if (step.target && !el) {
      // Target vanished mid-tour (layout changed): advance to the next
      // resolvable step instead of pointing at nothing.
      var next = _idx + 1;
      while (next < _steps.length && _steps[next].target && !_resolveTarget(_steps[next])) next++;
      if (next >= _steps.length) { _end(true); return; }
      _idx = next;
      _renderStep();
      return;
    }
    if (el) {
      var r = el.getBoundingClientRect();
      var pad = 8;
      _spot.style.display = 'block';
      _spot.style.left = Math.max(0, r.left - pad) + 'px';
      _spot.style.top = Math.max(0, r.top - pad) + 'px';
      _spot.style.width = (r.width + pad * 2) + 'px';
      _spot.style.height = (r.height + pad * 2) + 'px';
      _placeTip(r);
    } else {
      _spot.style.display = 'none';
      _placeTip(null);
    }
  }

  function _renderStep() {
    var step = _steps[_idx];
    if (!step) { _end(true); return; }
    var last = _idx === _steps.length - 1;
    _tip.innerHTML =
      '<h3 class="ftour-title"></h3>' +
      '<p class="ftour-body"></p>' +
      '<div class="ftour-actions">' +
        '<button type="button" class="ftour-btn ftour-back">Back</button>' +
        '<button type="button" class="ftour-btn ftour-next"></button>' +
        '<button type="button" class="ftour-skip">Skip tour</button>' +
      '</div>' +
      '<div class="ftour-progress"></div>';
    _tip.querySelector('.ftour-title').textContent = step.title;
    _tip.querySelector('.ftour-body').textContent = step.body;
    var nextBtn = _tip.querySelector('.ftour-next');
    var backBtn = _tip.querySelector('.ftour-back');
    nextBtn.textContent = last ? 'Finish' : 'Next';
    backBtn.disabled = _idx === 0;
    backBtn.style.opacity = _idx === 0 ? '0.4' : '';
    _tip.querySelector('.ftour-progress').textContent =
      'Step ' + (_idx + 1) + ' of ' + _steps.length;
    _tip.setAttribute('aria-label', step.title);
    backBtn.addEventListener('click', function () {
      if (_idx > 0) { _idx--; _renderStep(); }
    });
    nextBtn.addEventListener('click', function () {
      if (_idx < _steps.length - 1) { _idx++; _renderStep(); }
      else _end(true);
    });
    _tip.querySelector('.ftour-skip').addEventListener('click', function () {
      _end(true);
    });
    _positionCurrent();
  }

  function _scheduleReposition() {
    if (_raf) cancelAnimationFrame(_raf);
    _raf = requestAnimationFrame(function () {
      _raf = 0;
      if (_running) _positionCurrent();
    });
  }

  function _onKey(e) {
    if (!_running) return;
    if (e && e.key === 'Escape') {
      e.preventDefault();
      _end(true);
    }
  }

  function _end(completed) {
    if (!_running && !_overlay) return;
    _running = false;
    try {
      window.removeEventListener('scroll', _scheduleReposition, true);
      window.removeEventListener('resize', _scheduleReposition);
      document.removeEventListener('keydown', _onKey, true);
      if (_raf) cancelAnimationFrame(_raf);
      if (_overlay && _overlay.parentNode) _overlay.parentNode.removeChild(_overlay);
    } catch (e) { /* teardown must never throw */ }
    _overlay = null;
    _spot = null;
    _tip = null;
    _raf = 0;
    if (_addedBodyClass) {
      try { document.body.classList.remove('tour-active'); } catch (e) {}
      _addedBodyClass = false;
    }
    _ssDel(ACTIVE_KEY);
    if (completed) _lsSet(COMPLETED_KEY, '1');
  }

  function _start() {
    if (_running) return;
    // Resolve targets up front; steps whose element is missing or hidden
    // are skipped silently (e.g. the palette step before it exists).
    _steps = STEPS.filter(function (s) {
      return !s.target || !!_resolveTarget(s);
    });
    if (!_steps.length) return;
    _running = true;
    _idx = 0;
    _ssSet(ACTIVE_KEY, '1');
    try {
      if (!document.body.classList.contains('tour-active')) {
        document.body.classList.add('tour-active');
        _addedBodyClass = true;
      }
    } catch (e) { /* classList unavailable */ }
    _injectCss();
    _build();
    window.addEventListener('scroll', _scheduleReposition, true);
    window.addEventListener('resize', _scheduleReposition);
    document.addEventListener('keydown', _onKey, true);
    _renderStep();
  }

  function _maybeStart() {
    if (_lsGet(COMPLETED_KEY)) return;
    setTimeout(function () {
      var tries = 0;
      (function poll() {
        if (_lsGet(COMPLETED_KEY)) return;
        if (!_appBusy()) { _start(); return; }
        if (++tries >= QUIET_POLLS_MAX) return; // never quiet: skip silently this session
        setTimeout(poll, QUIET_POLL_MS);
      })();
    }, START_DELAY_MS);
  }

  function init() {
    if (_inited) return;
    _inited = true;
    if (_lsGet(COMPLETED_KEY)) return;
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', _maybeStart);
    } else {
      _maybeStart();
    }
  }

  init();
})();
