#!/usr/bin/env python3
"""
Vault — Budget & Life Dashboard
Serves the Vault UI with real Plaid/Wise/Phantom data.

Usage:
    python app.py

On first run with no saved config → redirects to /setup.
After setup, config is saved to saved_config.json — never asks again on restart.
"""

import os, json, re, threading, time, uuid
from flask import Flask, request, session, redirect, url_for, render_template_string, jsonify, send_from_directory, flash
from datetime import datetime, timedelta, timezone
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from authlib.integrations.flask_client import OAuth
import jwt as pyjwt
from pathlib import Path
from models import db, User as UserModel, PlaidConnection, WiseConnection, CryptoWallet, InvestmentAccount, InvestmentSnapshot, UsedSsoToken, PasswordReset, InstitutionLogo, BrokerConnection, BrokerSnapshot
import brokers

# Normalize French categories to English (matches generate_data.py CATEGORY_ALIASES)
CATEGORY_ALIASES = {
    "Épicerie":         "Groceries",
    "Bouffe/Resto":     "Food/Dining",
    "Gaz":              "Gas",
    "Essence":          "Gas",
    "Transport":        "Transport",
    "Shopping":          "Shopping",
    "Business/Tech":    "Business/Tech",
    "Gym":              "Gym",
    "Santé":            "Health",
    "Télécom":          "Phone/Internet",
    "Divertissement":   "Entertainment",
    "Abonnements":      "Subscriptions",
    "Logement":         "Housing",
    "Utilities":        "Utilities",
    "Cash/Virements":   "Cash/Transfers",
    "Investissements":  "Investments",
    "Revenu":           "Income",
    "Autre":            "Other",
    "Moto":             "Auto",
    "Auto":             "Auto",
    "Épargne":          "Savings",
}
CATEGORY_ICONS = {
    "Groceries": "🛒", "Food/Dining": "🍽️", "Gas": "⛽", "Transport": "🚇",
    "Shopping": "🛍️", "Gym": "💪", "Health": "💊", "Phone/Internet": "📱",
    "Entertainment": "🎬", "Subscriptions": "📺", "Housing": "🏠", "Utilities": "⚡",
    "Cash/Transfers": "💸", "Investments": "📈", "Income": "💼", "Other": "📂",
    "Business/Tech": "💻", "Auto": "🚗", "Moto": "🏍️", "Savings": "🏦",
}

app = Flask(__name__)

# Production = running on Railway. Never fall back to the public dev secret there:
# anyone could forge sessions and SSO tokens with it.
IS_PROD = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"))
if IS_PROD and not (os.getenv("SECRET_KEY") and os.getenv("JWT_SECRET")):
    raise RuntimeError("SECRET_KEY and JWT_SECRET must be set in production")
app.secret_key = os.getenv("SECRET_KEY", "vault-local-secret-do-not-deploy")

# Railway terminates TLS: trust its X-Forwarded-For/Proto so request.remote_addr is
# the real client (rate limits) and generated URLs are https.
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

# ── Errors: branded pages for people, JSON for the API, details only in the logs ──
import logging
import monitoring
from werkzeug.exceptions import HTTPException

app.logger.setLevel(logging.INFO)
if monitoring.init_logging(app):
    print("  Better Stack log shipping on")

def _error_response(code, ref=''):
    if request.path.startswith('/api/'):
        body = {'error': monitoring.ERROR_PAGES.get(code, monitoring.ERROR_PAGES[500])[1]}
        if ref:
            body['ref'] = ref
        return jsonify(body), code
    return monitoring.error_page(code, ref), code

@app.errorhandler(HTTPException)
def _handle_http_error(e):
    if e.code is None or e.code < 400:
        return e  # redirects raised by routing
    return _error_response(e.code)

@app.errorhandler(Exception)
def _handle_unexpected_error(e):
    # The reference lets support match a user's screenshot to the log line
    ref = uuid.uuid4().hex[:8]
    app.logger.exception('Unhandled error %s on %s %s', ref, request.method, request.path)
    return _error_response(500, ref)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PROD,
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SAMESITE="Lax",
    REMEMBER_COOKIE_SECURE=IS_PROD,
    REMEMBER_COOKIE_DURATION=timedelta(days=30),
)

_CORS_ORIGINS = {'https://fiscit.com', 'https://app.fiscit.com'}
if not IS_PROD:
    _CORS_ORIGINS |= {'http://localhost:3000', 'http://localhost:8081'}

# ── CORS for mobile app ─────────────────────────────────────────────────────
@app.after_request
def add_cors_headers(response):
    # Native mobile apps don't use CORS at all; browsers only get access from our own sites.
    origin = request.headers.get('Origin', '')
    if origin in _CORS_ORIGINS:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Vary'] = 'Origin'
        if request.path.startswith('/api/'):
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-CSRF-Token'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        else:
            response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response

@app.after_request
def add_security_headers(response):
    h = response.headers
    h.setdefault('X-Content-Type-Options', 'nosniff')
    h.setdefault('X-Frame-Options', 'DENY')
    h.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    h.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=(), payment=()')
    h.setdefault('Content-Security-Policy', "frame-ancestors 'none'; base-uri 'self'; object-src 'none'")
    if IS_PROD:
        h.setdefault('Strict-Transport-Security', 'max-age=63072000; includeSubDomains')
    return response

@app.before_request
def handle_preflight():
    if request.method == 'OPTIONS' and request.path.startswith('/api/'):
        return '', 204

# ── JWT auth for mobile API ──────────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET", os.getenv("SECRET_KEY", "vault-local-secret-do-not-deploy"))
JWT_EXPIRY_DAYS = 30

# ── CSRF protection ──────────────────────────────────────────────────────────
def generate_csrf_token():
    """Generate a CSRF token and store it in the session."""
    if 'csrf_token' not in session:
        session['csrf_token'] = uuid.uuid4().hex
    return session['csrf_token']

def csrf_required(f):
    """Decorator: check CSRF token on state-changing POST routes (not /api/ which uses JWT/Bearer)."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == 'GET':
            return f(*args, **kwargs)
        if request.path.startswith('/api/'):
            return f(*args, **kwargs)
        if request.is_json or request.content_type and 'application/json' in request.content_type:
            token = request.headers.get('X-CSRF-Token', '')
        else:
            token = request.form.get('csrf_token', '')
        if token != session.get('csrf_token'):
            return jsonify({'error': 'CSRF token missing or invalid'}), 403
        return f(*args, **kwargs)
    return decorated

# ── Rate limiting for registration ────────────────────────────────────────────
_register_attempts = {}  # {ip: [(timestamp, ), ...]}

def _check_register_rate_limit(ip):
    """Return True if the IP is rate-limited (more than 3 in the last hour)."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=1)
    # Clean old entries
    for k in list(_register_attempts.keys()):
        _register_attempts[k] = [t for t in _register_attempts[k] if t > cutoff]
        if not _register_attempts[k]:
            del _register_attempts[k]
    attempts = _register_attempts.get(ip, [])
    return len(attempts) >= 3

def _record_register_attempt(ip):
    """Record a registration attempt from this IP."""
    now = datetime.now(timezone.utc)
    _register_attempts.setdefault(ip, []).append(now)

_login_failures = {}  # {key: [timestamps]} — single gunicorn worker, so in-memory is shared
_LOGIN_WINDOW = timedelta(minutes=15)

def _login_blocked(email):
    """True when this IP+email or this IP has too many recent failures."""
    now = datetime.now(timezone.utc)
    ip = request.remote_addr or '0'
    for key, limit in ((f'{ip}|{email}', 8), (ip, 40)):
        hits = [t for t in _login_failures.get(key, []) if now - t < _LOGIN_WINDOW]
        _login_failures[key] = hits
        if len(hits) >= limit:
            return True
    return False

def _record_login_failure(email):
    now = datetime.now(timezone.utc)
    ip = request.remote_addr or '0'
    for key in (f'{ip}|{email}', ip):
        _login_failures.setdefault(key, []).append(now)

_DUMMY_HASH = None
def _check_login(email, password):
    """Return the user if the password matches. Runs bcrypt even for unknown
    emails so response time doesn't reveal which accounts exist."""
    global _DUMMY_HASH
    user = UserModel.query.filter_by(email=email).first()
    if user and user.check_password(password):
        return user
    if not user:
        import bcrypt
        if _DUMMY_HASH is None:
            _DUMMY_HASH = bcrypt.hashpw(b'fiscit-dummy', bcrypt.gensalt())
        try:
            bcrypt.checkpw(password.encode()[:72], _DUMMY_HASH)
        except Exception:
            pass
    return None

