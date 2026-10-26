"""Read-only investment connections: SnapTrade, Questrade, Interactive Brokers (Flex), Kraken.

Every provider exposes:
    connect(form)      -> (credentials, accounts)   validates what the user pasted
    sync(credentials)  -> (credentials, accounts)   credentials may rotate (Questrade)

`accounts` is a list of dicts in one shape:
    {key, name, institution, type, currency, balance_cad, positions: [
        {symbol, description, quantity, price, value_cad, currency}]}

Only read endpoints are ever called. Nothing here can place orders, withdraw or
transfer: SnapTrade connections are created with connectionType "read", Questrade
personal tokens only carry read scopes, IBKR Flex is a statement service, and the
Kraken key only needs "Query Funds".
"""
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests

TIMEOUT = 15


class BrokerError(Exception):
    """Message is safe to show to the user."""


def _usd_to_cad():
    try:
        return float(os.getenv('USD_TO_CAD', '1.38'))
    except ValueError:
        return 1.38


def _to_cad(amount, currency):
    amount = float(amount or 0)
    return amount * _usd_to_cad() if (currency or 'CAD').upper() == 'USD' else amount


# ═══════════════════════════ Questrade ═══════════════════════════
# Personal app: the user registers an app in Questrade's API Centre (read scopes
# only), generates a token and pastes it. Each redeem returns a NEW refresh token
# (the old one dies) and the api_server to use: always persist the latest.

def _qt_redeem(refresh_token):
    try:
        r = requests.get('https://login.questrade.com/oauth2/token',
                         params={'grant_type': 'refresh_token', 'refresh_token': refresh_token.strip()},
                         timeout=TIMEOUT)
    except requests.RequestException:
        raise BrokerError('Could not reach Questrade. Please try again.')
    if r.status_code != 200:
        raise BrokerError('Questrade rejected this token. Generate a new one in Questrade → API Centre and paste it here.')
    d = r.json()
    return {
        'refresh_token': d['refresh_token'],
        'access_token': d['access_token'],
        'api_server': d['api_server'].rstrip('/') + '/',
        'expires_at': time.time() + int(d.get('expires_in', 1800)) - 60,
    }


def _qt_get(creds, path):
    r = requests.get(creds['api_server'] + path, headers={'Authorization': f"Bearer {creds['access_token']}"}, timeout=TIMEOUT)
    if r.status_code == 401:
        raise PermissionError
    if r.status_code != 200:
        raise BrokerError(f'Questrade returned an error ({r.status_code}).')
    return r.json()


def _qt_accounts(creds):
    out = []
    for acc in _qt_get(creds, 'v1/accounts').get('accounts', []):
        number = str(acc.get('number'))
        bal = _qt_get(creds, f'v1/accounts/{number}/balances')
        combined = next((b for b in bal.get('combinedBalances', []) if b.get('currency') == 'CAD'), None)
        equity = combined.get('totalEquity', 0) if combined else sum(
            _to_cad(b.get('totalEquity', 0), b.get('currency')) for b in bal.get('perCurrencyBalances', []))
        raw = _qt_get(creds, f'v1/accounts/{number}/positions').get('positions', [])
        # Market values are in each security's currency: look the currencies up in one call
        ids = ','.join(str(p['symbolId']) for p in raw if p.get('symbolId'))
        symbols = {s['symbolId']: s for s in _qt_get(creds, f'v1/symbols?ids={ids}').get('symbols', [])} if ids else {}
        positions = []
        for p in raw:
            info = symbols.get(p.get('symbolId'), {})
            cur = info.get('currency') or 'CAD'
            positions.append({
                'symbol': p.get('symbol', ''), 'description': info.get('description') or p.get('symbol', ''),
                'quantity': p.get('openQuantity', 0), 'price': p.get('currentPrice', 0),
                'value_cad': round(_to_cad(p.get('currentMarketValue', 0), cur), 2), 'currency': cur,
            })
        out.append({
            'key': f'qt-{number}', 'name': f"{acc.get('type', 'Account')} · Questrade",
            'institution': 'Questrade', 'type': acc.get('type', ''), 'currency': 'CAD',
            'balance_cad': round(float(equity or 0), 2), 'positions': positions,
        })
    return out


