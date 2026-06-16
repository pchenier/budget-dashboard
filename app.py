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
from datetime import datetime, timedelta
from pathlib import Path
from models import db, User as UserModel, PlaidConnection, WiseConnection, CryptoWallet

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "vault-local-secret-do-not-deploy")
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///fiscit.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}

db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(UserModel, int(user_id))

class FlaskUser(UserMixin):
    def __init__(self, user_model):
        self.model = user_model
    @property
    def id(self): return str(self.model.id)
    @property
    def email(self): return self.model.email
    @property
    def name(self): return self.model.name

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
        cfg['wise_profile'] = wc.profile_id or ''
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
        with _user_state_lock:
            state["status"] = "error"
            state["error"]  = str(e)
        print(f"[error] fetch_data user {user_id}: {e}")

# ── On startup: create DB tables ─────────────────────────────────────────────
def startup():
    with app.app_context():
        db.create_all()
        print("  DB tables ready")

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

    # Connection status derived from config
    config = load_config() or {}
    connections = {
        'plaid': bool(config.get('plaid_client') and config.get('plaid_secret') and config.get('plaid_token')),
        'wise': bool(config.get('wise_token')),
        'crypto': bool(config.get('wallets')),
        'wealthsimple': False,
        'kraken': False,
    }

    # Wallets list for frontend
    wallets_list = config.get('wallets', [])

    # Derive user name from first account institution, or config
    user_name = config.get('user_name', '')
    user_email = config.get('user_email', '')
    if not user_name and accounts:
        user_name = accounts[0].get('inst', accounts[0].get('name', ''))
    if not user_name:
        user_name = ''

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
SETUP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fiscit — Setup</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',sans-serif;background:#080808;color:#f4f4f5;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
.card{background:#111;border:1px solid #222;border-radius:16px;padding:2.5rem;width:100%;max-width:440px}
.header{display:flex;align-items:center;gap:10px;margin-bottom:2rem}
.header svg{flex-shrink:0}
.brand{font-size:1.25rem;font-weight:700;color:#4ade80;letter-spacing:-0.02em}
.title{font-size:1.1rem;font-weight:600;margin-bottom:0.25rem;letter-spacing:-0.2px}
.sub{font-size:0.85rem;color:#71717a;margin-bottom:2rem;line-height:1.6}
.section{margin-bottom:1.5rem}
.sec-label{font-size:0.65rem;font-weight:600;letter-spacing:0.1em;text-transform:uppercase;color:#4ade80;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid #1a1a1a}
label{display:block;font-size:0.75rem;font-weight:500;color:#a1a1aa;margin-bottom:4px;margin-top:10px;text-transform:uppercase;letter-spacing:0.04em}
input,select{width:100%;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:8px;padding:0.65rem 0.85rem;color:#f4f4f5;font-family:'Inter',sans-serif;font-size:0.85rem;outline:none;transition:border-color 0.15s}
input:focus,select:focus{border-color:#4ade80}
input::placeholder{color:#3f3f46}
.optional{font-size:0.65rem;color:#3f3f46;margin-left:4px}
.btn{width:100%;margin-top:1.75rem;padding:0.85rem;background:#4ade80;border:none;border-radius:8px;color:#080808;font-family:'Inter',sans-serif;font-size:0.9rem;font-weight:700;cursor:pointer;transition:opacity 0.15s}
.btn:hover{opacity:0.9}
.btn:disabled{opacity:0.4;cursor:not-allowed}
.error{background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.25);border-radius:8px;padding:0.65rem 0.85rem;font-size:0.85rem;color:#f87171;margin-bottom:1.25rem}
a{color:#4ade80;text-decoration:none}
.wallet-item{display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid #1a1a1a;font-size:0.85rem}
.wallet-chain{background:#1a2e1a;color:#4ade80;padding:2px 8px;border-radius:4px;font-size:0.65rem;font-weight:600;text-transform:uppercase;letter-spacing:0.05em}
.wallet-addr{color:#a1a1aa;font-family:monospace;font-size:0.75rem;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wallet-label{color:#d4d4d8;font-size:0.75rem}
.wallet-remove{background:none;border:none;color:#ef4444;cursor:pointer;font-size:0.85rem;padding:2px 6px;border-radius:4px}
.wallet-remove:hover{background:rgba(239,68,68,0.1)}
.add-wallet-btn{margin-top:8px;padding:6px 12px;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:8px;color:#a1a1aa;font-family:'Inter',sans-serif;font-size:0.75rem;font-weight:500;cursor:pointer;transition:all 0.15s}
.add-wallet-btn:hover{border-color:#4ade80;color:#4ade80}
.accounts{display:flex;flex-direction:column;gap:10px;margin-bottom:2rem}
.account-card{display:flex;align-items:center;gap:12px;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:12px;padding:14px 16px;cursor:pointer;transition:all 0.15s}
.account-card:hover{border-color:#4ade80;background:#1a1a1a}
.account-icon{width:40px;height:40px;border-radius:10px;background:#1a2e1a;color:#4ade80;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.account-icon.wise{background:#1a1a2e;color:#60a5fa}
.account-icon.crypto{background:#2e1a1a;color:#f59e0b}
.account-info{flex:1}
.account-name{font-size:0.9rem;font-weight:600;color:#f4f4f5}
.account-desc{font-size:0.75rem;color:#71717a;margin-top:2px}
.account-action{font-size:0.75rem;font-weight:600;color:#4ade80;text-transform:uppercase;letter-spacing:0.05em}
.sub-form{margin:-4px 0 8px 0;padding:12px 16px;background:#161616;border:1px solid #2a2a2a;border-radius:0 0 12px 12px}
.trust-powered a:hover{color:#71717a}
.hidden{display:none!important}
.account-icon.bank{background:#1a2e1a;color:#4ade80}
.account-card.connected{border-color:#1a3a1a;background:#0d1a0d}
.account-card.connected .account-action{color:#4ade80}
.account-card.connected .account-action::before{content:'✓ ';font-size:0.7rem}
.account-card.wise-connected{border-color:#1a3a1a;background:#0d1a0d}
.account-card.wise-connected .account-action{color:#4ade80}
.account-card.wise-connected .account-action::before{content:'✓ '}
.test-btn{width:100%;margin-top:10px;padding:0.6rem;background:transparent;border:1px solid #4ade80;border-radius:8px;color:#4ade80;font-family:'Inter',sans-serif;font-size:0.8rem;font-weight:600;cursor:pointer;transition:all 0.15s}
.test-btn:hover{background:#4ade80;color:#080808}
.test-result{margin-top:8px;padding:10px 12px;border-radius:8px;font-size:0.8rem;line-height:1.5}
.test-result.ok{background:rgba(74,222,128,0.1);border:1px solid rgba(74,222,128,0.25);color:#4ade80}
.test-result.err{background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.25);color:#f87171}
.trust-box{margin:-4px 0 8px 0;padding:16px;background:#0f1a0f;border:1px solid #1a3a1a;border-radius:0 0 12px 12px;animation:slideDown .2s ease}
@keyframes slideDown{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:translateY(0)}}
.trust-header{font-size:0.85rem;font-weight:600;color:#4ade80;margin-bottom:12px}
.trust-items{display:flex;flex-direction:column;gap:8px;margin-bottom:14px}
.trust-item{display:flex;align-items:center;gap:8px;font-size:0.8rem;color:#a1a1aa}
.trust-item svg{flex-shrink:0}
.trust-btn{width:100%;padding:0.7rem;background:#4ade80;border:none;border-radius:8px;color:#080808;font-family:'Inter',sans-serif;font-size:0.85rem;font-weight:700;cursor:pointer;transition:opacity 0.15s}
.trust-btn:hover{opacity:0.9}
.trust-powered{text-align:center;margin-top:8px;font-size:0.7rem;color:#3f3f46}
.trust-powered a{color:#52525b;text-decoration:none}
.trust-powered a:hover{color:#71717a}
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
  <div class="title">Get started</div>
  <p class="sub">Connect your accounts to see your full financial picture.</p>

  {% if error %}
  <div class="error">{{ error }}</div>
  {% endif %}

  <div class="accounts">
    <div class="account-card" id="bank-card" onclick="showBankTrust()">
      <div class="account-icon bank">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M3 21h18M3 10h18M5 10V21M9 10V21M15 10V21M19 10V21M3 10l9-7 9 7" stroke="#4ade80" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </div>
      <div class="account-info">
        <div class="account-name">Bank Account</div>
        <div class="account-desc">Secure connection via Plaid</div>
      </div>
      <div class="account-action">Connect</div>
    </div>
    <div id="bank-trust" class="hidden trust-box">
      <div class="trust-header">Your data is safe</div>
      <div class="trust-items">
        <div class="trust-item">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#4ade80" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
          <span>Bank grade 256-bit encryption</span>
        </div>
        <div class="trust-item">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#4ade80" stroke-width="2"><path d="M1 1h22v22H1z" stroke="none"/><path d="M20 6L9 17l-5-5" stroke="#4ade80" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
          <span>Read only access, we cannot move money</span>
        </div>
        <div class="trust-item">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#4ade80" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
          <span>Used by millions via Plaid</span>
        </div>
      </div>
      <button type="button" class="trust-btn" onclick="openPlaidLink()">Continue to Plaid</button>
      <div class="trust-powered">Powered by <a href="https://plaid.com" target="_blank">Plaid</a></div>
    </div>

    <div class="account-card" id="wise-card" onclick="document.getElementById('wise-form').classList.toggle('hidden')">
      <div class="account-icon wise">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" stroke="#9FE870" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </div>
      <div class="account-info">
        <div class="account-name">Wise</div>
        <div class="account-desc">International transfers</div>
      </div>
      <div class="account-action">Set up</div>
    </div>
    <div id="wise-form" class="hidden sub-form">
      <label>API Token</label>
      <input id="wise-token-input" name="wise_token" placeholder="932aba85-..." value="{{ vals.wise_token or '' }}">
      <label>Profile ID</label>
      <input id="wise-profile-input" name="wise_profile" placeholder="63963106" value="{{ vals.wise_profile or '' }}">
      <button type="button" class="test-btn" onclick="testWise()">Test Connection</button>
      <div id="wise-test-result"></div>
    </div>

    <div class="account-card" onclick="document.getElementById('crypto-form').classList.toggle('hidden')">
      <div class="account-icon crypto">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="12" cy="12" r="10" fill="#F7931A"/><path d="M14.5 10.5c0-1.1-.8-1.6-1.8-1.8V7.5h-1.4v1.1c-1 .2-1.8.8-1.8 1.9 0 1.6 1.4 1.6 2.8 1.9.8.2 1.2.5 1.2 1.1 0 .7-.6 1.1-1.4 1.1s-1.4-.4-1.5-1.2l-1.3.3c.2 1.2 1 1.8 2 2v1.2h1.4v-1.2c1.1-.2 1.9-.9 1.9-2 0-1.6-1.4-1.7-2.8-2-.8-.2-1.2-.4-1.2-1 0-.5.5-.9 1.2-.9.6 0 1.1.3 1.2.9l1.3-.3z" fill="#fff"/></svg>
      </div>
      <div class="account-info">
        <div class="account-name">Crypto Wallets</div>
        <div class="account-desc">BTC, ETH, SOL, USDT, USDC & more</div>
      </div>
      <div class="account-action">Add</div>
    </div>
    <div id="crypto-form" class="hidden sub-form">
      <div id="wallet-list">
        {% for w in vals.wallets or [] %}
        <div class="wallet-item" data-idx="{{ loop.index0 }}">
          <span class="wallet-chain">{{ w.chain }}</span>
          <span class="wallet-addr" title="{{ w.address }}">{{ w.address[:8] }}...{{ w.address[-4:] }}</span>
          <span class="wallet-label">{{ w.label }}</span>
          <button type="button" class="wallet-remove" onclick="removeWallet({{ loop.index0 }})">✕</button>
        </div>
        {% endfor %}
      </div>
      <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
        <select id="wallet-chain" style="width:30%">
          <option value="bitcoin">Bitcoin</option>
          <option value="ethereum">Ethereum</option>
          <option value="solana">Solana</option>
          <option value="polygon">Polygon</option>
          <option value="arbitrum">Arbitrum</option>
          <option value="optimism">Optimism</option>
          <option value="avalanche">Avalanche</option>
          <option value="base">Base</option>
          <option value="bnb">BNB Chain</option>
          <option value="usdt">USDT</option>
          <option value="usdc">USDC</option>
        </select>
        <input id="wallet-address" placeholder="Wallet address" style="width:45%">
        <input id="wallet-label" placeholder="Label" style="width:25%">
      </div>
      <button type="button" class="add-wallet-btn" onclick="addWallet()">+ Add Wallet</button>
    </div>
  </div>

  <button type="button" class="btn" id="submit-btn" onclick="window.location.href='/'">Continue to Dashboard</button>
  </form>
  <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
  <script>
    let plaidConnected = false;

    function showBankTrust(){
      const el=document.getElementById('bank-trust');
      el.classList.toggle('hidden');
    }

    function openPlaidLink(){
      fetch('/api/plaid/link_token',{method:'POST'})
        .then(r=>r.json()).then(d=>{
          if(!d.link_token){
            document.getElementById('bank-trust').innerHTML='<div class="trust-header">Something went wrong</div><div style="font-size:0.8rem;color:#a1a1aa;margin-bottom:14px;line-height:1.5">We could not connect to Plaid. Please try again later.</div><button type="button" class="trust-btn" onclick="openPlaidLink()">Retry</button><div class="trust-powered" style="margin-top:12px">Powered by <a href="https://plaid.com" target="_blank">Plaid</a></div>';
            return;}
          const handler=Plaid.create({
            token:d.link_token,
            onSuccess:async(publicToken)=>{
              const res=await fetch('/api/plaid/exchange',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({public_token:publicToken})});
              const data=await res.json();
              if(data.ok){
                plaidConnected=true;
                document.getElementById('bank-card').classList.add('connected');
                document.querySelector('.account-card:first-child .account-action').textContent='Connected';
                document.querySelector('.account-card:first-child .account-action').style.color='#4ade80';
                document.querySelector('.account-card:first-child').style.borderColor='#4ade80';
              } else { alert(data.error||'Failed to connect bank'); }
            },
            onExit:()=>{},
            onEvent:()=>{},
          });
          handler.open();
        }).catch(e=>alert('Error: '+e));
    }

    function addWallet(){
      const chain=document.getElementById('wallet-chain').value;
      const address=document.getElementById('wallet-address').value.trim();
      const label=document.getElementById('wallet-label').value.trim();
      if(!address){alert('Please enter a wallet address');return;}
      fetch('/api/wallets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chain,address,label})})
        .then(r=>r.json()).then(d=>{
          if(d.ok){
            const list=document.getElementById('wallet-list');
            const idx=list.children.length;
            const div=document.createElement('div');
            div.className='wallet-item';
            div.dataset.idx=idx;
            div.innerHTML=`<span class="wallet-chain">${chain}</span><span class="wallet-addr" title="${address}">${address.slice(0,8)}...${address.slice(-4)}</span><span class="wallet-label">${label||chain.charAt(0).toUpperCase()+chain.slice(1)}</span><button type="button" class="wallet-remove" onclick="removeWallet(${idx})">✕</button>`;
            list.appendChild(div);
            document.getElementById('wallet-address').value='';
            document.getElementById('wallet-label').value='';
          }
          else{alert(d.error||'Failed to add wallet');}
        }).catch(e=>alert('Error: '+e));
    }
    function testWise(){
      const token=document.getElementById('wise-token-input').value.trim();
      const profile=document.getElementById('wise-profile-input').value.trim();
      const el=document.getElementById('wise-test-result');
      if(!token){el.innerHTML='<div class="test-result err">Enter your API token first.</div>';return;}
      el.innerHTML='<div class="test-result" style="color:#a1a1aa">Testing...</div>';
      fetch('/api/wise/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({wise_token:token})})
        .then(r=>r.json()).then(d=>{
          if(d.ok){
            const profiles=d.profiles.map(p=>`Profile ${p.id} (${p.type})`).join(', ');
            let msg='Connected! '+profiles;
            if(!profile&&d.profiles.length===1){
              document.getElementById('wise-profile-input').value=d.profiles[0].id;
            }
            document.getElementById('wise-card').classList.add('wise-connected');
            document.querySelector('#wise-card .account-action').textContent='Connected';
            el.innerHTML='<div class="test-result ok">'+msg+'</div>';
          }else{
            el.innerHTML='<div class="test-result err">'+(d.error||'Connection failed.')+'</div>';
          }
        }).catch(()=>{el.innerHTML='<div class="test-result err">Network error.</div>'});
    }

    function removeWallet(idx){
      if(!confirm('Remove this wallet?'))return;
      fetch('/api/wallets/'+idx,{method:'DELETE'})
        .then(r=>r.json()).then(d=>{
          if(d.ok){location.reload();}
          else{alert(d.error||'Failed to remove wallet');}
        }).catch(e=>alert('Error: '+e));
    }
  </script>
</div>
</body>
</html>"""

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
<title>Fiscit — Sign Up</title>
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
  <div class="title">Create your account</div>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="POST">
    <label>Name</label>
    <input type="text" name="name" placeholder="Your name">
    <label>Email</label>
    <input type="email" name="email" placeholder="you@example.com" required>
    <label>Password</label>
    <input type="password" name="password" placeholder="Choose a password" required>
    <button type="submit" class="btn">Sign up</button>
  </form>
  <div class="link">Already have an account? <a href="/login">Log in</a></div>
</div>
</body>
</html>"""

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        name = request.form.get('name', '').strip()
        if not email or not password:
            return render_template_string(REGISTER_HTML, error='Email and password are required.')
        if len(password) < 6:
            return render_template_string(REGISTER_HTML, error='Password must be at least 6 characters.')
        if UserModel.query.filter_by(email=email).first():
            return render_template_string(REGISTER_HTML, error='An account with that email already exists.')
        user = UserModel(email=email, name=name)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flask_user = FlaskUser(user)
        login_user(flask_user)
        return redirect(url_for('setup'))
    return render_template_string(REGISTER_HTML, error='')

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
    return render_template_string(LOGIN_HTML, error='')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# ── Main routes ─────────────────────────────────────────────────────────────
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
        if not _has_any_account(cfg):
            return redirect(url_for('setup'))
        if status == "idle":
            t = threading.Thread(target=fetch_data, args=(uid, cfg), daemon=True)
            t.start()
            return render_template_string(LOADING_HTML)
        return render_template_string(LOADING_HTML)

    return build_vault_html(data)

@app.route('/setup', methods=['GET', 'POST'])
@login_required
def setup():
    error = request.args.get('error', '')
    uid = current_user.model.id
    user_model = current_user.model

    if request.method == 'POST':
        # Wise token/profile from form
        wise_token = request.form.get('wise_token', '').strip()
        wise_profile = request.form.get('wise_profile', '').strip()
        _PLACEHOLDERS = {'your_wise_profile_id', 'your_wise_api_token'}
        if wise_token and wise_token not in _PLACEHOLDERS:
            # Save or update Wise connection
            wc = WiseConnection.query.filter_by(user_id=uid).first()
            if wc:
                wc.api_token = wise_token
                if wise_profile and wise_profile.isdigit() and wise_profile not in _PLACEHOLDERS:
                    wc.profile_id = wise_profile
            else:
                wc = WiseConnection(user_id=uid, api_token=wise_token, profile_id=wise_profile if (wise_profile and wise_profile.isdigit()) else '')
                db.session.add(wc)
        db.session.commit()
        cfg = build_user_config(current_user)
        if _has_any_account(cfg):
            t = threading.Thread(target=fetch_data, args=(uid, cfg), daemon=True)
            t.start()
            return render_template_string(LOADING_HTML)
        return redirect(url_for('setup'))

    # Build vals for template
    pc = PlaidConnection.query.filter_by(user_id=uid).first()
    wc = WiseConnection.query.filter_by(user_id=uid).first()
    wallets = CryptoWallet.query.filter_by(user_id=uid).all()
    vals = {
        'plaid_token': pc.access_token if pc else '',
        'wise_token': wc.api_token if wc else '',
        'wise_profile': wc.profile_id if wc else '',
        'wallets': [{'chain': w.chain, 'address': w.address, 'label': w.label} for w in wallets],
    }
    return render_template_string(SETUP_HTML, error=error, vals=vals)

# ── API routes ──────────────────────────────────────────────────────────────
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
    return jsonify(data)

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
    startup()
    app.run(host='0.0.0.0', port=port, debug=debug, use_reloader=False)