SSO_AUDIENCE = 'fiscit-sso'

def make_jwt(user_id, email, token_version=0):
    return pyjwt.encode({
        'sub': str(user_id),
        'email': email,
        'tv': token_version,
        'exp': datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRY_DAYS),
    }, JWT_SECRET, algorithm='HS256')

def verify_jwt(token):
    try:
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        return payload
    except (pyjwt.ExpiredSignatureError, pyjwt.InvalidTokenError):
        return None

def _token_version_ok(user, payload):
    """A token issued before the user's last "sign out everywhere" / password reset is dead."""
    try:
        return int(payload.get('tv', 0)) == (user.token_version or 0)
    except (TypeError, ValueError):
        return False

def api_auth_required(f):
    """Accept either Flask-Login cookie auth or JWT Bearer token."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        # Try JWT Bearer token first
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]
            payload = verify_jwt(token)
            if payload:
                user = UserModel.query.filter_by(id=int(payload['sub'])).first()
                if user and _token_version_ok(user, payload):
                    from flask_login import login_user as _login_user
                    _login_user(FlaskUser(user), remember=False)
                    return f(*args, **kwargs)
        # Fall back to Flask-Login cookie auth
        if current_user.is_authenticated:
            return f(*args, **kwargs)
        return jsonify({'error': 'Authentication required'}), 401
    return decorated

# Handle DATABASE_URL: Railway gives postgres:// — pin the psycopg2 driver explicitly,
# since SQLAlchemy 2.1+ defaults bare postgresql:// to psycopg (v3), which isn't installed.
_database_url = os.getenv('DATABASE_URL', 'sqlite:///fiscit.db')
for _prefix in ('postgres://', 'postgresql://'):
    if _database_url.startswith(_prefix):
        _database_url = 'postgresql+psycopg2://' + _database_url[len(_prefix):]
        break

app.config['SQLALCHEMY_DATABASE_URI'] = _database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}

db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
app.permanent_session_lifetime = timedelta(days=30)

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
    # Session ids are "<user id>:<token version>" (plain "<user id>" from before
    # versions existed counts as version 0), so bumping the version logs out
    # every existing session and remember-me cookie.
    uid, _, tv = str(user_id).partition(':')
    try:
        user = db.session.get(UserModel, int(uid))
        tv = int(tv or 0)
    except (TypeError, ValueError):
        return None
    if user and tv == (user.token_version or 0):
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
    def get_id(self): return f'{self.model.id}:{self.model.token_version or 0}'

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
def _plaid_base():
    env = os.getenv('PLAID_ENV', 'production')
    return ('https://production.plaid.com' if env == 'production'
            else 'https://development.plaid.com' if env == 'development'
            else 'https://sandbox.plaid.com')

def _plaid_account_keys(access_token):
    """Fingerprints of an Item's accounts: (mask, subtype), or (name, subtype) when
    the bank gives no mask. Returns None if Plaid can't be reached, so we never
    delete anything on a guess."""
    import requests as req
    try:
        r = req.post(f'{_plaid_base()}/accounts/get', json={
            'client_id': os.getenv('PLAID_CLIENT_ID'), 'secret': os.getenv('PLAID_SECRET'),
            'access_token': access_token,
        }, timeout=15)
        if r.status_code != 200:
            return None
        return {((a.get('mask') or a.get('name') or '').strip().lower(), a.get('subtype') or '')
                for a in r.json().get('accounts', [])}
    except Exception:
        return None

def _dedupe_plaid_connections(uid):
    """The same bank linked twice shows every account twice. Two connections at the
    same institution that share at least one account are the same login: keep the
    newest (freshest credentials), revoke the older Item at Plaid, delete its row.
    Different logins at the same bank (no shared account) are left alone."""
    conns = PlaidConnection.query.filter_by(user_id=uid).order_by(PlaidConnection.created_at.desc(), PlaidConnection.id.desc()).all()
    # Connections made from fiscit.com's onboarding have no institution name yet
    if len(conns) > 1 and any(not c.institution for c in conns):
        for c in conns:
            if not c.institution:
                c.institution = _plaid_item_institution(c.access_token)
        db.session.commit()
    by_inst = {}
    for c in conns:
        by_inst.setdefault((c.institution or '').strip().lower(), []).append(c)
    removed = []
    for inst, group in by_inst.items():
        if not inst or len(group) < 2:
            continue
        kept = []  # (conn, account keys), newest first
        for c in group:
            keys = _plaid_account_keys(c.access_token)
            if keys is None:
                kept.append((c, set()))
                continue
            if any(keys & k for _, k in kept):
                removed.append(c)
            else:
                kept.append((c, keys))
    if removed:
        _plaid_remove_items({c.access_token for c in removed})
        for c in removed:
            db.session.delete(c)
        db.session.commit()
        app.logger.info('Removed %d duplicate Plaid connection(s) for user %s', len(removed), uid)
    return len(removed)

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
    # Plaid connections (user can have multiple banks). Drop a re-linked duplicate first.
    try:
        _dedupe_plaid_connections(user.model.id)
    except Exception as e:
        db.session.rollback()
        app.logger.warning('Plaid dedupe skipped: %s', type(e).__name__)
    plaid_conns = PlaidConnection.query.filter_by(user_id=user.model.id).all()
    if plaid_conns:
        cfg['plaid_tokens'] = [pc.access_token for pc in plaid_conns]
        cfg['plaid_token'] = plaid_conns[0].access_token  # backwards compat
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
        config.get("plaid_tokens") or config.get("plaid_token") or
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
        # Pass previous Plaid accounts as fallback in case Plaid API fails
        prev_accounts = []
        if state.get("data") and isinstance(state["data"], dict):
            prev_accounts = state["data"].get("accounts", [])
        config["_prev_accounts"] = prev_accounts
        data = generate_data.pull_all(config)

        # Bank / platform logos for the Accounts and Investments pages
        try:
            with app.app_context():
                _sync_user_brokers(user_id)
                names = [a.get('inst') or a.get('name') for a in data.get('accounts', [])]
                names += [i.platform for i in InvestmentAccount.query.filter_by(user_id=user_id).all()]
                names += [a.get('institution') for c in BrokerConnection.query.filter_by(user_id=user_id).all()
                          for a in json.loads(c.cache or '[]')]
                _ensure_logos(names)
        except Exception as e:
            app.logger.warning('logo refresh skipped: %s', type(e).__name__)

        with _user_state_lock:
            state["data"]      = data
            state["status"]    = "ready"
            state["loaded_at"] = datetime.now().isoformat()
            state["msg"]       = f"Last synced: {datetime.now().strftime('%H:%M')}"

    except Exception as e:
        import traceback
        with _user_state_lock:
            state["status"] = "error"
            state["error"]  = 'Could not sync your accounts. Please try again later.'
        print(f"[error] fetch_data user {user_id}: {e}")
        traceback.print_exc()

def _migrate_token_encryption(inspector):
    """One-time, idempotent: encrypt plaintext tokens, and move Plaid Items that the
    fiscit.com onboarding saved only in vault_credentials into plaid_connections
    (the dashboard syncs from there, so those banks never showed up)."""
    import crypto_util
    from sqlalchemy import text
    tables = set(inspector.get_table_names())
    targets = [('plaid_connections', 'access_token'), ('wise_connections', 'api_token')]
    if 'vault_credentials' in tables:
        targets += [('vault_credentials', c) for c in ('plaid_token', 'wise_token')
                    if c in [col['name'] for col in inspector.get_columns('vault_credentials')]]
    for table, col in targets:
        if table not in tables:
            continue
        key = 'user_id' if table == 'vault_credentials' else 'id'
        rows = db.session.execute(text(f'SELECT {key}, {col} FROM {table}')).fetchall()
        changed = 0
        for rid, val in rows:
            if val and not crypto_util.is_encrypted(val):
                db.session.execute(text(f'UPDATE {table} SET {col} = :v WHERE {key} = :id'),
                                   {'v': crypto_util.encrypt(val), 'id': rid})
                changed += 1
        if changed:
            db.session.commit()
            print(f"  Encrypted {changed} token(s) in {table}.{col}")
    if 'vault_credentials' in tables:
        moved = 0
        for uid, stored in db.session.execute(text('SELECT user_id, plaid_token FROM vault_credentials WHERE plaid_token IS NOT NULL')).fetchall():
            tok = crypto_util.decrypt(stored)
            if not tok or not tok.startswith('access-'):
                continue
            if any(pc.access_token == tok for pc in PlaidConnection.query.filter_by(user_id=uid).all()):
                continue
            if not db.session.get(UserModel, uid):
                continue
            db.session.add(PlaidConnection(user_id=uid, access_token=tok, institution=_plaid_item_institution(tok)))
            moved += 1
        if moved:
            db.session.commit()
            print(f"  Linked {moved} onboarding Plaid connection(s) to the dashboard")

def _plaid_item_institution(access_token):
    """Institution name of a Plaid Item (best effort, '' on failure)."""
    import requests as req
    cid, secret = os.getenv('PLAID_CLIENT_ID'), os.getenv('PLAID_SECRET')
    if not (cid and secret):
        return ''
    base = {'production': 'https://production.plaid.com', 'development': 'https://development.plaid.com'}.get(os.getenv('PLAID_ENV', 'production'), 'https://sandbox.plaid.com')
    try:
        item = req.post(f'{base}/item/get', json={'client_id': cid, 'secret': secret, 'access_token': access_token}, timeout=8).json().get('item', {})
        inst_id = item.get('institution_id')
        if not inst_id:
            return ''
        inst = req.post(f'{base}/institutions/get_by_id', json={'client_id': cid, 'secret': secret, 'institution_id': inst_id,
                        'country_codes': [c.strip().upper() for c in os.getenv('PLAID_COUNTRY_CODES', 'CA').split(',') if c.strip()]}, timeout=8)
        return (inst.json().get('institution') or {}).get('name', '') if inst.ok else ''
    except Exception:
        return ''

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
        if 'tour_seen' not in existing_cols:
            db.session.execute(text('ALTER TABLE users ADD COLUMN tour_seen BOOLEAN NOT NULL DEFAULT FALSE'))
            db.session.commit()
            print("  Migrated: added users.tour_seen")
        if 'token_version' not in existing_cols:
            db.session.execute(text('ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0'))
            db.session.commit()
            print("  Migrated: added users.token_version")
        if 'email_confirmed' not in existing_cols:
            db.session.execute(text("ALTER TABLE users ADD COLUMN email_confirmed BOOLEAN NOT NULL DEFAULT TRUE"))
            db.session.commit()
            print("  Migrated: added users.email_confirmed")
        # Make password_hash nullable for Google-only users
        if 'password_hash' in existing_cols:
            try:
                db.session.execute(text('ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL'))
                db.session.commit()
                print("  Migrated: users.password_hash now nullable")
            except Exception:
                pass  # already nullable
        # Encrypt third-party tokens still stored in plaintext (idempotent)
        _migrate_token_encryption(inspector)
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
    logos = _logo_urls([a.get("inst", a.get("institution", a.get("name", ""))) for a in raw_accs])
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
            "logo":  logos.get(a.get("inst", a.get("institution", a.get("name", ""))), ""),
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

    # Inject CSRF token meta tag into <head>
    csrf_token = generate_csrf_token()
    html = html.replace(
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
        f'<meta name="viewport" content="width=device-width, initial-scale=1.0">\n<meta name="csrf-token" content="{csrf_token}">',
        1
    )

    return html

# ── Institution logos ─────────────────────────────────────────────────────────
# Official logos come from Plaid's institution metadata. Platforms Plaid doesn't
# list (brokers, Wise…) fall back to the icon published on their official site,
# from this fixed list. Everything is fetched server-side and cached, so the
# browser never contacts third parties.
_KNOWN_DOMAINS = {
    'questrade': 'questrade.com', 'wealthsimple': 'wealthsimple.com', 'wise': 'wise.com',
    'interactive brokers': 'interactivebrokers.ca', 'disnat': 'disnat.com',
    'national bank direct brokerage': 'nbdb.ca', 'national bank': 'nbc.ca', 'banque nationale': 'bnc.ca',
    'td direct investing': 'td.com', 'td ': 'td.com', 'rbc direct investing': 'rbcdirectinvesting.com', 'rbc': 'rbc.com',
    'bmo investorline': 'bmo.com', 'bmo': 'bmo.com', 'scotia': 'scotiabank.com', 'cibc': 'cibc.com',
    'desjardins': 'desjardins.com', 'tangerine': 'tangerine.ca', 'simplii': 'simplii.com', 'eq bank': 'eqbank.ca',
    'koho': 'koho.ca', 'neo financial': 'neofinancial.com', 'american express': 'americanexpress.com',
    'capital one': 'capitalone.ca', 'laurentian': 'laurentianbank.ca', 'atb': 'atb.com', 'hsbc': 'hsbc.ca',
}
_LOGO_MIMES = {'image/png', 'image/jpeg', 'image/x-icon', 'image/vnd.microsoft.icon', 'image/webp', 'image/gif'}

def _logo_key(name):
    return re.sub(r'[^a-z0-9]+', '-', (name or '').lower()).strip('-')[:120]

def _plaid_institution(name):
    """(logo bytes or None, primary color, website) for the best Plaid match."""
    import requests as req, base64
    cid, secret = os.getenv('PLAID_CLIENT_ID'), os.getenv('PLAID_SECRET')
    if not (cid and secret):
        return None, '', ''
    try:
        r = req.post(f'{_plaid_base()}/institutions/search', json={
            'client_id': cid, 'secret': secret, 'query': name[:100],
            'country_codes': PLAID_COUNTRY_CODES, 'options': {'include_optional_metadata': True},
        }, timeout=8)
        if r.status_code != 200:
            return None, '', ''
        insts = r.json().get('institutions', [])
        if not insts:
            return None, '', ''
        best = next((i for i in insts if (i.get('name') or '').lower() == name.lower()), insts[0])
        logo = base64.b64decode(best['logo']) if best.get('logo') else None
        return logo, best.get('primary_color') or '', best.get('url') or ''
    except Exception:
        return None, '', ''

def _site_icon(domain):
    """Icon from the institution's own https site (apple-touch-icon, then favicon)."""
    import requests as req
    from urllib.parse import urlparse
    from urllib.parse import urljoin
    host = urlparse(domain if '://' in domain else 'https://' + domain).hostname or ''
    if not re.fullmatch(r'[a-z0-9.-]+\.[a-z]{2,}', host or ''):
        return None, ''
    base = '.'.join(host.split('.')[-2:])
    candidates = []
    # Icons declared in the homepage (<link rel="apple-touch-icon"|"icon">), biggest first
    try:
        page = req.get(f'https://{host}/', timeout=6, stream=True, headers={'User-Agent': 'Fiscit/1.0 (logo fetch)'})
        html_text = page.raw.read(500_000, decode_content=True).decode('utf-8', 'ignore') if page.status_code == 200 else ''
        links = []
        for tag in re.findall(r'<link\b[^>]*>', html_text, flags=re.I):
            rel = (re.search(r'rel=["\']([^"\']+)', tag, re.I) or [None, ''])[1].lower()
            href = (re.search(r'href=["\']([^"\']+)', tag, re.I) or [None, ''])[1]
            if 'icon' not in rel or not href or href.lower().endswith('.svg'):
                continue
            size = max([int(x) for x in re.findall(r'(\d+)x\d+', tag)] or [180 if 'apple' in rel else 32])
            links.append((size, urljoin(page.url, href)))
        for _, url in sorted(links, reverse=True):
            h = urlparse(url).hostname or ''
            if url.startswith('https://') and (h == base or h.endswith('.' + base)):
                candidates.append(url)
    except Exception:
        pass
    candidates += [f'https://{host}/apple-touch-icon.png', f'https://{host}/favicon.ico']
    for url in candidates[:6]:
        try:
            r = req.get(url, timeout=6, stream=True, headers={'User-Agent': 'Fiscit/1.0 (logo fetch)'})
            mime = (r.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            if r.status_code != 200 or not r.url.startswith('https://') or mime not in _LOGO_MIMES:
                continue
            body = r.raw.read(300_000 + 1, decode_content=True)
            if 0 < len(body) <= 300_000:
                return body, mime
        except Exception:
            continue
    return None, ''

def _ensure_logos(names):
    """Fetch and cache logos for these institution names (skips fresh entries)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for name in {n.strip() for n in names if n and n.strip()}:
        key = _logo_key(name)
        if not key:
            continue
        row = db.session.get(InstitutionLogo, key)
        if row:
            age = now - _naive(row.fetched_at)
            if (row.data and age < timedelta(days=30)) or (not row.data and age < timedelta(days=7)):
                continue
        data, color, site = _plaid_institution(name)
        mime = 'image/png' if data else ''
        if not data:
            low = name.lower()
            domain = next((d for k, d in _KNOWN_DOMAINS.items() if k in low + ' '), '') or site
            if domain:
                data, mime = _site_icon(domain)
        row = row or InstitutionLogo(key=key)
        row.name, row.data, row.mime, row.color, row.fetched_at = name[:200], data, mime, color[:16], now
        db.session.merge(row)
        db.session.commit()

def _ensure_logos_async(names):
    def run():
        with app.app_context():
            try:
                _ensure_logos(names)
            except Exception as e:
                db.session.rollback()
                app.logger.warning('logo fetch failed: %s', type(e).__name__)
    threading.Thread(target=run, daemon=True).start()

def _logo_urls(names):
    """{name: '/logo/<key>'} for names that have a cached logo (no network)."""
    keys = {_logo_key(n): n for n in names if n}
    if not keys:
        return {}
    rows = InstitutionLogo.query.filter(InstitutionLogo.key.in_(list(keys)), InstitutionLogo.data.isnot(None)).with_entities(InstitutionLogo.key).all()
    return {keys[k]: f'/logo/{k}' for (k,) in rows}

@app.route('/logo/<key>')
@login_required
def institution_logo(key):
    row = db.session.get(InstitutionLogo, _logo_key(key))
    if not row or not row.data or row.mime not in _LOGO_MIMES:
        return '', 404
    resp = app.response_class(row.data, mimetype=row.mime)
    resp.headers['Cache-Control'] = 'private, max-age=86400'
    resp.headers['Content-Security-Policy'] = "default-src 'none'; sandbox"
    return resp

def _naive(dt):
    return dt.replace(tzinfo=None) if dt and dt.tzinfo else dt

def _investment_js(a, updated=None):
    """Manual investment account -> the shape vault.html's Investments page renders."""
    label = a.label or a.platform
    return {
        'id': a.id, 'platform': a.platform, 'label': label, 'balance': a.balance,
        'name': a.platform if label == a.platform else f"{label} · {a.platform}",
        'ticker': label[:4].upper(), 'val': a.balance, 'pct': 0, 'color': '#3b82f6',
        'source': 'manual', 'updated': (updated or a.created_at).strftime('%Y-%m-%d') if (updated or a.created_at) else '',
    }

_PLAID_INV_COLORS = ['#a78bfa', '#22d3ee', '#f472b6', '#fb923c', '#34d399']

# ── Broker connections (read-only) ───────────────────────────────────────────
_BROKER_MIN_INTERVAL = timedelta(minutes=30)

def _broker_public(c):
    """What the browser may see about a connection (never credentials)."""
    return {'id': c.id, 'provider': c.provider, 'label': c.label or brokers.PROVIDERS.get(c.provider, {}).get('label', c.provider),
            'status': c.status, 'error': c.last_error or '',
            'lastSynced': _naive(c.last_synced).strftime('%Y-%m-%d %H:%M') if c.last_synced else ''}

def _record_broker_sync(conn, creds, accounts):
    conn.credentials = json.dumps(creds)
    conn.cache = json.dumps(accounts)
    conn.status, conn.last_error = 'ok', ''
    conn.last_synced = datetime.now(timezone.utc)
    today = datetime.now(timezone.utc).date()
    for a in accounts:
        snap = BrokerSnapshot.query.filter_by(connection_id=conn.id, account_key=a['key'], day=today).first()
        if snap:
            snap.balance = a['balance_cad']
        else:
            db.session.add(BrokerSnapshot(connection_id=conn.id, user_id=conn.user_id, account_key=a['key'],
                                          balance=a['balance_cad'], day=today))
    db.session.commit()

def _sync_broker(conn):
    """Sync one connection; errors are stored on the connection, never raised."""
    provider = brokers.PROVIDERS.get(conn.provider)
    if not provider or conn.status == 'pending':
        return
    creds = json.loads(conn.credentials or '{}')

    def persist(new_creds):  # rotating tokens (Questrade) are saved the moment they change
        conn.credentials = json.dumps(new_creds)
        db.session.commit()
    try:
        creds, accounts = provider['sync'](creds, persist)
        _record_broker_sync(conn, creds, accounts)
        app.logger.warning('Broker sync %s (connection %s): ok, %d account(s), %d position(s)', conn.provider, conn.id,
                           len(accounts), sum(len(a.get('positions', [])) for a in accounts))
        if conn.provider == 'snaptrade' and brokers.last_diag:
            app.logger.warning('SnapTrade detail (connection %s): %s', conn.id, brokers.last_diag[:400])
    except brokers.BrokerError as e:
        db.session.rollback()
        conn.status, conn.last_error = 'error', str(e)[:300]
        db.session.commit()
        app.logger.warning('Broker sync %s (connection %s): %s', conn.provider, conn.id, str(e)[:200])
    except Exception as e:
        db.session.rollback()
        app.logger.error('Broker sync %s failed: %s', conn.provider, type(e).__name__)
        conn.status, conn.last_error = 'error', 'Sync failed. Try again later.'
        db.session.commit()

def _sync_user_brokers(user_id, force=False):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for conn in BrokerConnection.query.filter_by(user_id=user_id).all():
        empty = conn.status == 'error' or not json.loads(conn.cache or '[]')
        interval = timedelta(minutes=1) if empty else _BROKER_MIN_INTERVAL  # retry empty/failed links quickly
        if force or not conn.last_synced or now - _naive(conn.last_synced) > interval:
            _sync_broker(conn)

def _retry_broker_later(conn_id, delays=(15, 45, 120)):
    """SnapTrade imports accounts a little after the user links a broker: retry in the background."""
    def run():
        for d in delays:
            time.sleep(d)
            with app.app_context():
                conn = db.session.get(BrokerConnection, conn_id)
                if not conn:
                    return
                if conn.status == 'ok' and json.loads(conn.cache or '[]'):
                    return
                _sync_broker(conn)
    threading.Thread(target=run, daemon=True).start()

def _broker_keepalive():
    """Questrade refresh tokens die if unused for a few days: refresh them even when
    the user doesn't open the dashboard. One gunicorn worker, so one loop."""
    while True:
        time.sleep(6 * 3600)
        try:
            with app.app_context():
                stale = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=12)
                for conn in BrokerConnection.query.filter_by(provider='questrade').all():
                    if conn.status != 'pending' and (not conn.last_synced or _naive(conn.last_synced) < stale):
                        _sync_broker(conn)
        except Exception as e:
            app.logger.warning('broker keepalive: %s', type(e).__name__)

