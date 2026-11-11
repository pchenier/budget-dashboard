"""
Exchange rates to CAD for accounts held in other currencies (e.g. a Wise USD balance).

Source: Bank of Canada daily rates (official, free, no key), with the ECB rates from
frankfurter.app as a fallback. Cached in memory for 6 hours; if every source fails,
USD falls back to the user's configured rate and other currencies are left unconverted
(reported as such) rather than guessed.
"""

import threading
import time

import requests

_CACHE = {'at': 0.0, 'rates': {}}
_LOCK = threading.Lock()
_TTL = 6 * 3600

# Currencies people can pick by hand in Accounts (detection covers any Plaid sends)
COMMON = ['CAD', 'USD', 'EUR', 'GBP', 'AUD', 'CHF', 'JPY', 'MXN', 'HKD', 'SGD', 'NZD', 'CNY', 'INR', 'BRL']


def _bank_of_canada():
    r = requests.get('https://www.bankofcanada.ca/valet/observations/group/FX_RATES_DAILY/json',
                     params={'recent': 10}, timeout=10)
    r.raise_for_status()
    rates = {}
    for obs in r.json().get('observations', []):  # oldest first: later days overwrite
        for key, val in obs.items():
            if key.startswith('FX') and key.endswith('CAD') and isinstance(val, dict) and val.get('v'):
                rates[key[2:5]] = float(val['v'])
    return rates


def _frankfurter():
    r = requests.get('https://api.frankfurter.app/latest', params={'from': 'CAD'}, timeout=10)
    r.raise_for_status()
    return {cur: 1 / float(v) for cur, v in (r.json().get('rates') or {}).items() if v}


def rates_to_cad():
    """{currency: CAD per 1 unit}, always including CAD: 1."""
    with _LOCK:
        if _CACHE['rates'] and time.time() - _CACHE['at'] < _TTL:
            return dict(_CACHE['rates'])
    rates = {}
    for source in (_bank_of_canada, _frankfurter):
        try:
            rates = source()
            if rates.get('USD'):
                break
        except Exception as e:
            print(f"  FX {source.__name__}: error → {e}")
    rates['CAD'] = 1.0
    if len(rates) > 1:
        with _LOCK:
            _CACHE.update(at=time.time(), rates=dict(rates))
    return rates


def to_cad(amount, currency, rates, usd_fallback=1.38):
    """(CAD amount, converted?) for an amount in `currency`."""
    cur = (currency or 'CAD').upper()
    if cur == 'CAD':
        return amount, True
    rate = rates.get(cur) or (usd_fallback if cur == 'USD' else None)
    if rate is None:
        return amount, False
    return amount * rate, True
