#!/usr/bin/env python3
"""
Vault — Budget & Life Dashboard
Serves the Vault UI with real Plaid/Wise/Phantom data.

Usage:
    python app.py

On first run with no saved config → redirects to /setup.
After setup, config is saved to saved_config.json — never asks again on restart.
"""

import os, json, threading, uuid
from flask import Flask, request, session, redirect, url_for, render_template_string, jsonify, send_from_directory
from datetime import datetime, timedelta
from pathlib import Path

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "vault-local-secret-do-not-deploy")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
CONFIG_FILE  = BASE_DIR / "saved_config.json"
VAULT_HTML   = BASE_DIR / "vault.html"
CACHE_FILE   = BASE_DIR / "data_cache.json"

# ── In-memory state ───────────────────────────────────────────────────────────
_state_lock = threading.Lock()
_state = {
    "status":  "idle",   # idle | loading | ready | error
    "msg":     "",
    "error":   "",
    "data":    None,     # the real data dict from generate_data.pull_all()
    "loaded_at": None,
}

# ── Persistent config ─────────────────────────────────────────────────────────
def load_config():
    try:
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text())
            if cfg.get("plaid_client") and cfg.get("plaid_secret") and cfg.get("plaid_token"):
                return cfg
    except Exception:
        pass
    return None

def save_config(config):
    try:
        CONFIG_FILE.write_text(json.dumps(config, indent=2))
    except Exception as e:
        print(f"[warn] Could not save config: {e}")

# ── Data cache (disk) ─────────────────────────────────────────────────────────
def load_data_cache():
    """Load last pulled data from disk (survives server restarts)."""
    try:
        if CACHE_FILE.exists():
            raw = json.loads(CACHE_FILE.read_text())
            # Check age — use cached data up to 6 hours
            loaded = raw.get("_cached_at")
            if loaded:
                age = datetime.now() - datetime.fromisoformat(loaded)
                if age < timedelta(hours=6):
                    return raw
    except Exception:
        pass
    return None

def save_data_cache(data):
    try:
        data["_cached_at"] = datetime.now().isoformat()
        CACHE_FILE.write_text(json.dumps(data, indent=2, default=str))
    except Exception as e:
        print(f"[warn] Could not save data cache: {e}")

# ── Background data fetch ─────────────────────────────────────────────────────
def fetch_data(config):
    """Pull all real data in background thread."""
    def status_cb(msg):
        with _state_lock:
            _state["msg"] = msg
        print(f"  [{msg}]")

    config["_status_cb"] = status_cb

    try:
        with _state_lock:
            _state["status"] = "loading"
            _state["msg"]    = "Starting..."
            _state["error"]  = ""

        import generate_data
        data = generate_data.pull_all(config)

        save_data_cache(data)

        with _state_lock:
            _state["data"]      = data
            _state["status"]    = "ready"
            _state["loaded_at"] = datetime.now().isoformat()
            _state["msg"]       = f"Last synced: {datetime.now().strftime('%H:%M')}"

    except Exception as e:
        with _state_lock:
            _state["status"] = "error"
            _state["error"]  = str(e)
        print(f"[error] fetch_data: {e}")

# ── On startup: load config + cached data, kick off refresh ───────────────────
def startup():
    config = load_config()
    if not config:
        print("\n⚠️  No saved config — open http://localhost:5050/setup\n")
        return

    # Load disk cache immediately so first page load is instant
    cached = load_data_cache()
    if cached:
        with _state_lock:
            _state["data"]   = cached
            _state["status"] = "ready"
            _state["msg"]    = f"Cached data from {cached.get('_generated','?')}"
        print(f"  Loaded cached data ({cached.get('_generated','?')})")

    # Then refresh in background
    print("  Refreshing data from APIs...")
    t = threading.Thread(target=fetch_data, args=(config,), daemon=True)
    t.start()

# ── Vault HTML with data injection ───────────────────────────────────────────
def build_vault_html(data):
    """Read vault.html and inject real data by replacing the mock D = {...} block."""
    if not VAULT_HTML.exists():
        return "<h1>vault.html not found</h1>"

    html = VAULT_HTML.read_text(encoding="utf-8")

    # Build real data JS object
    real_js = _build_real_data_js(data)

    # Replace mock D object — use lambda to avoid re backreference issues with unicode
    import re
    html = re.sub(
        r'const D = \{.*?\n\};',
        lambda m: real_js,
        html,
        count=1,
        flags=re.DOTALL,
    )

    # Inject sync timestamp + refresh button into topbar
    html = html.replace(
        '</header>',
        f'<!-- vault-injected --></header>',
        1
    )

    return html