def _investments_payload(user_id, accounts):
    """Everything the Investments page and net worth need.
    - manual accounts (entered by the user; NOT in the bank-sync net worth -> add them)
    - Plaid accounts of type Investment (already counted in net worth; shown read-only)
    - monthly value history of manual accounts, from their balance snapshots"""
    manual = InvestmentAccount.query.filter_by(user_id=user_id).order_by(InvestmentAccount.id).all()
    snaps = {}
    for sn in InvestmentSnapshot.query.filter_by(user_id=user_id).order_by(InvestmentSnapshot.recorded_at).all():
        snaps.setdefault(sn.account_id, []).append((_naive(sn.recorded_at), sn.balance))
    items = []
    logos = _logo_urls([a.platform for a in manual] + [a.get('inst', '') for a in (accounts or [])])
    for a in manual:
        hist = snaps.get(a.id, [])
        items.append({**_investment_js(a, updated=hist[-1][0] if hist else None), 'logo': logos.get(a.platform, '')})
    manual_total = round(sum(a.balance or 0 for a in manual), 2)

    for i, acc in enumerate(a for a in (accounts or []) if a.get('type') == 'Investment'):
        name = acc.get('name') or 'Investment account'
        val = acc.get('balance', acc.get('bal', 0)) or 0
        items.append({
            'id': f"plaid-{acc.get('id', i)}", 'name': name, 'platform': acc.get('inst', ''),
            'label': acc.get('subtype') or 'Investment', 'ticker': (acc.get('subtype') or name)[:4].upper(),
            'val': val, 'balance': val, 'pct': 0, 'color': _PLAID_INV_COLORS[i % len(_PLAID_INV_COLORS)],
            'source': 'plaid', 'updated': '', 'logo': logos.get(acc.get('inst', ''), ''),
        })

    # Read-only broker connections (SnapTrade, Questrade, IBKR, Kraken)
    conns = BrokerConnection.query.filter_by(user_id=user_id).all()
    broker_total = 0.0
    broker_logos = _logo_urls([a.get('institution', '') for c in conns for a in json.loads(c.cache or '[]')])
    for c in conns:
        for a in json.loads(c.cache or '[]'):
            broker_total += a.get('balance_cad', 0) or 0
            items.append({
                'id': f"broker-{c.id}-{a['key']}", 'connectionId': c.id, 'provider': c.provider,
                'name': a.get('name', ''), 'platform': a.get('institution', ''), 'label': a.get('type', ''),
                'ticker': (a.get('type') or a.get('institution') or '?')[:4].upper(), 'val': a.get('balance_cad', 0),
                'balance': a.get('balance_cad', 0), 'pct': 0, 'color': '#22d3ee', 'source': 'broker',
                'updated': _naive(c.last_synced).strftime('%Y-%m-%d') if c.last_synced else '',
                'logo': broker_logos.get(a.get('institution', ''), ''),
                'positions': sorted(a.get('positions', []), key=lambda p: -(p.get('value_cad') or 0))[:25],
            })
    broker_snaps = {}
    for sn in BrokerSnapshot.query.filter_by(user_id=user_id).order_by(BrokerSnapshot.day).all():
        broker_snaps.setdefault((sn.connection_id, sn.account_key), []).append((sn.day, sn.balance))

    # Month-end value of manual + broker accounts over the last 12 months (only months they existed)
    now = datetime.now()
    labels, values = [], []
    for back in range(11, -1, -1):
        y, mth = now.year, now.month - back
        while mth <= 0:
            mth += 12
            y -= 1
        month_end = datetime(y + (mth == 12), (mth % 12) + 1, 1) - timedelta(seconds=1)
        total, seen = 0.0, False
        for a in manual:
            hist = snaps.get(a.id) or [(_naive(a.created_at) or now, a.balance or 0)]
            known = [b for t, b in hist if t <= month_end]
            if known:
                total += known[-1]
                seen = True
        for hist in broker_snaps.values():
            known = [b for d, b in hist if d <= month_end.date()]
            if known:
                total += known[-1]
                seen = True
        if seen:
            labels.append(datetime(y, mth, 1).strftime('%b'))
            values.append(round(total, 2))
    return {'investments': items, 'manualTotal': manual_total,
            # Not part of the bank sync, so added to net worth
            'externalTotal': round(manual_total + broker_total, 2),
            'investHistory': values, 'investHistoryLabels': labels,
            'brokers': [_broker_public(c) for c in conns], 'snaptradeEnabled': brokers.snaptrade_enabled()}

