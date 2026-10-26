/* Fiscit 2 dashboard motion: greeting, staggered entrance, count-up numbers,
   cursor-follow light on cards. Wraps vault.html's navigate(). */
(function () {
  const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const SURFACES = '.page-hero,.filter-bar,.metric,.card,.card-xs,.acc-card,.insight,.habit-card';

  function greeting() {
    const h = new Date().getHours();
    const part = h < 5 ? 'Up late' : h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
    const raw = (typeof D !== 'undefined' && D.userName) || '';
    const first = (typeof unesc === 'function' ? unesc(raw) : raw).trim().split(/\s+/)[0];
    const safe = first.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
    const date = new Date().toLocaleDateString('en', { weekday: 'long', month: 'long', day: 'numeric' });
    const el = document.createElement('div');
    el.className = 'page-hero';
    el.innerHTML = `<h1>${part}${safe ? `, <em>${safe}.</em>` : '<em>.</em>'}</h1>
      <p>Here's where your money stands — ${date}.</p>`;
    return el;
  }

  // "$164,211" / "-$2,640.50" / "37.7%" → animated from 0, ending on the exact original text
  const NUM = /^(-?)(\$?)([\d,]+(?:\.\d+)?)(%?)$/;
  function countUp(el, delay) {
    if (el.childNodes.length !== 1 || el.firstChild.nodeType !== 3) return;
    const text = el.textContent.trim();
    const m = text.match(NUM);
    if (!m) return;
    const target = parseFloat(m[3].replace(/,/g, ''));
    if (!target) return;
    const decimals = (m[3].split('.')[1] || '').length;
    const fmt = (v) => m[1] + m[2] + v.toLocaleString('en-CA', { minimumFractionDigits: decimals, maximumFractionDigits: decimals }) + m[4];
    const dur = 1100;
    let t0 = null;
    el.textContent = fmt(0);
    function frame(t) {
      if (!el.isConnected) return;
      if (t0 === null) t0 = t + delay;
      const p = Math.min(1, Math.max(0, (t - t0) / dur));
      const e = p === 1 ? 1 : 1 - Math.pow(2, -10 * p); // ease-out expo
      el.textContent = p === 1 ? text : fmt(target * e);
      if (p < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  function enhance(page) {
    const wrap = document.querySelector('#content > .fade-in');
    if (!wrap) return;
    if (page === 'dashboard') wrap.prepend(greeting());
    wrap.querySelectorAll(SURFACES).forEach((el, i) => el.style.setProperty('--i', Math.min(i, 14)));
    if (reduced()) return;
    wrap.querySelectorAll('.metric-val, .acc-bal').forEach((el, i) => countUp(el, 120 + i * 45));
  }

  function wrapNavigate() {
    if (typeof window.navigate !== 'function' || window.navigate.__branded) return;
    const original = window.navigate;
    const branded = function (page) {
      const r = original.apply(this, arguments);
      try { enhance(page); } catch (e) { console.error('motion:', e); }
      return r;
    };
    branded.__branded = true;
    window.navigate = branded;
  }

  // Cursor-follow light on surfaces (one delegated listener)
  document.addEventListener('pointermove', (e) => {
    if (e.pointerType !== 'mouse') return;
    const el = e.target.closest && e.target.closest('.metric,.card,.acc-card,.insight');
    if (!el) return;
    const r = el.getBoundingClientRect();
    el.style.setProperty('--mx', e.clientX - r.left + 'px');
    el.style.setProperty('--my', e.clientY - r.top + 'px');
  }, { passive: true });

  wrapNavigate();
})();
