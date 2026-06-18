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
from flask import Flask, request, session, redirect, url_for, render_template_string, jsonify, send_from_directory, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from authlib.integrations.flask_client import OAuth
from datetime import datetime, timedelta
import jwt as pyjwt
from pathlib import Path
from models import db, User as UserModel, PlaidConnection, WiseConnection, CryptoWallet

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "vault-local-secret-do-not-deploy")

# Handle DATABASE_URL: Railway gives postgres:// but SQLAlchemy needs postgresql://
_database_url = os.getenv('DATABASE_URL', 'sqlite:///fiscit.db')
if _database_url.startswith('postgres://'):
    _database_url = _database_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = _database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}

db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# ── Google OAuth setup ──────────────────────────────────────────────────────
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.getenv('GOOGLE_CLIENT_ID', ''),
    client_secret=os.getenv('GOOGLE_CLIENT_SECRET', ''),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'},
)

@login_manager.user_loader
def load_user(user_id):
    user = db.session.get(UserModel, int(user_id))
    if user:
        return FlaskUser(user)
    return None

class FlaskUser(UserMixin):
    def __init__(self, user_model):
        self.model = user_model
    @property
    def id(self): return str(self.model.id)
    @property
    def email(self): return self.model.email
    @property
    def name(self): return self.model.name
    def get_id(self): return str(self.model.id)

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
CONFIG_FILE  = BASE_DIR / "saved_config.json"
VAULT_HTML   = BASE_DIR / "vault.html"
CACHE_FILE   = BASE_DIR / "data_cache.json"

# ── Per-user state ────────────────────────────────────────────────────────────
_user_states = {}  # {user_id: {status, msg, error, data, loaded_at}}
_user_state_lock = threading.Lock()

def _get_state(user_id):
    """Get or create state dict for a user."""
    if user_id not in _user_states:
        _user_states[user_id] = {
            "status": "idle",
            "msg": "",
            "error": "",
            "data": None,
            "loaded_at": None,
        }
    return _user_states[user_id]

# ── Build config dict for a user from DB ────────────────────────────────────
def build_user_config(user):
    """Build the config dict for generate_data.pull_all() from DB records."""
    cfg = {
        'plaid_client': os.getenv('PLAID_CLIENT_ID', ''),
        'plaid_secret': os.getenv('PLAID_SECRET', ''),
        'plaid_env': os.getenv('PLAID_ENV', 'production'),
        'plaid_token': '',
        'wise_token': '',
        'wise_profile': '',
        'usd_to_cad': '1.38',
        'start_date': '2025-01-01',
        'wallets': [],
    }
    # Plaid connection (first active one)
    pc = PlaidConnection.query.filter_by(user_id=user.model.id).first()
    if pc:
        cfg['plaid_token'] = pc.access_token
    # Wise connection
    wc = WiseConnection.query.filter_by(user_id=user.model.id).first()
    if wc:
        cfg['wise_token'] = wc.api_token
        profile = wc.profile_id or ''
        # Filter out placeholder values
        if profile in ('your_wise_profile_id', 'your_wise_api_token', ''):
            profile = ''
        cfg['wise_profile'] = profile
    # Crypto wallets
    wallets = CryptoWallet.query.filter_by(user_id=user.model.id).all()
    cfg['wallets'] = [{'chain': w.chain, 'address': w.address, 'label': w.label} for w in wallets]
    return cfg

def _has_any_account(config):
    """Check if config has at least one account source."""
    return bool(
        config.get("plaid_token") or
        config.get("wise_token") or
        (config.get("wallets") and len(config["wallets"]) > 0)
    )

# ── Background data fetch (per user) ────────────────────────────────────────
def fetch_data(user_id, config):
    """Pull all real data in background thread for a specific user."""
    state = _get_state(user_id)
    def status_cb(msg):
        with _user_state_lock:
            state["msg"] = msg
        print(f"  [user {user_id}: {msg}]")

    if not _has_any_account(config):
        with _user_state_lock:
            state["data"]   = {"_generated": "empty", "accounts": [], "net_worth": 0}
            state["status"] = "ready"
            state["msg"]    = "No accounts connected"
        return

    config["\u005fstatus_cb"] = status_cb

    try:
        with _user_state_lock:
            state["status"] = "loading"
            state["msg"]    = "Starting..."
            state["error"]  = ""

        import generate_data
        data = generate_data.pull_all(config)

        with _user_state_lock:
            state["data"]      = data
            state["status"]    = "ready"
            state["loaded_at"] = datetime.now().isoformat()
            state["msg"]       = f"Last synced: {datetime.now().strftime('%H:%M')}"

    except Exception as e:
        import traceback
        with _user_state_lock:
            state["status"] = "error"
            state["error"]  = str(e)
        print(f"[error] fetch_data user {user_id}: {e}")
        traceback.print_exc()

# ── On startup: create DB tables + migrate ──────────────────────────────────
with app.app_context():
    try:
        db.create_all()
        print("  DB tables ready")
        # Migrate: add 'onboarded' column if missing (Postgres)
        from sqlalchemy import text, inspect as sa_inspect
        inspector = sa_inspect(db.engine)
        existing_cols = [c['name'] for c in inspector.get_columns('users')]
        if 'onboarded' not in existing_cols:
            db.session.execute(text('ALTER TABLE users ADD COLUMN onboarded BOOLEAN NOT NULL DEFAULT FALSE'))
            db.session.commit()
            print("  Migrated: added users.onboarded")
        if 'google_id' not in existing_cols:
            db.session.execute(text('ALTER TABLE users ADD COLUMN google_id VARCHAR(255) UNIQUE'))
            db.session.commit()
            print("  Migrated: added users.google_id")
        # Make password_hash nullable for Google-only users
        if 'password_hash' in existing_cols:
            try:
                db.session.execute(text('ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL'))
                db.session.commit()
                print("  Migrated: users.password_hash now nullable")
            except Exception:
                pass  # already nullable
        # Verify connection
        result = db.session.execute(db.text('SELECT 1'))
        print(f"  DB connection OK: {result.scalar()}")
    except Exception as e:
        print(f"  DB ERROR: {e}")
        raise