def _html_safe(obj):
    """HTML-escape every string in a JSON-like structure. The dashboard renders
    bank- and user-supplied text (merchant names, memos, account labels, names)
    with innerHTML, so it must never arrive as markup."""
    import html as _html
    if isinstance(obj, str):
        return _html.escape(obj, quote=True)
    if isinstance(obj, list):
        return [_html_safe(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _html_safe(v) for k, v in obj.items()}
    return obj

def _js(obj):
    """json.dumps that is safe inside an inline <script> (no </script> breakout)."""
    return (json.dumps(_html_safe(obj), indent=2)
            .replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
            .replace('\u2028', '\\u2028').replace('\u2029', '\\u2029'))

def _build_real_data_js(data, user=None):
    """Convert Python data dict to JS const D = {...} that matches vault.html mock field names."""
    raw_accounts = data.get("accounts", [])
    raw_txns     = data.get("transactions", [])
    budget       = data.get("budget", [])
    cat_spend    = data.get("categorySpending", [])
    inv = _investments_payload(user.id, raw_accounts) if user else {'investments': [], 'manualTotal': 0, 'investHistory': [], 'investHistoryLabels': []}
    # Manually tracked investments aren't part of the bank sync: add them to net worth
    net_worth    = round((data.get("netWorth", 0) or 0) + inv['externalTotal'], 2)
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
    logos = _logo_urls([a.get('inst', a.get('name', '')) for a in raw_accounts])
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
            "logo":  logos.get(a.get("inst", a["name"]), ""),
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
            "cat":      CATEGORY_ALIASES.get(t["category"], t["category"]),
            "acc":      acc_name_map.get(t["account"], t["account"]),
            "ico":      CATEGORY_ICONS.get(CATEGORY_ALIASES.get(t["category"], t["category"]), t.get("ico", "folder")),
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
  totalAssets:     {round((data.get("totalAssets", 0) or 0) + inv['externalTotal'], 2)},
  totalDebt:       {total_debt},
  income:          {income},
  spending:        {spending},
  cashFlow:        {cash_flow},
  generated:       {_js(generated)},

  netWorthHistory: [{net_worth}],
  monthLabels:     ["Now"],

  accounts: {_js(accounts)},
  txns: {_js(txns[:200])},
  budget: {_js(budget_js)},
  catLabels: {_js(cat_labels)},
  catAmounts: {json.dumps(cat_amounts)},

  // User profile
  userName:  {_js(user_name)},
  userEmail: {_js(user_email)},

  // Connection status (derived from config)
  connections: {_js(connections)},

  // Wallets list (for frontend wallet management)
  wallets: {_js(wallets_list)},

  // Investment accounts (manual + future integrations)
  investments: {_js(inv['investments'])},
  investHistory: {json.dumps(inv['investHistory'])},
  investHistoryLabels: {_js(inv['investHistoryLabels'])},
  brokers: {_js(inv['brokers'])},
  snaptradeEnabled: {json.dumps(inv['snaptradeEnabled'])},
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
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label>Email</label>
    <input type="email" name="email" placeholder="you@example.com" required>
    <label>Password</label>
    <input type="password" name="password" placeholder="Your password" required>
    <button type="submit" class="btn">Log in</button>
  </form>
  <div class="link"><a href="https://fiscit.com/forgot">Forgot your password?</a></div>
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
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <label>Name</label>
      <input type="text" name="name" placeholder="How should we call you?" required>
      <label>Email</label>
      <input type="email" name="email" placeholder="you@example.com" required>
      <label>Password</label>
      <input type="password" name="password" placeholder="At least 8 characters" required>
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
@csrf_required
def register():
    # Accounts are created on fiscit.com, which verifies the email address.
    # (This route used to create accounts with an unverified email.)
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    return redirect('https://fiscit.com/register')

@app.route('/login', methods=['GET', 'POST'])
@csrf_required
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        if _login_blocked(email):
            return render_template_string(LOGIN_HTML, error='Too many attempts. Please wait 15 minutes and try again.', csrf_token=generate_csrf_token()), 429
        user = _check_login(email, password)
        if not user:
            _record_login_failure(email)
            return render_template_string(LOGIN_HTML, error='Invalid email or password.', csrf_token=generate_csrf_token())
        if not user.email_confirmed:
            return render_template_string(LOGIN_HTML, error='Please confirm your email first — check your inbox for the link from Fiscit.', csrf_token=generate_csrf_token())
        session.permanent = True
        generate_csrf_token()  # Generate CSRF token for the session
        login_user(FlaskUser(user), remember=True)
        return redirect(url_for('index'))
    # Show error from Google OAuth redirect if any
    error = ''
    err_param = request.args.get('error')
    if err_param == 'google_failed':
        error = 'Google sign-in failed. Please try again.'
    elif err_param == 'no_google_data':
        error = 'Could not get your info from Google. Please try again.'
    return render_template_string(LOGIN_HTML, error=error, csrf_token=generate_csrf_token())

@app.route('/logout', methods=['GET', 'POST'])
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/api/auth/logout-all', methods=['POST'])
@login_required
def api_logout_all():
    """Sign out of every device: the dashboard here, fiscit.com, and mobile tokens."""
    # csrf_required lets /api/ through, so check the dashboard's token here
    if not session.get('csrf_token') or request.headers.get('X-CSRF-Token') != session.get('csrf_token'):
        return jsonify({'error': 'CSRF token missing or invalid'}), 403
    user = current_user.model
    user.token_version = (user.token_version or 0) + 1
    db.session.commit()
    app.logger.info('User %s signed out of all devices', user.id)
    logout_user()
    session.clear()
    return jsonify({'ok': True})

@app.route('/api/auth/logout', methods=['POST'])
def api_logout():
    """Logout endpoint for frontend JavaScript — clears session."""
    logout_user()
    session.clear()
    return jsonify({'ok': True})

# ── JWT auth endpoints for mobile app ────────────────────────────────────────
@app.route('/api/auth/login', methods=['POST'])
def api_auth_login():
    """Login via API — returns JWT token for mobile app."""
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    if not email or not password:
        return jsonify({'error': 'Email and password required'}), 400
    if _login_blocked(email):
        return jsonify({'error': 'Too many attempts. Please wait 15 minutes and try again.'}), 429
    user = _check_login(email, password)
    if not user:
        _record_login_failure(email)
        return jsonify({'error': 'Invalid email or password'}), 401
    if not user.email_confirmed:
        return jsonify({'error': 'Please confirm your email first', 'needsVerification': True}), 403
    token = make_jwt(user.id, user.email, user.token_version or 0)
    return jsonify({
        'ok': True,
        'token': token,
        'user': {'id': user.id, 'email': user.email, 'name': user.name or '', 'onboarded': user.onboarded},
    })

@app.route('/api/auth/register', methods=['POST'])
def api_auth_register():
    """Sign-up happens on fiscit.com, which verifies the email address.
    (This endpoint used to auto-confirm any email, allowing account pre-hijacking.)"""
    return jsonify({
        'error': 'Create your account at https://fiscit.com/register, then sign in here.',
        'signupUrl': 'https://fiscit.com/register',
    }), 403

@app.route('/api/auth/me', methods=['GET'])
@api_auth_required
def api_auth_me():
    """Get current user info via JWT or cookie."""
    u = current_user.model
    return jsonify({
        'id': u.id, 'email': u.email, 'name': u.name or '',
        'onboarded': u.onboarded, 'email_confirmed': u.email_confirmed,
    })

# ── SSO: accept JWT from fiscit.com ──────────────────────────────────────
@app.route('/sso')
def sso():
    """Accept a short-lived, single-use handoff JWT from fiscit.com and create a Flask session.
    Only tokens minted for SSO (aud=fiscit-sso, with a jti) are accepted, so a long-lived
    session or mobile token can't be replayed here, and each link works once."""
    token = request.args.get('token')
    if not token:
        app.logger.warning('SSO rejected: no token')
        return redirect(url_for('login'))
    try:
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=['HS256'], audience=SSO_AUDIENCE,
                               options={'require': ['exp', 'sub', 'jti', 'aud']})
        jti = str(payload['jti'])[:64]
        if db.session.get(UsedSsoToken, jti):
            app.logger.warning('SSO rejected: token already used')
            return redirect(url_for('login'))
        db.session.add(UsedSsoToken(jti=jti))
        # Forget used tokens once they could no longer be valid anyway
        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        UsedSsoToken.query.filter(UsedSsoToken.used_at < cutoff).delete()
        db.session.commit()
        user = db.session.get(UserModel, int(payload['sub']))
        if not user:
            app.logger.warning('SSO rejected: user %s not found', payload['sub'])
            return redirect(url_for('login'))
        if not _token_version_ok(user, payload):
            app.logger.warning('SSO rejected: session was revoked')
            return redirect(url_for('login'))
        session.permanent = True
        login_user(FlaskUser(user), remember=True)
        return redirect(url_for('index'))
    except pyjwt.ExpiredSignatureError:
        app.logger.warning('SSO rejected: token expired')
        return redirect(url_for('login'))
    except pyjwt.InvalidSignatureError:
        app.logger.warning('SSO rejected: bad signature (JWT_SECRET differs between Vercel and Railway?)')
        return redirect(url_for('login'))
    except Exception as e:
        db.session.rollback()
        app.logger.warning('SSO rejected: %s', type(e).__name__)
        return redirect(url_for('login'))