def questrade_connect(form):
    token = (form.get('token') or '').strip()
    if not token or len(token) > 200:
        raise BrokerError('Paste the token generated in Questrade → API Centre.')
    creds = _qt_redeem(token)
    return questrade_sync(creds)


def questrade_sync(creds, persist=lambda c: None):
    # Each redeem kills the previous refresh token: persist the new one right away,
    # before anything else can fail, or the connection would be lost.
    if not creds.get('access_token') or time.time() > creds.get('expires_at', 0):
        creds = _qt_redeem(creds['refresh_token'])
        persist(creds)
    try:
        accounts = _qt_accounts(creds)
    except PermissionError:
        creds = _qt_redeem(creds['refresh_token'])
        persist(creds)
        accounts = _qt_accounts(creds)
    return creds, accounts


# ═══════════════════════════ Interactive Brokers (Flex Web Service) ═══════════════════════════
# The user enables Flex Web Service, creates an Activity Flex Query (Open Positions,
# Cash Report and Net Asset Value sections) and pastes the token + query ID.

FLEX_BASE = 'https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService'


def _flex_statement(token, query_id):
    try:
        r = requests.get(f'{FLEX_BASE}/SendRequest', params={'t': token, 'q': query_id, 'v': '3'}, timeout=TIMEOUT)
        root = ET.fromstring(r.text)
    except (requests.RequestException, ET.ParseError):
        raise BrokerError('Could not reach Interactive Brokers. Please try again.')
    if root.findtext('Status') != 'Success':
        raise BrokerError('Interactive Brokers refused the request: ' + (root.findtext('ErrorMessage') or 'check the token and query ID.'))
    ref = root.findtext('ReferenceCode')
    url = root.findtext('Url') or f'{FLEX_BASE}/GetStatement'
    for _ in range(6):  # the statement is generated asynchronously
        time.sleep(3)
        s = requests.get(url, params={'t': token, 'q': ref, 'v': '3'}, timeout=30)
        try:
            doc = ET.fromstring(s.text)
        except ET.ParseError:
            continue
        if doc.tag == 'FlexQueryResponse':
            return doc
        code = doc.findtext('ErrorCode')
        if code not in ('1019', '1018'):  # 1019 = statement generation in progress
            raise BrokerError('Interactive Brokers: ' + (doc.findtext('ErrorMessage') or 'statement unavailable.'))
    raise BrokerError('Interactive Brokers is still preparing the statement. Try again in a minute.')


def _flex_accounts(doc):
    out = []
    for stmt in doc.iter('FlexStatement'):
        acct_id = stmt.get('accountId', '')
        info = stmt.find('AccountInformation')
        base = (info.get('currency') if info is not None else None) or 'CAD'
        acct_type = (info.get('accountType') if info is not None else '') or 'Account'
        positions = []
        for p in stmt.iter('OpenPosition'):
            cur = p.get('currency', base)
            value = float(p.get('positionValue') or 0)
            positions.append({
                'symbol': p.get('symbol', ''), 'description': p.get('description', ''),
                'quantity': float(p.get('position') or 0), 'price': float(p.get('markPrice') or 0),
                'value_cad': round(_to_cad(value * float(p.get('fxRateToBase') or 1), base), 2), 'currency': cur,
            })
        # Prefer NAV (positions + cash); fall back to positions + cash report
        total = None
        nav = [e for e in stmt.iter('EquitySummaryByReportDateInBase')]
        if nav:
            total = float(nav[-1].get('total') or 0)
        if total is None:
            cash = sum(float(c.get('endingCash') or 0) for c in stmt.iter('CashReportCurrency') if c.get('currency') == 'BASE_SUMMARY')
            total = sum(float(p.get('positionValue') or 0) * float(p.get('fxRateToBase') or 1) for p in stmt.iter('OpenPosition')) + cash
        out.append({
            'key': f'ibkr-{acct_id}', 'name': f'{acct_type} · Interactive Brokers',
            'institution': 'Interactive Brokers', 'type': acct_type, 'currency': base,
            'balance_cad': round(_to_cad(total, base), 2), 'positions': positions,
        })
    if not out:
        raise BrokerError('The Flex query returned no account. Make sure it includes Open Positions and Net Asset Value.')
    return out