def _build_real_data_js(data):
    """Convert Python data dict to JS const D = {...} that matches vault.html mock field names."""
    raw_accounts = data.get("accounts", [])
    raw_txns     = data.get("transactions", [])
    budget       = data.get("budget", [])
    cat_spend    = data.get("categorySpending", [])
    net_worth    = data.get("netWorth", 0)
    income       = data.get("monthlyIncome", 0)
    spending     = data.get("monthlySpending", 0)
    cash_flow    = data.get("cashFlow", 0)
    total_debt   = data.get("totalDebt", 0)
    generated    = data.get("_generated", "")

    # Auto-assign colors per account type
    TYPE_COLORS = {
        "Chequing / Savings": "#22c55e",
        "Credit Card":        "#ef4444",
        "Investment":         "#3b82f6",
        "Savings":            "#6366f1",
        "Crypto":             "#f59e0b",
        "International":      "#8b5cf6",
        "Loan":               "#f87171",
        "Other":              "#71717a",
    }

    # Build account id → name map for txn rendering
    acc_name_map = {a["id"]: a["name"] for a in raw_accounts}

    # Normalize accounts to match mock field names
    accounts = []
    for a in raw_accounts:
        color = TYPE_COLORS.get(a.get("type", ""), "#71717a")
        init  = (a.get("name") or "?")[0].upper()
        accounts.append({
            "id":    a["id"],
            "name":  a["name"],
            "inst":  a.get("inst", a["name"]),
            "bal":   a["balance"],           # mock uses "bal"
            "type":  a.get("type", ""),
            "delta": 0,                      # no delta available yet
            "sync":  a.get("sync", "just now"),
            "color": color,
            "init":  init,
        })

    # Normalize transactions to match mock field names
    txns = []
    for t in raw_txns:
        amt = t["amount"]
        # Plaid: positive = money out (debit). Make debit negative for display
        display_amt = -abs(amt) if amt > 0 else abs(amt)
        txns.append({
            "name":     t["name"],
            "merchant": t["name"],
            "amt":      round(display_amt, 2),
            "date":     t["date"],               # human-readable "Jun 08"
            "date_iso": t.get("date_iso") or t["date"],  # ISO YYYY-MM-DD from generate_data
            "cat":      t["category"],
            "acc":      acc_name_map.get(t["account"], t["account"]),
            "ico":      t.get("ico", "folder"),
        })

    # Budget: already matches mock format (cat, spent, limit, ico)
    # just rename "limit" → "lim" to match mock
    budget_js = [{"cat": b["cat"], "spent": b["spent"], "lim": b["limit"], "ico": b["ico"]} for b in budget]

    # Category chart
    cat_labels  = [c["cat"] for c in cat_spend]
    cat_amounts = [c["amt"] for c in cat_spend]

    return f"""const D = {{
  // ── Real data injected by Vault/Flask ({generated}) ──
  netWorth:        {net_worth},
  totalAssets:     {data.get("totalAssets", 0)},
  totalDebt:       {total_debt},
  income:          {income},
  spending:        {spending},
  cashFlow:        {cash_flow},
  generated:       {json.dumps(generated)},

  netWorthHistory: [{net_worth}],
  monthLabels:     ["Now"],

  accounts: {json.dumps(accounts, indent=2)},
  txns: {json.dumps(txns[:200], indent=2)},
  budget: {json.dumps(budget_js, indent=2)},
  catLabels: {json.dumps(cat_labels)},
  catAmounts: {json.dumps(cat_amounts)},

  // Life data (manual / future integrations)
  investments: [],
  investHistory: [],
  crypto: [],
  habits: [],
  gym: [],
  gymDays: [],
  meals: [],
  bills: [],
}};"""

