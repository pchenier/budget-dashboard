#!/usr/bin/env python3
"""
Budget App — Flask server for Railway/Render deployment.
Each user enters their own API keys via the /setup page.
Keys are stored in session only (never persisted server-side).

Usage local:
    python app.py

Deploy:
    Railway / Render — set SECRET_KEY env var, push repo, done.
"""

import os, json, threading
from flask import Flask, request, session, redirect, url_for, render_template_string, jsonify
from datetime import datetime, timedelta
from pathlib import Path

# Import data functions from generate.py
import sys
sys.path.insert(0, os.path.dirname(__file__))

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me-in-production-please")

# Cache per session (in-memory, keyed by session id)
_cache = {}
_cache_lock = threading.Lock()

CACHE_TTL_DAYS = 7

SETUP_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Budget App — Setup</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Montserrat', sans-serif; background: #0a0a0a; color: #f1f1f1; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 24px; }
  .card { background: #111; border: 1px solid #222; border-radius: 20px; padding: 40px; width: 100%; max-width: 520px; }
  h1 { font-size: 1.8rem; font-weight: 800; margin-bottom: 6px; background: linear-gradient(90deg,#fff,#93c5fd); -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; }
  .sub { font-size: 13px; color: #666; margin-bottom: 32px; }
  .section { margin-bottom: 28px; }
  .section-title { font-size: 11px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; color: #444; margin-bottom: 14px; padding-bottom: 8px; border-bottom: 1px solid #1e1e1e; }
  label { display: block; font-size: 12px; color: #888; margin-bottom: 5px; margin-top: 14px; }
  input { width: 100%; background: #181818; border: 1px solid #2a2a2a; border-radius: 8px; padding: 10px 14px; color: #f1f1f1; font-family: Montserrat; font-size: 13px; outline: none; transition: border .2s; }
  input:focus { border-color: #2563eb; }
  input::placeholder { color: #444; }
  .optional { font-size: 10px; color: #333; margin-left: 6px; }
  .btn { width: 100%; margin-top: 28px; padding: 14px; background: linear-gradient(135deg, #1e3a8a, #2563eb); border: none; border-radius: 10px; color: #fff; font-family: Montserrat; font-size: 15px; font-weight: 700; cursor: pointer; transition: opacity .2s; }
  .btn:hover { opacity: .9; }
  .btn:disabled { opacity: .5; cursor: not-allowed; }
  .error { background: rgba(247,110,110,.1); border: 1px solid rgba(247,110,110,.3); border-radius: 8px; padding: 12px 16px; font-size: 13px; color: #f76e6e; margin-bottom: 20px; }
  .help { font-size: 11px; color: #444; margin-top: 5px; }
  a { color: #2563eb; }
</style>
</head>
<body>
<div class="card">
  <h1>💰 Budget Dashboard</h1>
  <p class="sub">Entre tes clés API pour générer ton dashboard personnel. Rien n'est sauvegardé côté serveur.</p>

  {% if error %}
  <div class="error">{{ error }}</div>
  {% endif %}

  <form method="POST" action="/setup" id="form">
    <div class="section">
      <div class="section-title">🏦 Plaid (obligatoire)</div>
      <p class="help">Crée un compte sur <a href="https://dashboard.plaid.com" target="_blank">dashboard.plaid.com</a> → Team Settings → Keys</p>
      <label>Client ID</label>
      <input name="plaid_client" placeholder="6a148508..." required value="{{ vals.plaid_client or '' }}">
      <label>Secret</label>
      <input name="plaid_secret" type="password" placeholder="e953c3d2..." required>
      <label>Access Token <span class="optional">(obtenu via setup_plaid.py ou Plaid Quickstart)</span></label>
      <input name="plaid_token" placeholder="access-production-..." required value="{{ vals.plaid_token or '' }}">
      <label>Environment</label>
      <select name="plaid_env" style="width:100%;background:#181818;border:1px solid #2a2a2a;border-radius:8px;padding:10px 14px;color:#f1f1f1;font-family:Montserrat;font-size:13px;outline:none">
        <option value="production" {% if vals.plaid_env == 'production' %}selected{% endif %}>Production</option>
        <option value="sandbox"    {% if vals.plaid_env == 'sandbox' %}selected{% endif %}>Sandbox (test)</option>
      </select>
      <label>Date de début <span class="optional">(transactions depuis quand)</span></label>
      <input name="start_date" placeholder="2025-01-01" value="{{ vals.start_date or '2025-01-01' }}">
    </div>

    <div class="section">
      <div class="section-title">🌍 Wise <span class="optional">optionnel</span></div>
      <p class="help"><a href="https://wise.com/settings/account" target="_blank">wise.com/settings</a> → API tokens → Read-only</p>
      <label>API Token</label>
      <input name="wise_token" placeholder="932aba85-..." value="{{ vals.wise_token or '' }}">
      <label>Profile ID</label>
      <input name="wise_profile" placeholder="63963106" value="{{ vals.wise_profile or '' }}">
    </div>

    <div class="section">
      <div class="section-title">👻 Phantom <span class="optional">optionnel</span></div>
      <p class="help">Adresse publique Solana seulement (read-only)</p>
      <label>Wallet Address</label>
      <input name="phantom_wallet" placeholder="YourSolanaPublicKey..." value="{{ vals.phantom_wallet or '' }}">
    </div>

    <div class="section">
      <div class="section-title">⚙️ Options</div>
      <label>USD → CAD</label>
      <input name="usd_to_cad" placeholder="1.38" value="{{ vals.usd_to_cad or '1.38' }}">
    </div>

    <button type="submit" class="btn" id="submit-btn">🚀 Générer mon dashboard</button>
  </form>
  <script>
    document.getElementById('form').addEventListener('submit', function() {
      const btn = document.getElementById('submit-btn');
      btn.disabled = true;
      btn.textContent = '⏳ Connexion aux APIs...';
    });
  </script>
</div>
</body>
</html>"""

LOADING_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Budget App — Chargement...</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
<style>
  body { font-family: Montserrat, sans-serif; background: #0a0a0a; color: #f1f1f1; display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100vh; gap: 20px; }
  .spinner { width: 48px; height: 48px; border: 4px solid #1e1e1e; border-top-color: #2563eb; border-radius: 50%; animation: spin 1s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .msg { font-size: 15px; color: #888; }
  .submsg { font-size: 12px; color: #444; }
</style>
<script>
  // Poll until dashboard is ready
  function poll() {
    fetch('/status')
      .then(r => r.json())
      .then(d => {
        if (d.ready) { window.location = '/dashboard'; }
        else if (d.error) { window.location = '/setup?error=' + encodeURIComponent(d.error); }
        else { document.querySelector('.submsg').textContent = d.msg || ''; setTimeout(poll, 1500); }
      })
      .catch(() => setTimeout(poll, 2000));
  }
  window.addEventListener('DOMContentLoaded', poll);
</script>
</head>
<body>
  <div class="spinner"></div>
  <div class="msg">Connexion à tes comptes...</div>
  <div class="submsg">Plaid, Wise, Solana</div>
</body>
</html>"""


def get_session_key():
    if 'uid' not in session:
        import uuid
        session['uid'] = str(uuid.uuid4())
    return session['uid']


def get_cache(uid):
    with _cache_lock:
        entry = _cache.get(uid)
        if not entry:
            return None
        if datetime.now() - entry['ts'] > timedelta(days=CACHE_TTL_DAYS):
            del _cache[uid]
            return None
        return entry


def set_cache(uid, html, config):
    with _cache_lock:
        _cache[uid] = {'html': html, 'ts': datetime.now(), 'config': config}


def set_status(uid, msg=None, error=None, ready=False):
    with _cache_lock:
        if uid not in _cache:
            _cache[uid] = {'html': None, 'ts': datetime.now(), 'config': {}}
        _cache[uid]['status_msg']   = msg
        _cache[uid]['status_error'] = error
        _cache[uid]['status_ready'] = ready


def generate_for_user(uid, config):
    """Run in background thread — pulls APIs, builds HTML, stores in cache."""
    try:
        # Temporarily patch env vars for this thread
        import generate as gen
        import importlib

        # Override config
        gen.PLAID_CLIENT  = config['plaid_client']
        gen.PLAID_SECRET  = config['plaid_secret']
        gen.PLAID_TOKEN   = config['plaid_token']
        gen.PLAID_BASE    = "https://production.plaid.com" if config['plaid_env'] == 'production' else "https://sandbox.plaid.com"
        gen.WISE_TOKEN    = config.get('wise_token', '')
        gen.WISE_PROFILE  = int(config.get('wise_profile', 0) or 0)
        gen.PHANTOM_ADDR  = config.get('phantom_wallet', '')
        gen.USD_TO_CAD    = float(config.get('usd_to_cad', 1.38) or 1.38)
        gen.START_DATE    = config.get('start_date', '2025-01-01') or '2025-01-01'
        gen.END_DATE      = datetime.now().strftime('%Y-%m-%d')

        set_status(uid, msg='Plaid: balances...')
        balances = gen.get_plaid_balances()

        set_status(uid, msg='Wise: balances...')
        wise_bal = gen.get_wise_balances()

        set_status(uid, msg='Solana: balance...')
        sol_bal, sol_usd = gen.get_phantom_balance()

        set_status(uid, msg='Plaid: transactions...')
        raw_txns = gen.get_plaid_transactions()

        set_status(uid, msg='Traitement...')
        txns = gen.process_transactions(raw_txns)

        set_status(uid, msg='Génération HTML...')
        html = gen.build_html(balances, wise_bal, sol_bal, sol_usd, txns)

        # Inject refresh button for web
        html = inject_web_refresh(html, uid)

        set_cache(uid, html, config)
        set_status(uid, ready=True)

    except Exception as e:
        set_status(uid, error=str(e))


def inject_web_refresh(html, uid):
    """Inject a Refresh button that hits /refresh."""
    btn_css = """
  <style>
    .web-refresh-btn {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 8px 16px; background: rgba(79,134,247,.15);
      border: 1px solid rgba(79,134,247,.4); border-radius: 8px;
      color: #4f86f7; font-size: 13px; font-weight: 600;
      cursor: pointer; text-decoration: none; transition: all .2s;
      font-family: Montserrat, sans-serif;
    }
    .web-refresh-btn:hover { background: rgba(79,134,247,.3); }
    .web-refresh-btn.loading { opacity: .5; pointer-events: none; }
    @keyframes spin2 { to { transform: rotate(360deg); } }
    .spin2 { display: inline-block; animation: spin2 1s linear infinite; }
  </style>"""
    btn_script = """
<script>
function webRefresh() {
  const btn = document.getElementById('web-refresh');
  if (!btn) return;
  btn.classList.add('loading');
  btn.innerHTML = '<span class="spin2">⟳</span> Refresh...';
  fetch('/refresh', {method:'POST'})
    .then(r => r.json())
    .then(() => window.location.reload())
    .catch(() => { btn.classList.remove('loading'); btn.innerHTML = '🔄 Refresh'; });
}
</script>"""

    generated_str = datetime.now().strftime("%B %d, %Y at %H:%M")
    html = html.replace("</head>", btn_css + "\n</head>", 1)
    # Replace generated text + add button in header area
    html = html.replace(
        f'<div class="generated">Generated {generated_str}</div>',
        f'<div style="display:flex;flex-direction:column;align-items:flex-end;gap:8px">'
        f'<div class="generated">Generated {generated_str}</div>'
        f'<button id="web-refresh" class="web-refresh-btn" onclick="webRefresh()">🔄 Refresh</button>'
        f'</div>'
    )
    html = html.replace("</body>", btn_script + "\n</body>", 1)
    return html


@app.route('/')
def index():
    uid = get_session_key()
    entry = get_cache(uid)
    if entry and entry.get('html'):
        return redirect(url_for('dashboard'))
    return redirect(url_for('setup'))


@app.route('/setup', methods=['GET', 'POST'])
def setup():
    uid = get_session_key()
    error = request.args.get('error', '')
    vals = session.get('config', {})

    if request.method == 'POST':
        config = {
            'plaid_client':   request.form.get('plaid_client', '').strip(),
            'plaid_secret':   request.form.get('plaid_secret', '').strip(),
            'plaid_token':    request.form.get('plaid_token', '').strip(),
            'plaid_env':      request.form.get('plaid_env', 'production'),
            'start_date':     request.form.get('start_date', '2025-01-01').strip(),
            'wise_token':     request.form.get('wise_token', '').strip(),
            'wise_profile':   request.form.get('wise_profile', '').strip(),
            'phantom_wallet': request.form.get('phantom_wallet', '').strip(),
            'usd_to_cad':     request.form.get('usd_to_cad', '1.38').strip(),
        }
        # Save FULL config in session (including secret — encrypted by Flask session)
        session['config'] = config
        vals = {k: v for k, v in config.items() if k != 'plaid_secret'}

        if not config['plaid_client'] or not config['plaid_secret'] or not config['plaid_token']:
            return render_template_string(SETUP_HTML, error='Plaid Client ID, Secret et Access Token sont obligatoires.', vals=vals)

        # Init cache slot and kick off background generation
        set_status(uid, msg='Démarrage...')
        t = threading.Thread(target=generate_for_user, args=(uid, config), daemon=True)
        t.start()
        return render_template_string(LOADING_HTML)

    return render_template_string(SETUP_HTML, error=error, vals=vals)


@app.route('/status')
def status():
    uid = get_session_key()
    with _cache_lock:
        entry = _cache.get(uid, {})
    return jsonify({
        'ready': entry.get('status_ready', False),
        'error': entry.get('status_error'),
        'msg':   entry.get('status_msg', ''),
    })


@app.route('/dashboard')
def dashboard():
    uid = get_session_key()
    entry = get_cache(uid)
    if not entry or not entry.get('html'):
        return redirect(url_for('setup'))
    return entry['html']


@app.route('/refresh', methods=['POST'])
def refresh():
    uid = get_session_key()
    entry = get_cache(uid)
    # Try cache config first, then session config (has the secret)
    full_config = (entry or {}).get('config') or session.get('config', {})
    if not full_config.get('plaid_client') or not full_config.get('plaid_secret'):
        return jsonify({'ok': False, 'msg': 'Session expirée — retourne sur /setup pour rentrer tes clés'})
    set_status(uid, msg='Refresh en cours...')
    t = threading.Thread(target=generate_for_user, args=(uid, full_config), daemon=True)
    t.start()
    return jsonify({'ok': True})


@app.route('/logout')
def logout():
    uid = get_session_key()
    with _cache_lock:
        _cache.pop(uid, None)
    session.clear()
    return redirect(url_for('setup'))


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5050))
    debug = os.getenv('FLASK_DEBUG', '0') == '1'
    print(f"\n💰 Budget App → http://localhost:{port}\n")
    app.run(host='0.0.0.0', port=port, debug=debug)