# ── Google OAuth routes ──────────────────────────────────────────────────────
def _google_user(google_id, email, name):
    """Find or create the user for a verified Google identity.
    An existing account for the same email is linked only safely:
      - unconfirmed (someone may have pre-registered the address): Google proves
        ownership, so drop that password and confirm the account;
      - already bound to a different Google account: refuse."""
    user = UserModel.query.filter_by(google_id=google_id).first()
    if user:
        return user
    user = UserModel.query.filter_by(email=email).first()
    if user:
        if user.google_id and user.google_id != google_id:
            return None
        if not user.email_confirmed:
            user.password_hash = None
            user.email_confirmed = True
        user.google_id = google_id
    else:
        user = UserModel(email=email, name=name, google_id=google_id)
        user.password_hash = None  # Google-only account
        user.email_confirmed = True
        db.session.add(user)
    db.session.commit()
    return user

@app.route('/api/auth/google', methods=['POST'])
def api_auth_google():
    """Google OAuth for mobile — accepts an ID token from expo-auth-session."""
    import google.auth.transport.requests as google_transport
    from google.oauth2 import id_token as google_id_token

    data = request.get_json(silent=True) or {}
    id_token_str = data.get('id_token') or ''
    if not id_token_str:
        return jsonify({'error': 'Missing id_token'}), 400

    try:
        # Verify the Google ID token
        google_client_id = os.getenv('GOOGLE_CLIENT_ID', '')
        idinfo = google_id_token.verify_oauth2_token(
            id_token_str,
            google_transport.Request(),
            google_client_id,
        )
    except Exception as e:
        print(f"Google ID token verification failed: {e}")
        return jsonify({'error': 'Invalid Google token'}), 401

    google_id = idinfo.get('sub')
    email = (idinfo.get('email') or '').strip().lower()
    name = (idinfo.get('name') or '').strip()

    if not google_id or not email:
        return jsonify({'error': 'Missing Google account info'}), 400
    if idinfo.get('email_verified') is not True:
        return jsonify({'error': 'Your Google email address is not verified'}), 401

    user = _google_user(google_id, email, name)
    if not user:
        return jsonify({'error': 'This email is linked to a different Google account'}), 409

    token = make_jwt(user.id, user.email, user.token_version or 0)
    return jsonify({
        'ok': True,
        'token': token,
        'user': {'id': user.id, 'email': user.email, 'name': user.name or '', 'onboarded': user.onboarded},
    })


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
        # Fallback: verify the id_token properly using Google's verification
        id_token_raw = token.get('id_token')
        if id_token_raw:
            try:
                import google.auth.transport.requests as google_transport
                from google.oauth2 import id_token as google_id_token
                google_client_id = os.getenv('GOOGLE_CLIENT_ID', '')
                userinfo = google_id_token.verify_oauth2_token(
                    id_token_raw,
                    google_transport.Request(),
                    google_client_id,
                )
            except Exception:
                # If verification fails, redirect with error
                return redirect(url_for('login', error='google_failed'))

    google_id = userinfo.get('sub')
    email = (userinfo.get('email') or '').strip().lower()
    name = (userinfo.get('name') or '').strip()

    if not google_id or not email or userinfo.get('email_verified') is not True:
        return redirect(url_for('login', error='no_google_data'))

    user = _google_user(google_id, email, name)
    if not user:
        return redirect(url_for('login', error='google_failed'))

    session.permanent = True
    login_user(FlaskUser(user), remember=True)
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
@api_auth_required
def api_profile():
    data = request.get_json(silent=True) or {}
    name = str(data.get('name', '')).strip()[:100]
    if name:
        current_user.model.name = name
        db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/onboarding/complete', methods=['POST'])