# ── Vault HTML with data injection ───────────────────────────────────────────
def _normalize_api_data(data):
    """Normalize raw API data field names to match vault.html mock format.
    Raw data uses 'balance', 'amount', 'category', 'institution' etc.
    Vault.html expects 'bal', 'amt', 'cat', 'inst' etc.
    """
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
    out = dict(data)
    # Normalize accounts
    raw_accs = data.get("accounts", [])
    norm_accs = []
    for a in raw_accs:
        color = TYPE_COLORS.get(a.get("type", ""), "#71717a")
        init = (a.get("name") or "?")[0].upper()
        norm_accs.append({
            "id":    a.get("id", ""),
            "name":  a.get("name", ""),
            "inst":  a.get("inst", a.get("institution", a.get("name", ""))),
            "bal":   a.get("bal", a.get("balance", 0)),
            "type":  a.get("type", ""),
            "delta": a.get("delta", 0),
            "sync":  a.get("sync", "just now"),
            "color": a.get("color", color),
            "init":  a.get("init", init),
            "ico":   a.get("ico", ""),
        })
    out["accounts"] = norm_accs
    # Normalize transactions
    raw_txns = data.get("transactions", [])
    norm_txns = []
    for t in raw_txns:
        amt = t.get("amt", t.get("amount", 0))
        if isinstance(amt, (int, float)) and amt > 0:
            amt = -abs(amt)  # debit display convention
        norm_txns.append({
            "name":     t.get("name", ""),
            "merchant": t.get("merchant", t.get("name", "")),
            "amt":      round(amt, 2) if isinstance(amt, (int, float)) else 0,
            "date":     t.get("date", ""),
            "date_iso": t.get("date_iso", t.get("date", "")),
            "cat":      t.get("cat", t.get("category", "")),
            "ico":      t.get("ico", ""),
            "account":  t.get("account", t.get("_account", "")),
        })
    out["transactions"] = norm_txns
    return out

def build_vault_html(data, user=None):
    """Read vault.html and inject real data by replacing the mock D = {...} block."""
    if not VAULT_HTML.exists():
        return "<h1>vault.html not found</h1>"

    html = VAULT_HTML.read_text(encoding="utf-8")

    # Build real data JS object
    real_js = _build_real_data_js(data, user=user)

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

def _build_real_data_js(data, user=None):
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

    # Connection status derived from data presence
    connections = {
        'plaid': any(a.get('type', '') in ('Chequing / Savings', 'Credit Card', 'Investment', 'Loan') for a in raw_accounts),
        'wise': any(a.get('id', '').startswith('wise_') for a in raw_accounts),
        'crypto': bool(data.get('crypto_balances') or data.get('wallets')),
        'wealthsimple': False,
        'kraken': False,
    }

    # Wallets list for frontend (from data input, not old config)
    wallets_list = data.get('wallets', [])

    # Use real user name/email from DB, not institution name
    user_name = user.name if user and user.name else ''
    user_email = user.email if user and user.email else ''
    # Fallback: use institution name only if user has no name set
    if not user_name and accounts:
        user_name = accounts[0].get('inst', accounts[0].get('name', ''))

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

  // User profile
  userName:  {json.dumps(user_name)},
  userEmail: {json.dumps(user_email)},

  // Connection status (derived from config)
  connections: {json.dumps(connections)},

  // Wallets list (for frontend wallet management)
  wallets: {json.dumps(wallets_list)},

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