def ibkr_connect(form):
    token, query_id = (form.get('token') or '').strip(), (form.get('query_id') or '').strip()
    if not token.isalnum() or len(token) > 100 or not query_id.isdigit():
        raise BrokerError('Enter the Flex Web Service token and the numeric Flex Query ID.')
    creds = {'token': token, 'query_id': query_id}
    return creds, _flex_accounts(_flex_statement(token, query_id))


def ibkr_sync(creds, persist=None):
    return creds, _flex_accounts(_flex_statement(creds['token'], creds['query_id']))


# ═══════════════════════════ Kraken ═══════════════════════════
# API key with only "Query Funds" enabled. We only call Balance (private) and
# Ticker (public, for prices).

def _kraken_private(creds, method):
    path = f'/0/private/{method}'
    nonce = str(int(time.time() * 1000))
    body = urllib.parse.urlencode({'nonce': nonce})
    msg = path.encode() + hashlib.sha256((nonce + body).encode()).digest()
    try:
        sig = base64.b64encode(hmac.new(base64.b64decode(creds['private_key']), msg, hashlib.sha512).digest()).decode()
    except Exception:
        raise BrokerError('The Kraken private key is not valid.')
    try:
        r = requests.post('https://api.kraken.com' + path, data=body, timeout=TIMEOUT, headers={
            'API-Key': creds['api_key'], 'API-Sign': sig, 'Content-Type': 'application/x-www-form-urlencoded'})
        d = r.json()
    except (requests.RequestException, ValueError):
        raise BrokerError('Could not reach Kraken. Please try again.')
    if d.get('error'):
        raise BrokerError('Kraken: ' + ', '.join(d['error']) + ' (the key needs the "Query Funds" permission).')
    return d.get('result', {})


def _kraken_asset(code):
    code = code.split('.')[0]                    # staking variants: ETH2.S, DOT.S …
    if len(code) == 4 and code[0] in 'XZ' and code not in ('XTZ',):
        code = code[1:]
    return {'XBT': 'BTC', 'XDG': 'DOGE'}.get(code, code)


def _kraken_price_cad(asset):
    if asset == 'CAD':
        return 1.0
    if asset in ('USD', 'USDT', 'USDC'):
        return _usd_to_cad()
    k = {'BTC': 'XBT', 'DOGE': 'XDG'}.get(asset, asset)
    for quote, mult in (('CAD', 1.0), ('USD', _usd_to_cad())):
        try:
            d = requests.get('https://api.kraken.com/0/public/Ticker', params={'pair': k + quote}, timeout=TIMEOUT).json()
            if not d.get('error') and d.get('result'):
                return float(next(iter(d['result'].values()))['c'][0]) * mult
        except Exception:
            continue
    return 0.0


def _kraken_accounts(creds):
    positions, total = [], 0.0
    for code, amount in _kraken_private(creds, 'Balance').items():
        qty = float(amount or 0)
        if qty <= 0:
            continue
        asset = _kraken_asset(code)
        price = _kraken_price_cad(asset)
        value = round(qty * price, 2)
        total += value
        positions.append({'symbol': asset, 'description': asset, 'quantity': qty, 'price': price,
                          'value_cad': value, 'currency': 'CAD'})
    positions.sort(key=lambda p: -p['value_cad'])
    return [{'key': 'kraken', 'name': 'Kraken', 'institution': 'Kraken', 'type': 'Crypto', 'currency': 'CAD',
             'balance_cad': round(total, 2), 'positions': positions}]


def kraken_connect(form):
    creds = {'api_key': (form.get('api_key') or '').strip(), 'private_key': (form.get('private_key') or '').strip()}
    if not creds['api_key'] or not creds['private_key'] or len(creds['api_key']) > 200 or len(creds['private_key']) > 200:
        raise BrokerError('Paste both the API key and the private key from Kraken.')
    return creds, _kraken_accounts(creds)


def kraken_sync(creds, persist=None):
    return creds, _kraken_accounts(creds)


# ═══════════════════════════ SnapTrade ═══════════════════════════
# One integration for Wealthsimple, Questrade, IBKR, BMO, CIBC… The user signs in to
# their broker in SnapTrade's portal; we only keep SnapTrade's per-user secret.