@api_auth_required
def api_onboarding_complete():
    """Mark onboarding as complete for the current user."""
    current_user.model.onboarded = True
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/onboarding/reset', methods=['POST'])
@api_auth_required
def api_onboarding_reset():
    """Reset onboarding so user can replay it."""
    current_user.model.onboarded = False
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/tour/seen', methods=['POST'])
@api_auth_required
def api_tour_seen():
    """Remember that the guided tour was offered (taken or skipped)."""
    current_user.model.tour_seen = True
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/status')
@api_auth_required
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

def _api_investments(uid, accounts):
    p = _investments_payload(uid, accounts)
    return {'investments': p['investments'], 'investHistory': p['investHistory'],
            'investHistoryLabels': p['investHistoryLabels'], 'manualTotal': p['manualTotal'],
            'externalTotal': p['externalTotal'], 'brokers': p['brokers'], 'snaptradeEnabled': p['snaptradeEnabled']}

@app.route('/api/data')
@api_auth_required
def api_data():
    uid = current_user.model.id
    state = _get_state(uid)
    with _user_state_lock:
        data = state['data']
    if not data:
        return jsonify({
            'net_worth': 0,
            'accounts': [],
            'transactions': [],
            'userName': _html_safe(current_user.model.name or ''),
            'userEmail': _html_safe(current_user.model.email or ''),
            'onboarded': current_user.model.onboarded,
            'tourSeen': current_user.model.tour_seen,
            **_html_safe(_api_investments(uid, [])),
            'netWorth': _api_investments(uid, [])['externalTotal'],
        })
    # Normalize field names to match vault.html mock format
    result = _normalize_api_data(data)
    # Override userName/userEmail with real DB values
    result['userName'] = current_user.model.name or ''
    result['userEmail'] = current_user.model.email or ''
    result['onboarded'] = current_user.model.onboarded
    result['tourSeen'] = current_user.model.tour_seen
    inv = _api_investments(uid, data.get('accounts', []))
    result.update(inv)
    result['netWorth'] = round((data.get('netWorth', 0) or 0) + inv['externalTotal'], 2)
    result['totalAssets'] = round((data.get('totalAssets', 0) or 0) + inv['externalTotal'], 2)
    return jsonify(_html_safe(result))