# ── Loading page ──────────────────────────────────────────────────────────────
LOADING_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Fiscit — Loading</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',sans-serif;background:#080808;color:#f4f4f5;display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;gap:16px}
.spinner{width:20px;height:20px;border:2px solid #2a2a2a;border-top-color:#4ade80;border-radius:50%;animation:spin 0.8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.msg{font-size:0.85rem;color:#71717a}
.submsg{font-size:0.75rem;color:#3f3f46}
</style>
<script>
function poll(){
  fetch('/api/status').then(r=>r.json()).then(d=>{
    if(d.status==='ready'){window.location='/';}
    else if(d.status==='error'){window.location='/setup?error='+encodeURIComponent(d.error);}
    else if(d.status==='idle'){window.location='/setup';}
    else{document.querySelector('.submsg').textContent=d.msg||'';setTimeout(poll,1200);}
  }).catch(()=>setTimeout(poll,2000));
}
window.addEventListener('DOMContentLoaded',poll);
</script>
</head>
<body>
  <svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" width="36" height="36">
    <rect width="32" height="32" rx="8" fill="#0A0F1A"/>
    <rect x="7" y="6" width="5" height="20" rx="2" fill="#F0F4F8"/>
    <rect x="7" y="6" width="16" height="5" rx="2" fill="#F0F4F8"/>
    <rect x="7" y="14" width="12" height="4" rx="2" fill="#F0F4F8"/>
    <circle cx="26" cy="8.5" r="3.5" fill="#b8f566"/>
  </svg>
  <div class="spinner"></div>
  <div class="msg">Connecting to your accounts</div>
  <div class="submsg">Plaid · Wise · Solana</div>
</body>
</html>"""

# ── Auth routes ──────────────────────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fiscit — Log In</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',sans-serif;background:#080808;color:#f4f4f5;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
.card{background:#111;border:1px solid #222;border-radius:16px;padding:2.5rem;width:100%;max-width:400px}
.header{display:flex;align-items:center;gap:10px;margin-bottom:2rem}
.brand{font-size:1.25rem;font-weight:700;color:#4ade80;letter-spacing:-0.02em}
.title{font-size:1.1rem;font-weight:600;margin-bottom:1.5rem;letter-spacing:-0.2px}
label{display:block;font-size:0.75rem;font-weight:500;color:#a1a1aa;margin-bottom:4px;text-transform:uppercase;letter-spacing:0.04em}
input{width:100%;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:8px;padding:0.65rem 0.85rem;color:#f4f4f5;font-family:'Inter',sans-serif;font-size:0.85rem;outline:none;transition:border-color 0.15s;margin-bottom:1rem}
input:focus{border-color:#4ade80}
.btn{width:100%;padding:0.85rem;background:#4ade80;border:none;border-radius:8px;color:#080808;font-family:'Inter',sans-serif;font-size:0.9rem;font-weight:700;cursor:pointer;transition:opacity 0.15s}
.btn:hover{opacity:0.9}
.btn-google{width:100%;padding:0.85rem;background:#fff;border:1px solid #ddd;border-radius:8px;color:#333;font-family:'Inter',sans-serif;font-size:0.9rem;font-weight:600;cursor:pointer;transition:background 0.15s;display:flex;align-items:center;justify-content:center;gap:10px}
.btn-google:hover{background:#f5f5f5}
.btn-google svg{width:18px;height:18px}
.divider{display:flex;align-items:center;gap:12px;margin:1.25rem 0;color:#555;font-size:0.8rem}
.divider::before,.divider::after{content:'';flex:1;border-top:1px solid #2a2a2a}
.error{background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.25);border-radius:8px;padding:0.65rem 0.85rem;font-size:0.85rem;color:#f87171;margin-bottom:1.25rem}
a{color:#4ade80;text-decoration:none;font-size:0.85rem}
.link{text-align:center;margin-top:1.25rem}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" width="32" height="32">
      <rect width="32" height="32" rx="8" fill="#0A0F1A"/>
      <rect x="7" y="6" width="5" height="20" rx="2" fill="#F0F4F8"/>
      <rect x="7" y="6" width="16" height="5" rx="2" fill="#F0F4F8"/>
      <rect x="7" y="14" width="12" height="4" rx="2" fill="#F0F4F8"/>
      <circle cx="26" cy="8.5" r="3.5" fill="#b8f566"/>
    </svg>
    <span class="brand">Fiscit</span>
  </div>
  <div class="title">Log in</div>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <a href="/login/google" class="btn-google">
    <svg viewBox="0 0 24 24"><path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z"/><path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/><path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18A11.96 11.96 0 0 0 1 12c0 1.94.46 3.77 1.18 5.39l3.66-2.84z"/><path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/></svg>
    Sign in with Google
  </a>
  <div class="divider">or</div>
  <form method="POST">
    <label>Email</label>
    <input type="email" name="email" placeholder="you@example.com" required>
    <label>Password</label>
    <input type="password" name="password" placeholder="Your password" required>
    <button type="submit" class="btn">Log in</button>
  </form>
  <div class="link">Don't have an account? <a href="/register">Sign up</a></div>
</div>
</body>
</html>"""

REGISTER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fiscit — Get Started</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',sans-serif;background:#080808;color:#f4f4f5;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
.card{background:#111;border:1px solid #222;border-radius:16px;padding:2.5rem;width:100%;max-width:440px;transition:opacity 0.3s}
.header{display:flex;align-items:center;gap:10px;margin-bottom:1.5rem}
.brand{font-size:1.25rem;font-weight:700;color:#4ade80;letter-spacing:-0.02em}
.step-dots{display:flex;gap:6px;margin-bottom:1.5rem;justify-content:center}
.dot{width:8px;height:8px;border-radius:50%;background:#2a2a2a;transition:all 0.3s}
.dot.active{background:#4ade80;width:24px;border-radius:4px}
.dot.done{background:#4ade80}
.title{font-size:1.15rem;font-weight:600;margin-bottom:0.3rem;letter-spacing:-0.3px}
.sub{font-size:0.85rem;color:#71717a;margin-bottom:1.5rem;line-height:1.6}
label{display:block;font-size:0.7rem;font-weight:600;color:#a1a1aa;margin-bottom:4px;margin-top:12px;text-transform:uppercase;letter-spacing:0.06em}
input,select{width:100%;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:8px;padding:0.7rem 0.85rem;color:#f4f4f5;font-family:'Inter',sans-serif;font-size:0.85rem;outline:none;transition:border-color 0.15s}
input:focus,select:focus{border-color:#4ade80}
input::placeholder{color:#3f3f46}
.btn{width:100%;margin-top:1.5rem;padding:0.85rem;background:#4ade80;border:none;border-radius:8px;color:#080808;font-family:'Inter',sans-serif;font-size:0.9rem;font-weight:700;cursor:pointer;transition:opacity 0.15s}
.btn:hover{opacity:0.9}
.btn:disabled{opacity:0.4;cursor:not-allowed}
.btn-google{width:100%;margin-top:1rem;padding:0.85rem;background:#fff;border:1px solid #ddd;border-radius:8px;color:#333;font-family:'Inter',sans-serif;font-size:0.9rem;font-weight:600;cursor:pointer;transition:background 0.15s;display:flex;align-items:center;justify-content:center;gap:10px}
.btn-google:hover{background:#f5f5f5}
.btn-google svg{width:18px;height:18px}
.divider{display:flex;align-items:center;gap:12px;margin:1rem 0;color:#555;font-size:0.8rem}
.divider::before,.divider::after{content:'';flex:1;border-top:1px solid #2a2a2a}
.btn-outline{width:100%;margin-top:0.75rem;padding:0.75rem;background:transparent;border:1px solid #2a2a2a;border-radius:8px;color:#71717a;font-family:'Inter',sans-serif;font-size:0.85rem;font-weight:500;cursor:pointer;transition:all 0.15s}
.btn-outline:hover{border-color:#4ade80;color:#4ade80}
.error{background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.25);border-radius:8px;padding:0.65rem 0.85rem;font-size:0.85rem;color:#f87171;margin-bottom:1.25rem}
a{color:#4ade80;text-decoration:none}
.link{text-align:center;margin-top:1.25rem;font-size:0.85rem;color:#71717a}
.account-card{display:flex;align-items:center;gap:12px;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:12px;padding:14px 16px;cursor:pointer;transition:all 0.15s;margin-bottom:10px}
.account-card:hover{border-color:#4ade80}
.account-icon{width:40px;height:40px;border-radius:10px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.account-icon.bank{background:#1a2e1a}
.account-icon.wise{background:#1a1a2e}
.account-icon.crypto{background:#2e1a1a}
.account-info{flex:1}
.account-name{font-size:0.9rem;font-weight:600;color:#f4f4f5}
.account-desc{font-size:0.75rem;color:#71717a;margin-top:2px}
.account-action{font-size:0.75rem;font-weight:600;color:#4ade80;text-transform:uppercase;letter-spacing:0.05em}
.account-card.connected{border-color:#1a3a1a;background:#0d1a0d}
.account-card.connected .account-action{color:#4ade80}
.account-card.connected .account-action::before{content:'\\2713 '}
.hidden{display:none!important}
.success-card{text-align:center;padding:2rem 0}
.success-icon{width:64px;height:64px;border-radius:50%;background:#1a2e1a;display:inline-flex;align-items:center;justify-content:center;margin-bottom:1rem}
.success-icon svg{color:#4ade80}
.skip{font-size:0.8rem;color:#3f3f46;text-align:center;margin-top:1rem;cursor:pointer}
.skip:hover{color:#71717a}
</style>
</head>
<body>
<div class="card" id="onboarding">

  <!-- Step indicators -->
  <div class="step-dots" id="dots">
    <div class="dot active" data-step="0"></div>
    <div class="dot" data-step="1"></div>
    <div class="dot" data-step="2"></div>
    <div class="dot" data-step="3"></div>
  </div>

  <!-- Step 0: Sign up (name + email + password) -->
  <div id="step-0">
    <div class="header">
      <svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" width="32" height="32">
        <rect width="32" height="32" rx="8" fill="#0A0F1A"/>
        <rect x="7" y="6" width="5" height="20" rx="2" fill="#F0F4F8"/>
        <rect x="7" y="6" width="16" height="5" rx="2" fill="#F0F4F8"/>
        <rect x="7" y="14" width="12" height="4" rx="2" fill="#F0F4F8"/>
        <circle cx="26" cy="8.5" r="3.5" fill="#b8f566"/>
      </svg>
      <span class="brand">Fiscit</span>
    </div>
    <div class="title">Create your account</div>
    <p class="sub">Your finances, finally clear.</p>
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
    <form method="POST" id="reg-form">
      <label>Name</label>
      <input type="text" name="name" placeholder="How should we call you?" required>
      <label>Email</label>
      <input type="email" name="email" placeholder="you@example.com" required>
      <label>Password</label>
      <input type="password" name="password" placeholder="At least 6 characters" required>
      <button type="submit" class="btn">Sign up</button>
    </form>
    <div class="divider">or</div>
    <a href="/login/google" class="btn-google">
      <svg viewBox="0 0 24 24"><path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z"/><path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/><path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18A11.96 11.96 0 0 0 1 12c0 1.94.46 3.77 1.18 5.39l3.66-2.84z"/><path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/></svg>
      Sign up with Google
    </a>
    <div class="link">Already have an account? <a href="/login">Log in</a></div>
  </div>

  <!-- Step 1: Bank (Plaid) -->
  <div id="step-1" class="hidden">
    <div class="title">Connect your bank</div>
    <p class="sub">Securely sync your accounts via Plaid. Read only, we cannot move money.</p>
    <div class="account-card" id="bank-card" onclick="showBankTrust()">
      <div class="account-icon bank">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><path d="M3 21h18M3 10h18M5 10V21M9 10V21M15 10V21M19 10V21M3 10l9-7 9 7" stroke="#4ade80" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </div>
      <div class="account-info">
        <div class="account-name">Bank Account</div>
        <div class="account-desc" id="bank-status">Plaid, used by millions</div>
      </div>
      <div class="account-action" id="bank-action">Connect</div>
    </div>
    <div id="bank-trust" class="hidden" style="margin-top:-10px;margin-bottom:10px;padding:14px 16px;background:#0f1a0f;border:1px solid #1a3a1a;border-radius:0 0 12px 12px">
      <div style="font-size:0.85rem;font-weight:600;color:#4ade80;margin-bottom:10px">Your data is safe</div>
      <div style="font-size:0.8rem;color:#a1a1aa;line-height:1.8">Bank grade 256-bit encryption.<br>Read only, we cannot move money.<br>Used by millions via Plaid.</div>
      <button type="button" class="btn" style="margin-top:12px" onclick="openPlaidLink()">Continue to Plaid</button>
      <div style="text-align:center;margin-top:8px;font-size:0.7rem;color:#3f3f46">Powered by <a href="https://plaid.com" target="_blank">Plaid</a></div>
    </div>
    <button type="button" class="btn-outline" onclick="goStep(2)">Skip for now</button>
  </div>

  <!-- Step 1: Crypto -->
  <div id="step-2" class="hidden">
    <div class="title">Add crypto wallets</div>
    <p class="sub">Track BTC, ETH, SOL, USDT, USDC and more.</p>
    <div class="account-card" onclick="document.getElementById('crypto-form').classList.toggle('hidden')">
      <div class="account-icon crypto">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="10" fill="#F7931A"/><path d="M14.5 10.5c0-1.1-.8-1.6-1.8-1.8V7.5h-1.4v1.1c-1 .2-1.8.8-1.8 1.9 0 1.6 1.4 1.6 2.8 1.9.8.2 1.2.5 1.2 1.1 0 .7-.6 1.1-1.4 1.1s-1.4-.4-1.5-1.2l-1.3.3c.2 1.2 1 1.8 2 2v1.2h1.4v-1.2c1.1-.2 1.9-.9 1.9-2 0-1.6-1.4-1.7-2.8-2-.8-.2-1.2-.4-1.2-1 0-.5.5-.9 1.2-.9.6 0 1.1.3 1.2.9l1.3-.3z" fill="#fff"/></svg>
      </div>
      <div class="account-info">
        <div class="account-name">Crypto Wallet</div>
        <div class="account-desc" id="crypto-status">Optional, add anytime</div>
      </div>
      <div class="account-action">Add</div>
    </div>
    <div id="crypto-form" class="hidden" style="padding:12px 16px;background:#161616;border:1px solid #2a2a2a;border-radius:12px;margin-top:-10px;margin-bottom:10px">
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <select id="wallet-chain" style="width:35%;margin:0">
          <option value="bitcoin">Bitcoin</option>
          <option value="ethereum">Ethereum</option>
          <option value="solana">Solana</option>
          <option value="polygon">Polygon</option>
          <option value="base">Base</option>
          <option value="usdc">USDC</option>
        </select>
        <input id="wallet-address" placeholder="Wallet address" style="width:65%;margin:0">
      </div>
      <input id="wallet-label" placeholder="Label (optional)" style="margin-top:8px">
      <button type="button" style="width:100%;margin-top:10px;padding:8px;background:#1a2e1a;border:1px solid #1a3a1a;border-radius:8px;color:#4ade80;font-family:'Inter',sans-serif;font-size:0.8rem;font-weight:600;cursor:pointer" onclick="addWallet()">Add Wallet</button>
      <div id="wallet-list" style="margin-top:8px"></div>
    </div>
    <button type="button" class="btn-outline" onclick="goStep(3)">Skip for now</button>
  </div>

  <!-- Step 2: Wise + Done -->
  <div id="step-3" class="hidden">
    <div class="title">International accounts?</div>
    <p class="sub">Connect Wise for multi-currency transfers, or skip to start using Fiscit.</p>
    <div class="account-card" onclick="document.getElementById('wise-form').classList.toggle('hidden')">
      <div class="account-icon wise">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" stroke="#9FE870" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </div>
      <div class="account-info">
        <div class="account-name">Wise</div>
        <div class="account-desc" id="wise-status">International transfers</div>
      </div>
      <div class="account-action" id="wise-action">Set up</div>
    </div>
    <div id="wise-form" class="hidden" style="padding:12px 16px;background:#161616;border:1px solid #2a2a2a;border-radius:12px;margin-top:-10px;margin-bottom:10px">
      <label style="margin-top:0">API Token</label>
      <input id="wise-token" placeholder="Your Wise API token">
      <label>Profile ID</label>
      <input id="wise-profile" placeholder="Your profile ID">
      <button type="button" style="width:100%;margin-top:10px;padding:8px;background:transparent;border:1px solid #4ade80;border-radius:8px;color:#4ade80;font-family:'Inter',sans-serif;font-size:0.8rem;font-weight:600;cursor:pointer" onclick="saveWise()">Save and Test</button>
      <div id="wise-result" style="margin-top:8px"></div>
    </div>
    <button type="button" class="btn" onclick="finishOnboarding()" style="margin-top:0.5rem">You're all set!</button>
    <div class="skip" onclick="finishOnboarding()">Skip, I'll add later</div>
  </div>

  <!-- Success animation -->
  <div id="step-done" class="hidden">
    <div class="success-card">
      <div class="success-icon">
        <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#4ade80" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>
      </div>
      <div class="title" style="margin-bottom:0.5rem">You're all set!</div>
      <p class="sub">Your financial life, finally clear.</p>
      <div style="margin-top:1rem"><div class="spinner" style="margin:0 auto"></div><div style="font-size:0.75rem;color:#3f3f46;margin-top:10px">Loading your dashboard...</div></div>
    </div>
  </div>

</div>

<style>.spinner{width:20px;height:20px;border:2px solid #2a2a2a;border-top-color:#4ade80;border-radius:50%;animation:spin 0.8s linear infinite;margin:0 auto}@keyframes spin{to{transform:rotate(360deg)}}</style>

<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
<script>
let currentStep = 0;
let userName = '';
let plaidConnected = false;

function updateDots(){
  document.querySelectorAll('.dot').forEach((d,i)=>{
    d.classList.remove('active','done');
    if(i < currentStep) d.classList.add('done');
    if(i === currentStep) d.classList.add('active');
  });
}

function showStep(n){
  document.querySelectorAll('[id^="step-"]').forEach(el=>el.classList.add('hidden'));
  const el = document.getElementById('step-'+n);
  if(el){el.classList.remove('hidden');updateDots();}
}

function goStep(n){
  currentStep = n;
  showStep(n);
}

// Step 0 handled by form POST (name+email+password)

// Step 1: Plaid
function showBankTrust(){document.getElementById('bank-trust').classList.toggle('hidden');}
function openPlaidLink(){
  fetch('/api/plaid/link_token',{method:'POST'}).then(r=>r.json()).then(d=>{
    if(!d.link_token){alert('Could not connect to Plaid. Try again later.');return;}
    const handler=Plaid.create({
      token:d.link_token,
      onSuccess:async(publicToken)=>{
        const res=await fetch('/api/plaid/exchange',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({public_token:publicToken})});
        const data=await res.json();
        if(data.ok){
          plaidConnected=true;
          document.getElementById('bank-card').classList.add('connected');
          document.getElementById('bank-status').textContent='Connected!';
          document.getElementById('bank-action').textContent='Connected';
          document.getElementById('bank-trust').classList.add('hidden');
          setTimeout(()=>goStep(2),800);
        } else { alert(data.error||'Failed to connect bank'); }
      },
      onExit:()=>{},
      onEvent:()=>{},
    });
    handler.open();
  }).catch(e=>alert('Error: '+e));
}

// Step 3: Crypto wallets
function addWallet(){
  const chain=document.getElementById('wallet-chain').value;
  const address=document.getElementById('wallet-address').value.trim();
  const label=document.getElementById('wallet-label').value.trim();
  if(!address){alert('Enter a wallet address');return;}
  fetch('/api/wallets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chain,address,label})})
    .then(r=>r.json()).then(d=>{
      if(d.ok){
        const list=document.getElementById('wallet-list');
        const div=document.createElement('div');
        div.style.cssText='display:flex;align-items:center;gap:8px;padding:6px 0;font-size:0.8rem';
        div.innerHTML='<span style="background:#1a2e1a;color:#4ade80;padding:2px 6px;border-radius:4px;font-size:0.65rem;font-weight:600;text-transform:uppercase">'+chain+'</span><span style="color:#a1a1aa;font-family:monospace;font-size:0.75rem">'+address.slice(0,8)+'...'+address.slice(-4)+'</span>';
        list.appendChild(div);
        document.getElementById('wallet-address').value='';
        document.getElementById('wallet-label').value='';
        document.getElementById('crypto-status').textContent=d.wallets+' wallet'+(d.wallets>1?'s':'');
      } else { alert(d.error||'Failed to add wallet'); }
    }).catch(e=>alert('Error: '+e));
}

// Step 4: Wise
function saveWise(){
  const token=document.getElementById('wise-token').value.trim();
  const profile=document.getElementById('wise-profile').value.trim();
  if(!token){document.getElementById('wise-result').innerHTML='<div style="color:#f87171;font-size:0.8rem">Enter your API token first.</div>';return;}
  document.getElementById('wise-result').innerHTML='<div style="color:#71717a;font-size:0.8rem">Testing...</div>';
  fetch('/api/wise/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({wise_token:token})})
    .then(r=>r.json()).then(d=>{
      if(d.ok){
        document.getElementById('wise-action').textContent='Connected';
        document.getElementById('wise-status').textContent='Connected!';
        document.getElementById('wise-result').innerHTML='<div style="color:#4ade80;font-size:0.8rem">Connected!</div>';
      } else {
        document.getElementById('wise-result').innerHTML='<div style="color:#f87171;font-size:0.8rem">'+(d.error||'Connection failed.')+'</div>';
      }
    }).catch(()=>{document.getElementById('wise-result').innerHTML='<div style="color:#f87171;font-size:0.8rem">Network error.</div>'});
}

function finishOnboarding(){
  // Mark onboarding complete (both localStorage and server-side)
  localStorage.setItem('fiscit_onboarded','1');
  fetch('/api/onboarding/complete',{method:'POST',credentials:'include'}).catch(()=>{});
  // Show success animation
  document.querySelectorAll('[id^="step-"]').forEach(el=>el.classList.add('hidden'));
  document.getElementById('step-done').classList.remove('hidden');
  document.getElementById('dots').classList.add('hidden');
  // Trigger data fetch in background then redirect
  fetch('/api/data',{credentials:'include'}).catch(()=>{});
  setTimeout(()=>{window.location.href='/';},2000);
}

// Auto-advance based on Jinja step parameter
{% if step == '1' %}
// User just registered, show Plaid step
currentStep = 1;
showStep(1);
{% elif step == '2' %}
currentStep = 2;
showStep(2);
{% elif step == '3' %}
currentStep = 3;
showStep(3);
{% elif error %}
// Stay on step 0 on error
{% endif %}
</script>
</body>
</html>"""

@app.route('/register', methods=['GET', 'POST'])
def register():
    # If already logged in, go to dashboard
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        name = request.form.get('name', '').strip()
        if not email or not password:
            return render_template_string(REGISTER_HTML, error='Email and password are required.', step='0')
        if len(password) < 6:
            return render_template_string(REGISTER_HTML, error='Password must be at least 6 characters.', step='0')
        if UserModel.query.filter_by(email=email).first():
            return render_template_string(REGISTER_HTML, error='An account with that email already exists.', step='0')
        user = UserModel(email=email, name=name)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flask_user = FlaskUser(user)
        login_user(flask_user)
        return redirect(url_for('index'))
    return render_template_string(REGISTER_HTML, error='', step='0')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = UserModel.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            return render_template_string(LOGIN_HTML, error='Invalid email or password.')
        login_user(FlaskUser(user))
        return redirect(url_for('index'))
    # Show error from Google OAuth redirect if any
    error = ''
    err_param = request.args.get('error')
    if err_param == 'google_failed':
        error = 'Google sign-in failed. Please try again.'
    elif err_param == 'no_google_data':
        error = 'Could not get your info from Google. Please try again.'
    return render_template_string(LOGIN_HTML, error=error)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# ── SSO: accept JWT from fiscit.com ──────────────────────────────────────
@app.route('/sso')
def sso():
    """Accept a JWT token from fiscit.com and create a Flask session."""
    token = request.args.get('token')
    if not token:
        return redirect(url_for('login'))
    try:
        payload = pyjwt.decode(token, os.getenv('JWT_SECRET', ''), algorithms=['HS256'])
        user_id = payload.get('sub')
        if not user_id:
            return redirect(url_for('login'))
        user = db.session.get(UserModel, int(user_id))
        if not user:
            return redirect(url_for('login'))
        login_user(FlaskUser(user))
        return redirect(url_for('index'))
    except Exception:
        return redirect(url_for('login'))

# ── Google OAuth routes ──────────────────────────────────────────────────────
@app.route('/login/google')
def google_login():
    """Initiate Google OAuth2 flow."""
    # Force HTTPS redirect URI — Railway terminates SSL so Flask sees http://
    redirect_uri = url_for('google_callback', _external=True, _scheme='https')
    return google.authorize_redirect(redirect_uri)

@app.route('/login/google/callback')
def google_callback():
    """Handle Google OAuth2 callback — create or log in user."""
    try:
        token = google.authorize_access_token()
    except Exception:
        return redirect(url_for('login', error='google_failed'))

    userinfo = token.get('userinfo') or google.userinfo()
    if not userinfo:
        # Fallback: parse id_token manually
        from authlib.jose import jwt as jose_jwt
        id_token_raw = token.get('id_token')
        if id_token_raw:
            userinfo = jose_jwt.decode(id_token_raw, claims_options={'iss': {'values': ['https://accounts.google.com', 'accounts.google.com']}})

    google_id = userinfo.get('sub')
    email = (userinfo.get('email') or '').strip().lower()
    name = (userinfo.get('name') or '').strip()

    if not google_id or not email:
        return redirect(url_for('login', error='no_google_data'))

    # Look up existing user by google_id or email
    user = UserModel.query.filter_by(google_id=google_id).first()
    if not user:
        user = UserModel.query.filter_by(email=email).first()
        if user:
            # Existing email/password user — link their Google account
            user.google_id = google_id
            db.session.commit()
        else:
            # Brand new user via Google
            user = UserModel(email=email, name=name, google_id=google_id)
            user.password_hash = None  # no password for Google-only users
            db.session.add(user)
            db.session.commit()

    login_user(FlaskUser(user))
    return redirect(url_for('index'))

# ── Main routes ─────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    """Health check endpoint for Railway."""
    try:
        db.session.execute(db.text('SELECT 1'))
        return 'OK', 200
    except Exception:
        return 'DB error', 503

@app.route('/')
@login_required
def index():
    uid = current_user.model.id
    state = _get_state(uid)
    with _user_state_lock:
        status = state["status"]
        data = state["data"]

    if status == "loading" and data is None:
        return render_template_string(LOADING_HTML)

    if status == "idle" or data is None:
        cfg = build_user_config(current_user)
        if status == "idle":
            if _has_any_account(cfg):
                t = threading.Thread(target=fetch_data, args=(uid, cfg), daemon=True)
                t.start()
                return render_template_string(LOADING_HTML)
            # No accounts yet — serve dashboard, JS will show onboarding prompt
            return build_vault_html(data or {}, user=current_user.model)
        return render_template_string(LOADING_HTML)

    return build_vault_html(data, user=current_user.model)

@app.route('/setup', methods=['GET'])
@login_required
def setup():
    # Always serve the vault dashboard; JS will show the appropriate panel
    return redirect(url_for('index'))

# ── API routes ──────────────────────────────────────────────────────────────
@app.route('/api/profile', methods=['POST'])
@login_required
def api_profile():
    data = request.get_json(silent=True) or {}
    name = data.get('name', '').strip()
    if name:
        current_user.model.name = name
        db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/onboarding/complete', methods=['POST'])
@login_required
def api_onboarding_complete():
    """Mark onboarding as complete for the current user."""
    current_user.model.onboarded = True
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/onboarding/reset', methods=['POST'])
@login_required
def api_onboarding_reset():
    """Reset onboarding so user can replay it."""
    current_user.model.onboarded = False
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/status')
@login_required
def api_status():
    uid = current_user.model.id
    state = _get_state(uid)
    has_plaid = bool(os.getenv('PLAID_CLIENT_ID') and os.getenv('PLAID_SECRET'))
    with _user_state_lock:
        return jsonify({
            'status': state['status'],
            'msg': state['msg'],
            'error': state['error'],
            'loaded_at': state['loaded_at'],
            'plaid_configured': has_plaid,
            'onboarded': current_user.model.onboarded,
        })

@app.route('/api/data')
@login_required
def api_data():
    uid = current_user.model.id
    state = _get_state(uid)
    with _user_state_lock:
        data = state['data']
    if not data:
        return jsonify({'error': 'No data loaded'}), 404
    # Normalize field names to match vault.html mock format
    result = _normalize_api_data(data)
    # Override userName/userEmail with real DB values
    result['userName'] = current_user.model.name or ''
    result['userEmail'] = current_user.model.email or ''
    result['onboarded'] = current_user.model.onboarded
    return jsonify(result)

@app.route('/api/refresh', methods=['POST'])
@login_required
def api_refresh():
    uid = current_user.model.id
    state = _get_state(uid)
    with _user_state_lock:
        if state['status'] == 'error':
            state['status'] = 'idle'
            state['error'] = ''
            state['data'] = None
    cfg = build_user_config(current_user)
    if not _has_any_account(cfg):
        return jsonify({'ok': False, 'msg': 'No accounts connected'}), 400
    t = threading.Thread(target=fetch_data, args=(uid, cfg), daemon=True)
    t.start()
    return jsonify({'ok': True, 'msg': 'Refresh started'})

@app.route('/api/wallets', methods=['GET'])
@login_required
def api_list_wallets():
    uid = current_user.model.id
    wallets = CryptoWallet.query.filter_by(user_id=uid).all()
    return jsonify({'ok': True, 'wallets': [{'chain': w.chain, 'address': w.address, 'label': w.label} for w in wallets]})

@app.route('/api/wallets', methods=['POST'])
@login_required
def api_add_wallet():
    uid = current_user.model.id
    body = request.get_json() or {}
    chain = (body.get('chain') or '').strip().lower()
    address = (body.get('address') or '').strip()
    label = (body.get('label') or '').strip()
    if chain not in ('bitcoin','ethereum','solana','polygon','arbitrum','optimism','avalanche','base','bnb','usdt','usdc'):
        return jsonify({'ok': False, 'error': 'Invalid chain.'}), 400
    if not address:
        return jsonify({'ok': False, 'error': 'Address is required.'}), 400
    w = CryptoWallet(user_id=uid, chain=chain, address=address, label=label or chain.title())
    db.session.add(w)
    db.session.commit()
    # Trigger refresh
    cfg = build_user_config(current_user)
    if _has_any_account(cfg):
        t = threading.Thread(target=fetch_data, args=(uid, cfg), daemon=True)
        t.start()
    return jsonify({'ok': True, 'wallet': {'chain': chain, 'address': address, 'label': label or chain.title()}})

@app.route('/api/wallets/<int:wallet_id>', methods=['DELETE'])
@login_required
def api_delete_wallet(wallet_id):
    uid = current_user.model.id
    w = CryptoWallet.query.filter_by(id=wallet_id, user_id=uid).first()
    if not w:
        return jsonify({'ok': False, 'error': 'Wallet not found.'}), 404
    db.session.delete(w)
    db.session.commit()
    cfg = build_user_config(current_user)
    if _has_any_account(cfg):
        t = threading.Thread(target=fetch_data, args=(uid, cfg), daemon=True)
        t.start()
    return jsonify({'ok': True})

# ── Delete account ──────────────────────────────────────────────────────────
@app.route('/api/auth/delete', methods=['POST'])
@login_required
def api_delete_account():
    """Permanently delete the user's account and all associated data."""
    data = request.get_json(silent=True) or {}
    confirmation = data.get('confirmation', '')
    uid = current_user.model.id
    email = current_user.model.email
    # Require user to type their email to confirm
    if confirmation.strip().lower() != email.strip().lower():
        return jsonify({'ok': False, 'error': 'Confirmation does not match email.'}), 400

    # Delete related data
    CryptoWallet.query.filter_by(user_id=uid).delete()
    PlaidConnection.query.filter_by(user_id=uid).delete()
    WiseConnection.query.filter_by(user_id=uid).delete()
    db.session.execute(db.text('DELETE FROM email_verifications WHERE user_id = :uid'), {'uid': uid})
    db.session.execute(db.text('DELETE FROM vault_credentials WHERE user_id = :uid'), {'uid': uid})
    # Delete user
    UserModel.query.filter_by(id=uid).delete()
    db.session.commit()
    logout_user()
    return jsonify({'ok': True})

@app.route('/api/plaid/link_token', methods=['POST'])
@login_required
def plaid_link_token():
    import requests as req
    uid = current_user.model.id
    plaid_client = os.getenv('PLAID_CLIENT_ID')
    plaid_secret = os.getenv('PLAID_SECRET')
    if not plaid_client or not plaid_secret:
        return jsonify({'ok': False, 'error': 'Plaid credentials not configured.'}), 400
    _env = os.getenv('PLAID_ENV', 'production')
    base = ('https://production.plaid.com' if _env == 'production'
            else 'https://development.plaid.com' if _env == 'development'
            else 'https://sandbox.plaid.com')
    try:
        r = req.post(f'{base}/link/token/create', json={
            'client_id': plaid_client,
            'secret':    plaid_secret,
            'client_name': 'Fiscit',
            'country_codes': ['CA', 'US'],
            'language': 'en',
            'user': {'client_user_id': str(uid)},
            'products': ['transactions'],
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
        return jsonify({'ok': True, 'link_token': data['link_token']})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/plaid/exchange', methods=['POST'])
@login_required
def plaid_exchange():
    import requests as req
    uid = current_user.model.id
    plaid_client = os.getenv('PLAID_CLIENT_ID')
    plaid_secret = os.getenv('PLAID_SECRET')
    if not plaid_client or not plaid_secret:
        return jsonify({'ok': False, 'error': 'Plaid credentials not configured'}), 400
    body = request.get_json() or {}
    public_token = body.get('public_token')
    if not public_token:
        return jsonify({'ok': False, 'error': 'Missing public_token'}), 400
    _env = os.getenv('PLAID_ENV', 'production')
    base = ('https://production.plaid.com' if _env == 'production'
            else 'https://development.plaid.com' if _env == 'development'
            else 'https://sandbox.plaid.com')
    try:
        r = req.post(f'{base}/item/public_token/exchange', json={
            'client_id':    plaid_client,
            'secret':       plaid_secret,
            'public_token': public_token,
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
        access_token = data['access_token']
        # Save to DB for this user
        pc = PlaidConnection.query.filter_by(user_id=uid).first()
        if pc:
            pc.access_token = access_token
        else:
            pc = PlaidConnection(user_id=uid, access_token=access_token)
            db.session.add(pc)
        db.session.commit()
        # Trigger refresh
        cfg = build_user_config(current_user)
        t = threading.Thread(target=fetch_data, args=(uid, cfg), daemon=True)
        t.start()
        return jsonify({'ok': True, 'msg': 'Account connected! Refreshing data...'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/wise/test', methods=['POST'])
@login_required
def api_wise_test():
    import requests as req
    uid = current_user.model.id
    body = request.get_json() or {}
    token = body.get('wise_token', '').strip()
    profile = body.get('wise_profile', '').strip()
    if not token:
        return jsonify({'ok': False, 'error': 'API Token is required.'}), 400
    try:
        r = req.get('https://api.transferwise.com/v1/profiles',
                    headers={'Authorization': f'Bearer {token}'}, timeout=10)
        if r.status_code == 200:
            profiles = r.json()
            info = [{'id': p['id'], 'type': p['type']} for p in profiles]
            # Save/update Wise connection
            wc = WiseConnection.query.filter_by(user_id=uid).first()
            if wc:
                wc.api_token = token
                if profile:
                    wc.profile_id = profile
                elif len(profiles) == 1:
                    wc.profile_id = str(profiles[0]['id'])
            else:
                prof_id = profile or (str(profiles[0]['id']) if len(profiles) == 1 else '')
                wc = WiseConnection(user_id=uid, api_token=token, profile_id=prof_id)
                db.session.add(wc)
            db.session.commit()
            return jsonify({'ok': True, 'profiles': info})
        elif r.status_code == 401:
            return jsonify({'ok': False, 'error': 'Invalid API token.'}), 401
        else:
            return jsonify({'ok': False, 'error': f'Wise API returned {r.status_code}.'}), 400
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

if __name__ == '__main__':
    port  = int(os.getenv('PORT', 5050))
    debug = os.getenv('FLASK_DEBUG', '0') == '1'
    print(f"\n💰 Fiscit → http://localhost:{port}\n")
    app.run(host='0.0.0.0', port=port, debug=debug, use_reloader=False)