SNAP_BASE = 'https://api.snaptrade.com'


def snaptrade_enabled():
    return bool(os.getenv('SNAPTRADE_CLIENT_ID') and os.getenv('SNAPTRADE_CONSUMER_KEY'))


def _snap(method, path, query=None, body=None):
    """Signed SnapTrade request: HMAC-SHA256 (consumer key) over the JSON of
    {content, path, query}, sent in the Signature header."""
    params = {'clientId': os.getenv('SNAPTRADE_CLIENT_ID'), 'timestamp': str(int(time.time()))}
    params.update(query or {})
    query_string = urllib.parse.urlencode(params)
    full_path = '/api/v1' + path
    sig_payload = json.dumps({'content': body, 'path': full_path, 'query': query_string},
                             separators=(',', ':'), sort_keys=True)
    signature = base64.b64encode(hmac.new(os.getenv('SNAPTRADE_CONSUMER_KEY', '').encode(),
                                          sig_payload.encode(), hashlib.sha256).digest()).decode()
    try:
        r = requests.request(method, f'{SNAP_BASE}{full_path}?{query_string}', timeout=TIMEOUT,
                             headers={'Signature': signature, 'Content-Type': 'application/json'},
                             data=json.dumps(body, separators=(',', ':'), sort_keys=True) if body is not None else None)
    except requests.RequestException:
        raise BrokerError('Could not reach SnapTrade. Please try again.')
    if r.status_code >= 400:
        raise BrokerError(f'SnapTrade returned an error ({r.status_code}).')
    return r.json() if r.content else {}


def snaptrade_register(user_key):
    d = _snap('POST', '/snapTrade/registerUser', body={'userId': user_key})
    return {'user_id': d['userId'], 'user_secret': d['userSecret']}


def snaptrade_portal_url(creds, redirect_url):
    d = _snap('POST', '/snapTrade/login', query={'userId': creds['user_id'], 'userSecret': creds['user_secret']},
              body={'connectionType': 'read', 'customRedirect': redirect_url})
    url = d.get('redirectURI') or d.get('loginRedirectURI')
    if not url:
        raise BrokerError('SnapTrade did not return a connection link.')
    return url


def snaptrade_sync(creds, persist=None):
    q = {'userId': creds['user_id'], 'userSecret': creds['user_secret']}
    out = []
    for acc in _snap('GET', '/accounts', query=q) or []:
        total = ((acc.get('balance') or {}).get('total') or {})
        positions = []
        try:
            for p in _snap('GET', f"/accounts/{acc['id']}/positions", query=q) or []:
                sym = ((p.get('symbol') or {}).get('symbol') or {})
                cur = ((sym.get('currency') or {}).get('code')) or ((p.get('currency') or {}).get('code')) or 'CAD'
                qty, price = float(p.get('units') or 0), float(p.get('price') or 0)
                positions.append({'symbol': sym.get('symbol', ''), 'description': sym.get('description', ''),
                                  'quantity': qty, 'price': price, 'value_cad': round(_to_cad(qty * price, cur), 2), 'currency': cur})
        except BrokerError:
            pass
        out.append({
            'key': f"snap-{acc.get('id')}", 'name': acc.get('name') or acc.get('number') or 'Account',
            'institution': acc.get('institution_name') or '', 'type': (acc.get('meta') or {}).get('type', '') or '',
            'currency': total.get('currency') or 'CAD',
            'balance_cad': round(_to_cad(total.get('amount'), total.get('currency')), 2), 'positions': positions,
        })
    return creds, out


def snaptrade_delete_user(creds):
    try:
        _snap('DELETE', '/snapTrade/deleteUser', query={'userId': creds['user_id']})
    except BrokerError:
        pass


PROVIDERS = {
    'questrade': {'connect': questrade_connect, 'sync': questrade_sync, 'label': 'Questrade'},
    'ibkr': {'connect': ibkr_connect, 'sync': ibkr_sync, 'label': 'Interactive Brokers'},
    'kraken': {'connect': kraken_connect, 'sync': kraken_sync, 'label': 'Kraken'},
    'snaptrade': {'connect': None, 'sync': snaptrade_sync, 'label': 'SnapTrade'},
}