@app.route('/api/refresh', methods=['POST'])
@api_auth_required
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
@api_auth_required
def api_list_wallets():
    uid = current_user.model.id
    wallets = CryptoWallet.query.filter_by(user_id=uid).all()
    return jsonify({'ok': True, 'wallets': [{'chain': w.chain, 'address': w.address, 'label': w.label} for w in wallets]})

@app.route('/api/wallets', methods=['POST'])
@api_auth_required
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
    # Catch a wrong chain or a pasted typo now, rather than a wallet that never syncs
    formats = {
        'ethereum': (r'0x[0-9a-fA-F]{40}', 'An Ethereum address starts with 0x followed by 40 characters.'),
        'bitcoin':  (r'(bc1[02-9ac-hj-np-z]{11,87}|[13][1-9A-HJ-NP-Za-km-z]{25,34})', 'That does not look like a Bitcoin address (bc1…, 1… or 3…).'),
        'solana':   (r'[1-9A-HJ-NP-Za-km-z]{32,44}', 'That does not look like a Solana address.'),
    }
    if chain in formats and not re.fullmatch(formats[chain][0], address):
        return jsonify({'ok': False, 'error': formats[chain][1]}), 400
    if CryptoWallet.query.filter_by(user_id=uid, chain=chain, address=address).first():
        return jsonify({'ok': False, 'error': 'This wallet is already added.'}), 400
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
@api_auth_required
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

# ── Investment accounts ────────────────────────────────────────────────────
@app.route('/api/investments', methods=['GET'])
@api_auth_required
def api_get_investments():
    uid = current_user.model.id
    accts = InvestmentAccount.query.filter_by(user_id=uid).all()
    return jsonify({'ok': True, 'investments': [{'id': a.id, 'platform': a.platform, 'label': a.label, 'balance': a.balance} for a in accts]})

@app.route('/api/investments', methods=['POST'])
@api_auth_required
def api_add_investment():
    uid = current_user.model.id
    data = request.get_json(silent=True) or {}
    platform = data.get('platform', '').strip()
    label = data.get('label', '').strip()
    balance = data.get('balance', 0)
    if not platform:
        return jsonify({'ok': False, 'error': 'Platform is required.'}), 400
    try:
        balance = float(balance)
    except (TypeError, ValueError):
        balance = 0
    if not (0 <= balance < 1e12):
        return jsonify({'ok': False, 'error': 'Enter a valid balance.'}), 400
    acct = InvestmentAccount(user_id=uid, platform=platform[:100], label=(label or platform)[:100], balance=balance)
    db.session.add(acct)
    db.session.flush()
    db.session.add(InvestmentSnapshot(account_id=acct.id, user_id=uid, balance=balance))
    db.session.commit()
    _ensure_logos_async([acct.platform])
    return jsonify({'ok': True, 'id': acct.id})

# ── Broker connection routes (all read-only on the broker side) ─────────────
@app.route('/api/brokers', methods=['GET'])
@api_auth_required
def api_brokers():
    conns = BrokerConnection.query.filter_by(user_id=current_user.model.id).all()
    return jsonify({'ok': True, 'brokers': [_broker_public(c) for c in conns], 'snaptradeEnabled': brokers.snaptrade_enabled()})

@app.route('/api/brokers/<provider>', methods=['POST'])
@api_auth_required
def api_broker_connect(provider):
    """Direct connections: questrade (token), ibkr (Flex token + query id), kraken (Query Funds key)."""
    uid = current_user.model.id
    if provider not in ('questrade', 'ibkr', 'kraken'):
        return jsonify({'ok': False, 'error': 'Unknown provider.'}), 404
    if BrokerConnection.query.filter_by(user_id=uid).count() >= 10:
        return jsonify({'ok': False, 'error': 'You can connect up to 10 accounts.'}), 400
    try:
        creds, accounts = brokers.PROVIDERS[provider]['connect'](request.get_json(silent=True) or {})
    except brokers.BrokerError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        app.logger.error('Broker connect %s failed: %s', provider, type(e).__name__)
        return jsonify({'ok': False, 'error': 'Could not connect. Please try again.'}), 502
    conn = BrokerConnection(user_id=uid, provider=provider, label=brokers.PROVIDERS[provider]['label'],
                            credentials=json.dumps(creds), status='ok')
    db.session.add(conn)
    db.session.flush()
    _record_broker_sync(conn, creds, accounts)
    _ensure_logos_async([a.get('institution') for a in accounts])
    return jsonify({'ok': True, 'accounts': len(accounts)})

@app.route('/api/brokers/snaptrade/connect', methods=['POST'])
@api_auth_required
def api_snaptrade_connect():
    """Open SnapTrade's portal in read-only mode (Wealthsimple, Questrade, IBKR…)."""
    if not brokers.snaptrade_enabled():
        return jsonify({'ok': False, 'error': 'SnapTrade is not set up yet.'}), 503
    uid = current_user.model.id
    conn = BrokerConnection.query.filter_by(user_id=uid, provider='snaptrade').first()
    try:
        if not conn:
            creds = brokers.snaptrade_register(f'fiscit-{uid}-{uuid.uuid4().hex[:12]}')
            conn = BrokerConnection(user_id=uid, provider='snaptrade', label='SnapTrade',
                                    credentials=json.dumps(creds), status='pending')
            db.session.add(conn)
            db.session.commit()
        url = brokers.snaptrade_portal_url(json.loads(conn.credentials), url_for('snaptrade_done', _external=True, _scheme='https'))
    except brokers.BrokerError as e:
        return jsonify({'ok': False, 'error': str(e)}), 502
    return jsonify({'ok': True, 'url': url})

@app.route('/brokers/snaptrade/done')
@login_required
def snaptrade_done():
    conn = BrokerConnection.query.filter_by(user_id=current_user.model.id, provider='snaptrade').first()
    if conn:
        conn.status = 'ok'
        db.session.commit()
        _sync_broker(conn)
        if conn.status != 'ok' or not json.loads(conn.cache or '[]'):
            _retry_broker_later(conn.id)
    return redirect(url_for('index', view='investments'))

@app.route('/api/brokers/<int:conn_id>/sync', methods=['POST'])
@api_auth_required
def api_broker_sync(conn_id):
    conn = BrokerConnection.query.filter_by(id=conn_id, user_id=current_user.model.id).first()
    if not conn:
        return jsonify({'ok': False, 'error': 'Not found.'}), 404
    if conn.status == 'pending':
        conn.status = 'ok'
    _sync_broker(conn)
    if conn.status != 'ok':
        _retry_broker_later(conn.id, delays=(30, 90))
    return jsonify({'ok': conn.status == 'ok', 'error': conn.last_error})

@app.route('/api/brokers/<int:conn_id>', methods=['DELETE'])
@api_auth_required
def api_broker_delete(conn_id):
    conn = BrokerConnection.query.filter_by(id=conn_id, user_id=current_user.model.id).first()
    if not conn:
        return jsonify({'ok': False, 'error': 'Not found.'}), 404
    _delete_broker_connection(conn)
    db.session.commit()
    return jsonify({'ok': True})

def _delete_broker_connection(conn):
    if conn.provider == 'snaptrade' and brokers.snaptrade_enabled():
        brokers.snaptrade_delete_user(json.loads(conn.credentials or '{}'))  # also revokes its broker links
    BrokerSnapshot.query.filter_by(connection_id=conn.id).delete()
    db.session.delete(conn)


@app.route('/api/investments/<int:acct_id>', methods=['PUT'])
@api_auth_required
def api_update_investment(acct_id):
    """Update a manual account's balance (and optionally its label); keeps history."""
    uid = current_user.model.id
    a = InvestmentAccount.query.filter_by(id=acct_id, user_id=uid).first()
    if not a:
        return jsonify({'ok': False, 'error': 'Not found.'}), 404
    data = request.get_json(silent=True) or {}
    try:
        balance = float(data.get('balance'))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Enter a valid balance.'}), 400
    if not (0 <= balance < 1e12):
        return jsonify({'ok': False, 'error': 'Enter a valid balance.'}), 400
    label = str(data.get('label') or '').strip()[:100]
    # First edit of an account created before history existed: keep its original value
    if not InvestmentSnapshot.query.filter_by(account_id=a.id).first():
        db.session.add(InvestmentSnapshot(account_id=a.id, user_id=uid, balance=a.balance or 0,
                                          recorded_at=a.created_at or datetime.now(timezone.utc)))
    a.balance = balance
    if label:
        a.label = label
    db.session.add(InvestmentSnapshot(account_id=a.id, user_id=uid, balance=balance))
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/investments/<int:acct_id>', methods=['DELETE'])
@api_auth_required
def api_delete_investment(acct_id):
    uid = current_user.model.id
    a = InvestmentAccount.query.filter_by(id=acct_id, user_id=uid).first()
    if not a:
        return jsonify({'ok': False, 'error': 'Not found.'}), 404
    InvestmentSnapshot.query.filter_by(account_id=a.id).delete()
    db.session.delete(a)
    db.session.commit()
    return jsonify({'ok': True})