# ── Setup page HTML ───────────────────────────────────────────────────────────
SETUP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vault — Setup</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',sans-serif;background:#080808;color:#f4f4f5;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
.card{background:#0f0f0f;border:1px solid rgba(255,255,255,.07);border-radius:16px;padding:40px;width:100%;max-width:480px}
.logo{display:flex;align-items:center;gap:10px;margin-bottom:28px}
.logo-mark{width:28px;height:28px;background:#f4f4f5;border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;color:#080808}
.logo-name{font-size:16px;font-weight:600;color:#f4f4f5}
h2{font-size:18px;font-weight:600;margin-bottom:4px;letter-spacing:-.3px}
.sub{font-size:13px;color:#71717a;margin-bottom:28px;line-height:1.5}
.section{margin-bottom:24px}
.sec-label{font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:#3f3f46;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid rgba(255,255,255,.04)}
label{display:block;font-size:12px;color:#71717a;margin-bottom:4px;margin-top:12px}
input,select{width:100%;background:#161616;border:1px solid rgba(255,255,255,.07);border-radius:8px;padding:9px 12px;color:#f4f4f5;font-family:'Inter',sans-serif;font-size:13px;outline:none;transition:border-color .15s}
input:focus,select:focus{border-color:rgba(255,255,255,.14)}
input::placeholder{color:#3f3f46}
.optional{font-size:10px;color:#3f3f46;margin-left:4px}
.btn{width:100%;margin-top:28px;padding:12px;background:#f4f4f5;border:none;border-radius:8px;color:#080808;font-family:'Inter',sans-serif;font-size:14px;font-weight:600;cursor:pointer;transition:opacity .15s}
.btn:hover{opacity:.88}
.btn:disabled{opacity:.4;cursor:not-allowed}
.error{background:rgba(248,113,113,.08);border:1px solid rgba(248,113,113,.2);border-radius:8px;padding:10px 14px;font-size:13px;color:#f87171;margin-bottom:20px}
a{color:#60a5fa}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <div class="logo-mark">V</div>
    <span class="logo-name">Vault</span>
  </div>
  <h2>Connect your accounts</h2>
  <p class="sub">Your credentials are saved locally on your machine only — never sent to any server.</p>

  {% if error %}
  <div class="error">{{ error }}</div>
  {% endif %}

  <form method="POST" action="/setup" id="form">
    <div class="section">
      <div class="sec-label">Plaid — Required</div>
      <label>Client ID</label>
      <input name="plaid_client" placeholder="6a148508..." required value="{{ vals.plaid_client or '' }}">
      <label>Secret</label>
      <input name="plaid_secret" type="password" placeholder="e953c3d2..." required value="{{ vals.plaid_secret or '' }}">
      <label>Access Token</label>
      <input name="plaid_token" placeholder="access-production-..." required value="{{ vals.plaid_token or '' }}">
      <label>Environment</label>
      <select name="plaid_env">
        <option value="production" {% if vals.plaid_env != 'sandbox' %}selected{% endif %}>Production</option>
        <option value="sandbox" {% if vals.plaid_env == 'sandbox' %}selected{% endif %}>Sandbox</option>
      </select>
      <label>Start Date <span class="optional">transactions since</span></label>
      <input name="start_date" placeholder="2025-01-01" value="{{ vals.start_date or '2025-01-01' }}">
    </div>

    <div class="section">
      <div class="sec-label">Wise <span class="optional">Optional</span></div>
      <label>API Token</label>
      <input name="wise_token" placeholder="932aba85-..." value="{{ vals.wise_token or '' }}">
      <label>Profile ID</label>
      <input name="wise_profile" placeholder="63963106" value="{{ vals.wise_profile or '' }}">
    </div>

    <div class="section">
      <div class="sec-label">Options</div>
      <label>USD → CAD rate</label>
      <input name="usd_to_cad" placeholder="1.38" value="{{ vals.usd_to_cad or '1.38' }}">
    </div>

    <button type="submit" class="btn" id="submit-btn">Connect & Open Dashboard</button>
  </form>
  <script>
    document.getElementById('form').addEventListener('submit',function(){
      const btn=document.getElementById('submit-btn');
      btn.disabled=true;btn.textContent='Connecting...';
    });
  </script>
</div>
</body>
</html>"""

# ── Loading page ──────────────────────────────────────────────────────────────
LOADING_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Vault — Loading</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',sans-serif;background:#080808;color:#f4f4f5;display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;gap:16px}
.logo-mark{width:36px;height:36px;background:#f4f4f5;border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:16px;font-weight:700;color:#080808;margin-bottom:8px}
.spinner{width:20px;height:20px;border:2px solid rgba(255,255,255,.08);border-top-color:rgba(255,255,255,.4);border-radius:50%;animation:spin 0.8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.msg{font-size:14px;color:#71717a}
.submsg{font-size:12px;color:#3f3f46}
</style>
<script>
function poll(){
  fetch('/api/status').then(r=>r.json()).then(d=>{
    if(d.status==='ready'){window.location='/';}
    else if(d.status==='error'){window.location='/setup?error='+encodeURIComponent(d.error);}
    else{document.querySelector('.submsg').textContent=d.msg||'';setTimeout(poll,1200);}
  }).catch(()=>setTimeout(poll,2000));
}
window.addEventListener('DOMContentLoaded',poll);
</script>
</head>
<body>
  <div class="logo-mark">V</div>
  <div class="spinner"></div>
  <div class="msg">Connecting to your accounts</div>
  <div class="submsg">Plaid · Wise · Solana</div>
</body>
</html>"""

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    with _state_lock:
        status = _state["status"]
        data   = _state["data"]

    if status == "loading" and data is None:
        return render_template_string(LOADING_HTML)

    if data is None:
        # No config saved yet
        if load_config() is None:
            return redirect(url_for('setup'))
        # Config exists but data not loaded yet
        return render_template_string(LOADING_HTML)

    return build_vault_html(data)


@app.route('/setup', methods=['GET', 'POST'])
def setup():
    error = request.args.get('error', '')

    # Pre-fill from saved config if exists
    saved = load_config() or {}
    vals  = saved

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
        vals = config

        if not config['plaid_client'] or not config['plaid_secret'] or not config['plaid_token']:
            return render_template_string(SETUP_HTML,
                error='Plaid Client ID, Secret and Access Token are required.',
                vals=vals)

        # Save to disk permanently
        save_config(config)

        # Start fetch in background
        t = threading.Thread(target=fetch_data, args=(config,), daemon=True)
        t.start()

        return render_template_string(LOADING_HTML)

    return render_template_string(SETUP_HTML, error=error, vals=vals)


@app.route('/api/status')
def api_status():
    with _state_lock:
        return jsonify({
            'status':    _state['status'],
            'msg':       _state['msg'],
            'error':     _state['error'],
            'loaded_at': _state['loaded_at'],
        })


@app.route('/api/data')
def api_data():
    """Return current data as JSON (for debugging / future use)."""
    with _state_lock:
        data = _state['data']
    if not data:
        return jsonify({'error': 'No data loaded'}), 404
    return jsonify(data)


@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    """Force re-fetch from APIs."""
    config = load_config()
    if not config:
        return jsonify({'ok': False, 'msg': 'No config saved'}), 400
    t = threading.Thread(target=fetch_data, args=(config,), daemon=True)
    t.start()
    return jsonify({'ok': True, 'msg': 'Refresh started'})


@app.route('/api/plaid/link_token', methods=['POST'])
def plaid_link_token():
    """Create a Plaid Link token to initialize Plaid Link."""
    import requests as req
    config = load_config()
    if not config:
        return jsonify({'ok': False, 'error': 'No config'}), 400
    _env = config.get('plaid_env', 'production')
    base = ('https://production.plaid.com' if _env == 'production'
            else 'https://development.plaid.com' if _env == 'development'
            else 'https://sandbox.plaid.com')
    try:
        r = req.post(f'{base}/link/token/create', json={
            'client_id': config['plaid_client'],
            'secret':    config['plaid_secret'],
            'client_name': 'Vault',
            'country_codes': ['CA', 'US'],
            'language': 'en',
            'user': {'client_user_id': 'vault-user'},
            'products': ['transactions'],
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
        return jsonify({'ok': True, 'link_token': data['link_token']})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/plaid/exchange', methods=['POST'])
def plaid_exchange():
    """Exchange public token for access token and save it."""
    import requests as req
    config = load_config()
    if not config:
        return jsonify({'ok': False, 'error': 'No config'}), 400
    body = request.get_json() or {}
    public_token = body.get('public_token')
    if not public_token:
        return jsonify({'ok': False, 'error': 'Missing public_token'}), 400
    _env = config.get('plaid_env', 'production')
    base = ('https://production.plaid.com' if _env == 'production'
            else 'https://development.plaid.com' if _env == 'development'
            else 'https://sandbox.plaid.com')
    try:
        r = req.post(f'{base}/item/public_token/exchange', json={
            'client_id':    config['plaid_client'],
            'secret':       config['plaid_secret'],
            'public_token': public_token,
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
        access_token = data['access_token']
        # Save the new token
        config['plaid_token'] = access_token
        save_config(config)
        # Trigger refresh
        t = threading.Thread(target=fetch_data, args=(config,), daemon=True)
        t.start()
        return jsonify({'ok': True, 'msg': 'Account connected! Refreshing data...'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/logout')
def logout():
    CONFIG_FILE.unlink(missing_ok=True)
    CACHE_FILE.unlink(missing_ok=True)
    with _state_lock:
        _state['data']   = None
        _state['status'] = 'idle'
    return redirect(url_for('setup'))


if __name__ == '__main__':
    port  = int(os.getenv('PORT', 5050))
    debug = os.getenv('FLASK_DEBUG', '0') == '1'
    print(f"\n💰 Vault → http://localhost:{port}\n")
    startup()
    app.run(host='0.0.0.0', port=port, debug=debug, use_reloader=False)
