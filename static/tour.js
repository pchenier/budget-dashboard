/* Fiscit guided tour — spotlights each tab, opens its page, explains it.
   Depends on vault.html globals: navigate(page), D, feather. */
(function () {
  const DONE_KEY = 'fiscit_tour_v1';
  const MOBILE_MAX = 700;

  // On mobile the sidebar is hidden: point at the bottom tab instead, or at
  // "More" for pages that live in the More sheet.
  const mobileTab = (page) =>
    document.querySelector(`.mtabs [data-mpage="${page}"]`) || document.querySelector('.mtabs [data-more]');
  const nav = (page) => () => (isMobile() ? mobileTab(page) : document.querySelector(`.nav-item[data-page="${page}"]`));
  const group = (page) => () =>
    isMobile() ? mobileTab(page) : document.querySelector(`.nav-item[data-page="${page}"]`)?.closest('.nav-group');

  const STEPS = [
    {
      title: 'This is example data',
      body: 'During the tour every page is filled with a sample account so you can see how it looks. Your real numbers come back as soon as the tour ends.',
      page: 'dashboard',
    },
    {
      target: nav('dashboard'), page: 'dashboard',
      title: 'Dashboard',
      body: 'Your home base: income vs spending this month, cash flow, your top budgets, upcoming bills and the latest transactions.',
    },
    {
      target: nav('accounts'), page: 'accounts',
      title: 'Accounts',
      body: 'Every connected bank account and card with its current balance. Balances refresh each time Fiscit syncs.',
    },
    {
      target: nav('transactions'), page: 'transactions',
      title: 'Transactions',
      body: 'Search and filter everything. Click a transaction to change its category — and create a rule so future ones are sorted automatically.',
    },
    {
      target: nav('budget'), page: 'budget',
      title: 'Budget',
      body: 'Set a monthly limit per category and watch the bars fill up. When you go over, a red badge appears on this tab.',
    },
    {
      target: nav('insights'), page: 'insights',
      title: 'Insights',
      body: 'Short, plain-language notes about your month: what went up, what went down, and where to look.',
    },
    {
      target: nav('savings'), page: 'savings',
      title: 'Savings Rate',
      body: 'The share of your income you kept, month by month over the last year. The one number that tells you if you are getting ahead.',
    },
    {
      target: nav('subscriptions'), page: 'subscriptions',
      title: 'Subscriptions',
      body: 'Recurring charges pulled out of your transactions, with new ones flagged — handy for spotting what you forgot to cancel.',
    },
    {
      target: group('investments'), page: 'investments',
      title: 'Money: Investments & Crypto',
      body: 'Your portfolio and your crypto wallets (read-only public addresses), rolled into your net worth.',
    },
    {
      target: group('gym'), page: 'habits',
      title: 'Life: Gym, Eating, Habits, Calendar',
      body: 'Optional trackers for the rest of your routine. Heads-up: these are saved in this browser only, not synced across devices yet.',
    },
    {
      target: ['#sync-dot', '#sync-ts', '#refresh-btn'], page: 'dashboard',
      title: 'Sync status',
      body: 'The green dot means your data is live. Hit refresh to pull the latest from your banks right now — and the Tour button replays this walkthrough anytime.',
    },
    {
      target: '.icon-btn[title="Search transactions"]',
      title: 'Search',
      body: 'Jump straight to any transaction by merchant, category or amount.',
    },
    {
      target: '.icon-btn[title="Notifications"]',
      title: 'Notifications',
      body: 'Budget alerts and account events land here.',
    },
    {
      target: nav('settings'), page: 'settings',
      title: 'Settings',
      body: 'Connect banks (Plaid), Wise and crypto wallets, change your display currency, and replay this tour whenever you like.',
    },
    { final: true, page: 'dashboard' },
  ];

  let i = 0;
  let root, spot, card, badge, onKey, onResize, raf;
  let realData = null;
  let realConnected = false;

  function isMobile() { return window.innerWidth <= MOBILE_MAX; }
  const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function hasConnections() {
    const c = (typeof D !== 'undefined' && D.connections) || {};
    return !!(c.plaid || c.wise || (D.wallets || []).length || (D.accounts || []).length);
  }

  // ── Example data: swapped into D for the tour only, then restored ──
  function useDemoData() {
    if (realData || typeof window.buildFiscitDemo !== 'function') return;
    realConnected = hasConnections();
    realData = JSON.parse(JSON.stringify(D));
    const demo = window.buildFiscitDemo();
    demo.userName = realData.userName;
    demo.userEmail = realData.userEmail;
    Object.keys(D).forEach((k) => delete D[k]);
    Object.assign(D, demo);
    window.FISCIT_TOUR_ACTIVE = true;
  }

  function restoreRealData() {
    window.FISCIT_TOUR_ACTIVE = false;
    if (!realData) return;
    Object.keys(D).forEach((k) => delete D[k]);
    Object.assign(D, realData);
    realData = null;
  }

  function markSeen() {
    try { localStorage.setItem(DONE_KEY, '1'); } catch (e) {}
    const headers = typeof csrfHeaders === 'function' ? csrfHeaders() : {};
    fetch('/api/tour/seen', { method: 'POST', headers, credentials: 'include' }).catch(() => {});
  }

  function resolveTargets(step) {
    if (!step.target) return [];
    const list = Array.isArray(step.target) ? step.target : [step.target];
    return list
      .map((t) => (typeof t === 'function' ? t() : document.querySelector(t)))
      .filter((el) => el && el.offsetParent !== null && el.getClientRects().length);
  }

  function unionRect(els) {
    const rs = els.map((e) => e.getBoundingClientRect());
    const left = Math.min(...rs.map((r) => r.left));
    const top = Math.min(...rs.map((r) => r.top));
    const right = Math.max(...rs.map((r) => r.right));
    const bottom = Math.max(...rs.map((r) => r.bottom));
    return { left, top, width: right - left, height: bottom - top, right, bottom };
  }

  function injectStyles() {
    if (document.getElementById('tour-css')) return;
    const css = document.createElement('style');
    css.id = 'tour-css';
    css.textContent = `
#tour-root{position:fixed;inset:0;z-index:10000;pointer-events:none}
#tour-root .t-block{position:fixed;inset:0;pointer-events:auto}
#tour-spot{position:fixed;border-radius:10px;pointer-events:none;
  box-shadow:0 0 0 9999px rgba(0,0,0,.72),0 0 0 2px var(--green),0 0 24px 4px rgba(184,245,102,.35);
  transition:top .45s cubic-bezier(.16,1,.3,1),left .45s cubic-bezier(.16,1,.3,1),width .45s cubic-bezier(.16,1,.3,1),height .45s cubic-bezier(.16,1,.3,1),opacity .25s}
#tour-spot::after{content:'';position:absolute;inset:-6px;border-radius:14px;border:2px solid var(--green);opacity:0;animation:tourPulse 1.8s ease-out infinite}
@keyframes tourPulse{0%{opacity:.7;transform:scale(.96)}100%{opacity:0;transform:scale(1.12)}}
#tour-spot.t-none{width:0!important;height:0!important;left:50%!important;top:50%!important}
#tour-spot.t-none::after{display:none}
#tour-card{position:fixed;pointer-events:auto;width:340px;max-width:calc(100vw - 32px);background:var(--bg2);border:1px solid rgba(255,255,255,.1);
  border-radius:16px;padding:20px 20px 16px;box-shadow:0 24px 60px rgba(0,0,0,.55);font-family:var(--font);color:var(--text);
  transition:top .45s cubic-bezier(.16,1,.3,1),left .45s cubic-bezier(.16,1,.3,1),opacity .2s,transform .2s}
#tour-card.t-enter{opacity:0;transform:translateY(6px) scale(.98)}
#tour-card .t-arrow{position:absolute;width:12px;height:12px;background:var(--bg2);border:1px solid rgba(255,255,255,.1);transform:rotate(45deg)}
#tour-card .t-arrow.left{left:-7px;border-right:none;border-top:none}
#tour-card .t-arrow.top{top:-7px;border-right:none;border-bottom:none}
#tour-card .t-arrow.bottom{bottom:-7px;border-left:none;border-top:none}
#tour-card .t-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
#tour-card .t-count{font-size:11px;color:var(--text3);font-variant-numeric:tabular-nums;letter-spacing:.04em}
#tour-card .t-x{background:none;border:none;color:var(--text3);cursor:pointer;font-size:18px;line-height:1;padding:2px 4px;border-radius:6px}
#tour-card .t-x:hover{color:var(--text)}
#tour-card h3{font-size:16px;font-weight:600;margin:0 0 6px;letter-spacing:-.01em}
#tour-card p{font-size:13px;line-height:1.6;color:var(--text2);margin:0}
#tour-card .t-bar{height:3px;background:var(--bg3);border-radius:2px;margin:16px 0 14px;overflow:hidden}
#tour-card .t-fill{height:100%;background:var(--green);border-radius:2px;transition:width .45s cubic-bezier(.16,1,.3,1)}
#tour-card .t-actions{display:flex;justify-content:space-between;align-items:center;gap:8px}
#tour-card button.t-btn{font-family:var(--font);font-size:13px;border-radius:8px;padding:8px 16px;cursor:pointer;border:1px solid var(--border)}
#tour-card .t-back{background:none;color:var(--text2)}
#tour-card .t-back:disabled{opacity:.3;cursor:default}
#tour-card .t-next{background:var(--green);color:#000;border-color:var(--green);font-weight:600}
#tour-card .t-ghost{background:none;color:var(--text2)}
#tour-card button:focus{outline:none}
#tour-card button:focus-visible{outline:2px solid rgba(255,255,255,.55);outline-offset:2px}
#tour-card .t-keys{font-size:11px;color:var(--text3)}
#tour-card.t-sheet{left:12px!important;right:12px;bottom:calc(84px + env(safe-area-inset-bottom));top:auto!important;width:auto;max-width:none}
#tour-card.t-center{left:50%!important;top:50%!important;transform:translate(-50%,-50%)}
#tour-card.t-center.t-enter{transform:translate(-50%,-48%) scale(.98)}
#tour-badge{position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:10001;pointer-events:none;
  background:rgba(250,204,21,.12);color:#facc15;border:1px solid rgba(250,204,21,.35);border-radius:999px;
  padding:4px 12px;font:600 11px var(--font);letter-spacing:.06em;text-transform:uppercase;backdrop-filter:blur(8px)}
#tour-prompt{position:fixed;inset:0;z-index:10002;display:flex;align-items:center;justify-content:center;padding:16px;
  background:rgba(0,0,0,.78);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);animation:tpIn .25s ease}
#tour-prompt.tp-out{opacity:0;transition:opacity .18s}
#tour-prompt .tp-card{width:100%;max-width:420px;background:var(--bg2);border:1px solid rgba(255,255,255,.1);border-radius:18px;
  padding:32px 28px 22px;text-align:center;font-family:var(--font);color:var(--text);box-shadow:0 30px 80px rgba(0,0,0,.6);animation:tpCard .4s cubic-bezier(.16,1,.3,1)}
#tour-prompt .tp-ico{width:56px;height:56px;margin:0 auto 16px;border-radius:16px;display:flex;align-items:center;justify-content:center;
  background:rgba(184,245,102,.12);color:var(--green);box-shadow:0 0 0 6px rgba(184,245,102,.05)}
#tour-prompt h2{font-size:20px;font-weight:600;margin:0 0 8px;letter-spacing:-.01em}
#tour-prompt p{font-size:13px;line-height:1.6;color:var(--text2);margin:0 0 22px}
#tour-prompt .tp-actions{display:flex;gap:10px;flex-wrap:wrap-reverse}
#tour-prompt .tp-btn{flex:1 1 150px;font-family:var(--font);font-size:13px;border-radius:10px;padding:11px 14px;cursor:pointer;border:1px solid var(--border)}
#tour-prompt .tp-skip{background:none;color:var(--text2)}
#tour-prompt .tp-skip:hover{color:var(--text);border-color:rgba(255,255,255,.18)}
#tour-prompt .tp-go{background:var(--green);border-color:var(--green);color:#000;font-weight:600}
#tour-prompt .tp-btn:focus{outline:none}
#tour-prompt .tp-btn:focus-visible{outline:2px solid rgba(255,255,255,.55);outline-offset:2px}
#tour-prompt .tp-hint{margin-top:14px;font-size:11px;color:var(--text3)}
@keyframes tpIn{from{opacity:0}to{opacity:1}}
@keyframes tpCard{from{opacity:0;transform:translateY(10px) scale(.97)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion: reduce){#tour-spot,#tour-card,#tour-card .t-fill{transition:none}#tour-spot::after,#tour-prompt,#tour-prompt .tp-card{animation:none}}
@media (max-width:${MOBILE_MAX}px){#tour-card .t-keys{display:none}}`;
    document.head.appendChild(css);
  }

  function build() {
    injectStyles();
    root = document.createElement('div');
    root.id = 'tour-root';
    root.innerHTML = `<div class="t-block"></div><div id="tour-spot" class="t-none"></div>
      <div id="tour-card" role="dialog" aria-modal="true" aria-labelledby="tour-title" aria-describedby="tour-body"></div>`;
    document.body.appendChild(root);
    spot = root.querySelector('#tour-spot');
    card = root.querySelector('#tour-card');

    onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); end(); }
      else if (e.key === 'ArrowRight' || e.key === 'Enter') { e.preventDefault(); next(); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); back(); }
    };
    onResize = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(() => place(STEPS[i])); };
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', onResize);
  }

  function cardHTML(step) {
    const total = STEPS.length;
    if (step.final) {
      const connected = realConnected;
      return `
        <div class="t-top"><span class="t-count">DONE</span><button class="t-x" aria-label="Close tour">×</button></div>
        <h3 id="tour-title">You're all set 🎉</h3>
        <p id="tour-body">${connected
          ? 'Your accounts are connected — your real numbers are back on screen. Replay this tour anytime with the Tour button at the top.'
          : 'Next step: connect your bank so these pages fill up with your real numbers. It takes about a minute. You can replay this tour anytime with the Tour button at the top.'}</p>
        <div class="t-bar"><div class="t-fill" style="width:100%"></div></div>
        <div class="t-actions">
          ${connected ? '<span></span>' : '<button class="t-btn t-ghost" data-act="end">Later</button>'}
          <button class="t-btn t-next" data-act="${connected ? 'end' : 'connect'}">${connected ? 'Start using Fiscit' : 'Connect my bank'}</button>
        </div>`;
    }
    return `
      <div class="t-top"><span class="t-count">${i + 1} / ${total - 1}</span><button class="t-x" aria-label="Close tour">×</button></div>
      <h3 id="tour-title">${step.title}</h3>
      <p id="tour-body">${step.body}</p>
      <div class="t-bar"><div class="t-fill" style="width:${((i + 1) / (total - 1)) * 100}%"></div></div>
      <div class="t-actions">
        <button class="t-btn t-back" data-act="back" ${i === 0 ? 'disabled' : ''}>Back</button>
        <span class="t-keys">← → · Esc</span>
        <button class="t-btn t-next" data-act="next">${i === 0 ? 'Start tour' : i === total - 2 ? 'Finish' : 'Next'}</button>
      </div>`;
  }

  function place(step) {
    const els = resolveTargets(step);
    card.classList.remove('t-sheet', 't-center');
    card.querySelector('.t-arrow')?.remove();

    // No visible target (welcome, final, or the sidebar is hidden on mobile):
    // fall back to the page title so the user still sees where they are.
    let target = els.length ? unionRect(els) : null;
    if (!target && step.page && !step.final && i !== 0) {
      const title = document.getElementById('page-title');
      if (title && title.offsetParent) target = unionRect([title]);
    }

    if (!target) {
      spot.classList.add('t-none');
      if (isMobile()) card.classList.add('t-sheet'); else card.classList.add('t-center');
      return;
    }

    const pad = 6;
    spot.classList.remove('t-none');
    Object.assign(spot.style, {
      left: target.left - pad + 'px', top: target.top - pad + 'px',
      width: target.width + pad * 2 + 'px', height: target.height + pad * 2 + 'px',
    });

    if (isMobile()) { card.classList.add('t-sheet'); return; }

    const gap = 16, cw = card.offsetWidth, ch = card.offsetHeight;
    const vw = window.innerWidth, vh = window.innerHeight;
    const arrow = document.createElement('div');
    arrow.className = 't-arrow';
    let left, top;

    if (target.right + gap + cw <= vw - 16) {
      // Right of the target (sidebar items)
      left = target.right + gap;
      top = Math.min(Math.max(16, target.top + target.height / 2 - ch / 2), vh - ch - 16);
      arrow.classList.add('left');
      arrow.style.top = Math.min(Math.max(16, target.top + target.height / 2 - top - 6), ch - 28) + 'px';
    } else if (target.bottom + gap + ch <= vh - 16) {
      // Below the target (top bar buttons)
      top = target.bottom + gap;
      left = Math.min(Math.max(16, target.left + target.width / 2 - cw / 2), vw - cw - 16);
      arrow.classList.add('top');
      arrow.style.left = Math.min(Math.max(16, target.left + target.width / 2 - left - 6), cw - 28) + 'px';
    } else {
      top = Math.max(16, target.top - gap - ch);
      left = Math.min(Math.max(16, target.left + target.width / 2 - cw / 2), vw - cw - 16);
      arrow.classList.add('bottom');
      arrow.style.left = Math.min(Math.max(16, target.left + target.width / 2 - left - 6), cw - 28) + 'px';
    }
    card.style.left = left + 'px';
    card.style.top = top + 'px';
    card.appendChild(arrow);
  }

  function show() {
    const step = STEPS[i];
    if (step.page && typeof navigate === 'function') {
      const active = document.querySelector('.nav-item.active');
      if (!active || active.dataset.page !== step.page) navigate(step.page);
    }
    card.classList.add('t-enter');
    card.innerHTML = cardHTML(step);
    card.querySelectorAll('[data-act]').forEach((b) => (b.onclick = () => act(b.dataset.act)));
    card.querySelector('.t-x').onclick = end;

    // Let the page render before measuring the target.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      resolveTargets(step)[0]?.scrollIntoView({ block: 'nearest', behavior: reduced() ? 'auto' : 'smooth' });
      place(step);
      card.classList.remove('t-enter');
      card.querySelector('.t-next')?.focus({ preventScroll: true });
    }));
  }

  function act(a) {
    if (a === 'next') next();
    else if (a === 'back') back();
    else if (a === 'connect') { end(); navigate('settings'); }
    else end();
  }
  function next() { if (i < STEPS.length - 1) { i++; show(); } else end(); }
  function back() { if (i > 0) { i--; show(); } }

  function end() {
    markSeen();
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('resize', onResize);
    restoreRealData();
    if (typeof navigate === 'function') navigate('dashboard');
    [root, badge].forEach((el) => {
      if (!el) return;
      el.style.transition = 'opacity .2s';
      el.style.opacity = '0';
      setTimeout(() => el.remove(), 200);
    });
    root = badge = null;
  }

  function showBadge() {
    badge = document.createElement('div');
    badge.id = 'tour-badge';
    badge.textContent = 'Example data · Tour';
    document.body.appendChild(badge);
  }

  function start() {
    if (root) return;
    document.getElementById('tour-prompt')?.remove();
    i = 0;
    useDemoData();
    build();
    showBadge();
    if (typeof navigate === 'function') navigate(STEPS[0].page); // re-render with example data
    show();
  }

  // First visit: offer the tour; skipping goes straight to the dashboard.
  function offer() {
    if (document.getElementById('tour-prompt') || root) return;
    injectStyles();
    const p = document.createElement('div');
    p.id = 'tour-prompt';
    p.innerHTML = `
      <div class="tp-card" role="dialog" aria-modal="true" aria-labelledby="tp-title">
        <div class="tp-ico">${typeof ico === 'function' ? ico('map', 26) : ''}</div>
        <h2 id="tp-title">Take the guided tour?</h2>
        <p>One minute, every tab explained — shown with example data so you can see what Fiscit does before your accounts fill in.</p>
        <div class="tp-actions">
          <button class="tp-btn tp-skip" data-act="skip">Skip for now</button>
          <button class="tp-btn tp-go" data-act="go">Take the tour</button>
        </div>
        <div class="tp-hint">You can replay it anytime with the Tour button at the top.</div>
      </div>`;
    document.body.appendChild(p);
    if (window.feather) feather.replace({ width: 26, height: 26 });
    const close = (go) => {
      document.removeEventListener('keydown', key, true);
      p.classList.add('tp-out');
      setTimeout(() => p.remove(), 180);
      if (go) start(); else markSeen();
    };
    const key = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); close(false); }
      if (e.key === 'Enter') { e.preventDefault(); close(true); }
    };
    document.addEventListener('keydown', key, true);
    p.querySelector('[data-act="go"]').onclick = () => close(true);
    p.querySelector('[data-act="skip"]').onclick = () => close(false);
    p.querySelector('[data-act="go"]').focus();
  }

  function maybeStart() {
    let seenHere = false;
    try { seenHere = localStorage.getItem(DONE_KEY) === '1'; } catch (e) {}
    // The server flag follows the user across devices; localStorage covers a failed save.
    const seen = (typeof D !== 'undefined' && D.tourSeen) || seenHere;
    if (!seen && !document.getElementById('onboard-overlay')) setTimeout(offer, 500);
  }

  window.FiscitTour = { start, offer, maybeStart };
})();