def _plaid_remove_items(tokens):
    """Call Plaid /item/remove for each access token; failures are logged, not raised."""
    import requests as req
    cid, secret = os.getenv('PLAID_CLIENT_ID'), os.getenv('PLAID_SECRET')
    if not (cid and secret and tokens):
        return
    env = os.getenv('PLAID_ENV', 'production')
    base = ('https://production.plaid.com' if env == 'production'
            else 'https://development.plaid.com' if env == 'development'
            else 'https://sandbox.plaid.com')
    for t in tokens:
        try:
            r = req.post(f'{base}/item/remove', json={'client_id': cid, 'secret': secret, 'access_token': t}, timeout=10)
            if r.status_code != 200:
                app.logger.warning('Plaid item/remove returned %s', r.status_code)
        except Exception as e:
            app.logger.warning('Plaid item/remove failed: %s', type(e).__name__)

# ── Delete account ──────────────────────────────────────────────────────────
@app.route('/api/auth/delete', methods=['POST'])
@api_auth_required
def api_delete_account():
    """Permanently delete the user's account and all associated data."""
    data = request.get_json(silent=True) or {}
    confirmation = data.get('confirmation', '')
    uid = current_user.model.id
    email = current_user.model.email
    # Require user to type their email to confirm
    if confirmation.strip().lower() != email.strip().lower():
        return jsonify({'ok': False, 'error': 'Confirmation does not match email.'}), 400

    # Revoke bank access at Plaid first (best effort): deleting rows alone
    # would leave the items live on Plaid's side.
    tokens = [c.access_token for c in PlaidConnection.query.filter_by(user_id=uid).all()]
    try:
        row = db.session.execute(db.text('SELECT plaid_token FROM vault_credentials WHERE user_id = :uid'), {'uid': uid}).first()
        if row and row[0]:
            import crypto_util
            tokens.append(crypto_util.decrypt(row[0]))
    except Exception:
        db.session.rollback()
    _plaid_remove_items(set(t for t in tokens if t))

    try:
        CryptoWallet.query.filter_by(user_id=uid).delete()
        for bc in BrokerConnection.query.filter_by(user_id=uid).all():
            _delete_broker_connection(bc)
        InvestmentSnapshot.query.filter_by(user_id=uid).delete()
        PasswordReset.query.filter_by(user_id=uid).delete()
        InvestmentAccount.query.filter_by(user_id=uid).delete()
        PlaidConnection.query.filter_by(user_id=uid).delete()
        WiseConnection.query.filter_by(user_id=uid).delete()
        if db.engine.dialect.name == 'postgresql':
            _delete_postgres_user_rows(uid)
        UserModel.query.filter_by(id=uid).delete()
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.error('Account deletion failed for user %s: %s', uid, e)
        return jsonify({'ok': False, 'error': 'We could not delete your account. Please try again or contact support.'}), 500

    # Only log out once the data is actually gone
    logout_user()
    session.clear()
    return jsonify({'ok': True})


def _delete_postgres_user_rows(uid):
    """Rows in fiscit.com's tables, plus any table with a foreign key to users."""
    for table in ('email_verifications', 'vault_credentials'):
        if db.session.execute(db.text('SELECT to_regclass(:t)'), {'t': table}).scalar():
            db.session.execute(db.text(f'DELETE FROM {table} WHERE user_id = :uid'), {'uid': uid})
    # Table/column names come from the catalog, never from user input
    fks = db.session.execute(db.text("""
            SELECT DISTINCT kcu.table_name, kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY' AND ccu.table_name = 'users' AND tc.table_schema = 'public'
        """)).fetchall()
    for table, column in fks:
        if table in ('email_verifications', 'vault_credentials', 'users'):
            continue
        db.session.execute(db.text(f'DELETE FROM "{table}" WHERE "{column}" = :uid'), {'uid': uid})

PLAID_COUNTRY_CODES = [c.strip().upper() for c in os.getenv('PLAID_COUNTRY_CODES', 'CA').split(',') if c.strip()]

def _plaid_failure(endpoint, r):
    """Log Plaid's own error (code/type/message, never secrets) and give the user
    Plaid's display message when it has one."""
    try:
        err = r.json()
    except Exception:
        err = {}
    app.logger.error('Plaid %s %s: %s/%s %s (request_id=%s)', endpoint, r.status_code,
                     err.get('error_type'), err.get('error_code'), err.get('error_message'), err.get('request_id'))
    msg = err.get('display_message') or 'Plaid could not start the bank connection. Please try again later.'
    return jsonify({'ok': False, 'error': msg, 'code': err.get('error_code')}), 502

@app.route('/api/plaid/link_token', methods=['POST'])
@api_auth_required
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
            # Only countries enabled on our Plaid production account (CA today; US
            # needs separate approval and makes the whole request fail with 400).
            'country_codes': PLAID_COUNTRY_CODES,
            'language': 'en',
            'user': {'client_user_id': str(uid)},
            'products': ['transactions'],
        }, timeout=15)
        if r.status_code != 200:
            return _plaid_failure('link/token/create', r)
        return jsonify({'ok': True, 'link_token': r.json()['link_token']})
    except Exception as e:
        app.logger.error('Plaid link/token/create failed: %s', type(e).__name__)
        return jsonify({'ok': False, 'error': 'Could not reach Plaid. Please try again.'}), 502

@app.route('/api/plaid/exchange', methods=['POST'])
@api_auth_required
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
        if r.status_code != 200:
            return _plaid_failure('item/public_token/exchange', r)
        data = r.json()
        access_token = data['access_token']
        # Save to DB for this user (support multiple banks)
        item_id = data.get('item_id', '')
        # Try to get institution name from Plaid
        institution = ''
        try:
            ir = req.post(f'{base}/item/get', json={
                'client_id': plaid_client,
                'secret':    plaid_secret,
                'access_token': access_token,
            }, timeout=10)
            if ir.ok:
                idata = ir.json().get('item', {})
                inst_id = idata.get('institution_id', '')
                if inst_id:
                    # Get institution name
                    inr = req.post(f'{base}/institutions/get_by_id', json={
                        'client_id': plaid_client,
                        'secret':    plaid_secret,
                        'institution_id': inst_id,
                        'country_codes': PLAID_COUNTRY_CODES,
                    }, timeout=10)
                    if inr.ok:
                        institution = inr.json().get('institution', {}).get('name', inst_id)
        except Exception:
            pass
        pc = PlaidConnection(user_id=uid, access_token=access_token, item_id=item_id, institution=institution)
        db.session.add(pc)
        db.session.commit()
        # Trigger refresh
        cfg = build_user_config(current_user)
        t = threading.Thread(target=fetch_data, args=(uid, cfg), daemon=True)
        t.start()
        return jsonify({'ok': True, 'msg': 'Account connected! Refreshing data...'})
    except Exception as e:
        app.logger.error('Request failed: %s', e)
        return jsonify({'ok': False, 'error': 'Something went wrong. Please try again.'}), 500

@app.route('/api/wise/test', methods=['POST'])
@api_auth_required
def api_wise_test():
    """Test Wise API token — validates AND saves the connection on success.
    This is the intended UX: user enters their Wise token, we validate it and
    save it in one step so they don't have to submit twice."""
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
        app.logger.error('Request failed: %s', e)
        return jsonify({'ok': False, 'error': 'Something went wrong. Please try again.'}), 500

# Keep Questrade tokens alive between visits (defined above; production only)
if IS_PROD:
    threading.Thread(target=_broker_keepalive, daemon=True).start()

if __name__ == '__main__':
    port  = int(os.getenv('PORT', 5050))
    debug = os.getenv('FLASK_DEBUG', '0') == '1'
    print(f"\n💰 Fiscit → http://localhost:{port}\n")
    app.run(host='0.0.0.0', port=port, debug=debug, use_reloader=False)
