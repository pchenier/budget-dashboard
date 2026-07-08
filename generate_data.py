#!/usr/bin/env python3
"""
generate_data.py — Pull Plaid + Wise + Phantom → return clean JSON data dict.
Used by app.py to feed real data into Vault UI.
"""

import os, sys, json, requests
from datetime import datetime, date
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ── Import generate.py functions ──────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
import generate as gen

CATEGORY_ICONS = {
    "Groceries":         "🛒",
    "Food/Dining":      "🍽️",
    "Gas":              "⛽",
    "Transport":        "🚇",
    "Shopping":         "🛍️",
    "Gym":              "💪",
    "Health":           "💊",
    "Phone/Internet":   "📱",
    "Entertainment":    "🎬",
    "Subscriptions":    "📺",
    "Housing":          "🏠",
    "Utilities":        "⚡",
    "Cash/Transfers":   "💸",
    "Investments":     "📈",
    "Income":           "💼",
    "Other":            "📂",
}

# Normalize French categories from generate.py to English
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

ACCOUNT_TYPE_LABELS = {
    "depository": "Chequing / Savings",
    "credit":     "Credit Card",
    "investment": "Investment",
    "loan":       "Loan",
    "other":      "Other",
}


def pull_all(config):
    """
    Pull all data from Plaid + Wise + Phantom.
    Returns a big dict ready to be JSON-serialized and injected into Vault.
    """
    # ── Apply config to generate module ───────────────────────────────────────
    gen.PLAID_CLIENT = config["plaid_client"]
    gen.PLAID_SECRET = config["plaid_secret"]
    _env = config.get("plaid_env", "production")
    gen.PLAID_BASE   = ("https://production.plaid.com" if _env == "production"
                        else "https://development.plaid.com" if _env == "development"
                        else "https://sandbox.plaid.com")
    # Support multiple Plaid items (banks)
    plaid_tokens = config.get("plaid_tokens", [])
    if not plaid_tokens and config.get("plaid_token"):
        plaid_tokens = [config["plaid_token"]]
    gen.WISE_TOKEN   = config.get("wise_token", "")
    gen.WISE_PROFILE = int(config.get("wise_profile", 0) or 0) if str(config.get("wise_profile", "")).isdigit() else 0
    gen.PHANTOM_ADDR = ""  # Legacy: no longer used; wallets handled below
    gen.USD_TO_CAD   = float(config.get("usd_to_cad", 1.38) or 1.38)
    gen.START_DATE   = config.get("start_date", "2025-01-01") or "2025-01-01"
    gen.END_DATE     = date.today().isoformat()

    USD_TO_CAD = gen.USD_TO_CAD
    status_cb = config.get("_status_cb") or (lambda msg: None)

    # ── Crypto wallets (multi-chain) ─────────────────────────────────────────
    wallets = config.get("wallets", [])
    # Backwards compat: if phantom_wallet exists and wallets empty, treat as solana
    if not wallets and config.get("phantom_wallet", "").strip():
        wallets = [{"chain": "solana", "address": config["phantom_wallet"].strip(), "label": "Phantom"}]
        config["wallets"] = wallets

    # ── Load previous Plaid accounts (fallback if Plaid fails) ──
    _prev_plaid_accounts = {}
    # First: try data_cache.json
    try:
        import json as _json
        from pathlib import Path as _Path
        _cache_file = _Path(__file__).parent / "data_cache.json"
        if _cache_file.exists():
            _cached = _json.loads(_cache_file.read_text())
            for a in _cached.get("accounts", []):
                if not a["id"].startswith("wise_") and not a["id"].startswith("sol_") and not a["id"].startswith("crypto_"):
                    _prev_plaid_accounts[a["id"]] = {
                        "name":    a["name"],
                        "current": a.get("balance", 0),
                        "type":    a.get("type", "depository").lower().replace("chequing / savings", "depository").replace("credit card", "credit"),
                        "subtype": a.get("subtype", ""),
                        "_from_cache": True,
                    }
    except Exception as _e:
        print(f"  Plaid: prev cache load failed: {_e}")
    # Second: use _prev_accounts from config (passed by app.py from previous state)
    _prev_from_state = config.get("_prev_accounts", [])
    for a in _prev_from_state:
        if not isinstance(a, dict):
            continue
        aid = a.get("id", "")
        if aid and not aid.startswith("wise_") and not aid.startswith("sol_") and not aid.startswith("crypto_"):
            _prev_plaid_accounts[aid] = {
                "name":    a.get("name", ""),
                "current": a.get("bal", a.get("balance", 0)),
                "type":    a.get("type", "depository").lower().replace("chequing / savings", "depository").replace("credit card", "credit"),
                "subtype": a.get("subtype", ""),
                "_from_cache": True,
            }

    # ── Parallel data fetching ────────────────────────────────────────────────
    # All network calls are independent (except Wise txns which need balance_ids).
    # Fetch Plaid balances, Plaid transactions, Wise balances, and crypto in parallel.
    status_cb("Loading data...")

    def _fetch_plaid_balances(token):
        """Thread-safe: pass token directly to plaid_post instead of mutating global."""
        if not gen.PLAID_CLIENT or not gen.PLAID_SECRET:
            print(f"  Plaid: skipped balances (no client/secret)")
            return {}
        try:
            data = gen.plaid_post("/accounts/balance/get", {"access_token": token})
            result = {}
            for acc in data.get("accounts", []):
                aid = acc["account_id"]
                result[aid] = {
                    "name":    acc["name"],
                    "current": acc["balances"].get("current") or 0,
                    "type":    acc["type"],
                    "subtype": acc.get("subtype", ""),
                }
            print(f"  Plaid: {len(result)} accounts (token {token[:8]}...)")
            return result
        except Exception as e:
            print(f"  Plaid token {token[:8]}...: error → {e}")
            return {}

    def _fetch_plaid_txns(token):
        """Thread-safe: pass token directly to plaid_post."""
        if not gen.PLAID_CLIENT or not gen.PLAID_SECRET:
            return []
        try:
            all_txns, offset = [], 0
            while True:
                data = gen.plaid_post("/transactions/get", {
                    "access_token": token,
                    "start_date":   gen.START_DATE,
                    "end_date":     gen.END_DATE,
                    "options": {"count": 500, "offset": offset},
                })
                batch = data["transactions"]
                all_txns.extend(batch)
                if len(all_txns) >= data["total_transactions"] or not batch:
                    break
                offset += len(batch)
            print(f"  Plaid: {len(all_txns)} transactions (token {token[:8]}...)")
            return all_txns
        except Exception as e:
            print(f"  Plaid token {token[:8]}... txns error → {e}")
            return []

    def _fetch_crypto_wallet(w):
        chain   = w.get("chain", "solana").lower()
        address = w.get("address", "").strip()
        label   = w.get("label", chain.title())
        if not address:
            return None
        try:
            if chain == "solana":
                status_cb(f"Crypto: {label} (SOL)...")
                r = requests.post("https://api.mainnet-beta.solana.com", json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getBalance",
                    "params": [address],
                }, timeout=10)
                r.raise_for_status()
                lamports = r.json().get("result", {}).get("value", 0)
                native_bal = lamports / 1e9
                p = requests.get(
                    "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
                    timeout=10,
                )
                price = p.json().get("solana", {}).get("usd", 0) if p.ok else 0
                return ("solana", address, label, native_bal, "SOL", native_bal * price)

            elif chain == "ethereum":
                status_cb(f"Crypto: {label} (ETH)...")
                r = requests.get(
                    f"https://api.etherscan.io/api?module=account&action=balance&address={address}&tag=latest",
                    timeout=10,
                )
                r.raise_for_status()
                wei = int(r.json().get("result", "0") or "0")
                native_bal = wei / 1e18
                p = requests.get(
                    "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd",
                    timeout=10,
                )
                price = p.json().get("ethereum", {}).get("usd", 0) if p.ok else 0
                return ("ethereum", address, label, native_bal, "ETH", native_bal * price)

            elif chain == "bitcoin":
                status_cb(f"Crypto: {label} (BTC)...")
                r = requests.get(
                    f"https://blockchain.info/balance?active={address}",
                    timeout=10,
                )
                r.raise_for_status()
                satoshi = r.json().get(address, {}).get("final_balance", 0)
                native_bal = satoshi / 1e8
                p = requests.get(
                    "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
                    timeout=10,
                )
                price = p.json().get("bitcoin", {}).get("usd", 0) if p.ok else 0
                return ("bitcoin", address, label, native_bal, "BTC", native_bal * price)
        except Exception as e:
            print(f"  Crypto {label} ({chain}): error → {e}")
        return None

    # Launch all independent network calls in parallel
    with ThreadPoolExecutor(max_workers=6) as pool:
        # Submit Plaid balance tasks
        plaid_bal_futures = {pool.submit(_fetch_plaid_balances, t): t for t in plaid_tokens}
        # Submit Plaid transaction tasks
        plaid_txn_futures = {pool.submit(_fetch_plaid_txns, t): t for t in plaid_tokens}
        # Submit Wise balance task
        wise_bal_future = pool.submit(gen.get_wise_balances)
        # Submit crypto wallet tasks
        crypto_futures = {pool.submit(_fetch_crypto_wallet, w): w for w in wallets if w.get("address", "").strip()}

    # Collect Plaid balances
    status_cb("Plaid: balances...")
    balances = {}
    for future in as_completed(plaid_bal_futures):
        try:
            result = future.result()
            if result:
                balances.update(result)
                print(f"  Plaid balances: merged {len(result)} accounts")
        except Exception as e:
            print(f"  Plaid balances error: {e}")

    # If Plaid failed, fall back to previous accounts
    if not balances and _prev_plaid_accounts:
        print("  Plaid: using cached accounts from previous sync")
        balances = _prev_plaid_accounts

    # Collect Wise balances (need balance_ids for Wise transactions)
    status_cb("Wise: balances...")
    try:
        wise_bal, wise_balance_ids = wise_bal_future.result()
    except Exception as e:
        print(f"  Wise: error → {e}")
        wise_bal, wise_balance_ids = {}, {}

    # Fetch Wise transactions (depends on balance_ids from above)
    status_cb("Wise: transactions...")
    try:
        wise_txns = gen.get_wise_transactions(wise_balance_ids)
    except Exception as e:
        print(f"  Wise: txns error → {e}")
        wise_txns = []

    # Collect Plaid transactions
    status_cb("Plaid: transactions...")
    raw_txns = []
    for future in as_completed(plaid_txn_futures):
        try:
            result = future.result()
            if result:
                raw_txns.extend(result)
                print(f"  Plaid txns: got {len(result)} transactions")
        except Exception as e:
            print(f"  Plaid txns error: {e}")

    # Collect crypto balances
    crypto_balances = []
    for future in as_completed(crypto_futures):
        try:
            result = future.result()
            if result:
                crypto_balances.append(result)
                print(f"  Crypto: got {result[2]} balance")
        except Exception as e:
            print(f"  Crypto error: {e}")

    status_cb("Processing transactions...")
    txns = gen.process_transactions(raw_txns, wise_txns=wise_txns)

    # ─────────────────────────────────────────────────────────────────────────
    # Build accounts list
    # ─────────────────────────────────────────────────────────────────────────
    print(f"  Building accounts from {len(balances)} Plaid balances + {len(wise_bal or {})} Wise + {len(crypto_balances)} crypto")
    accounts = []
    for aid, acc in balances.items():
        bal = acc["current"]
        # credit cards: Plaid reports available as positive, owe = negative UX
        if acc["type"] == "credit":
            bal = -abs(bal)
        is_cached = acc.get("_from_cache", False)
        accounts.append({
            "id":      aid,
            "name":    acc["name"],
            "inst":    acc.get("institution", acc["name"]),
            "balance": round(bal, 2),
            "type":    ACCOUNT_TYPE_LABELS.get(acc["type"], acc["type"]),
            "subtype": acc.get("subtype", ""),
            "sync":    "cached" if is_cached else "just now",
        })

    # Add Wise accounts
    for currency, info in (wise_bal or {}).items():
        if isinstance(info, dict):
            amt = info.get("amount", 0) or 0
        else:
            amt = float(info or 0)
        # Convert USD to CAD if needed
        if currency == "USD":
            amt_cad = round(amt * USD_TO_CAD, 2)
        else:
            amt_cad = round(amt, 2)
        accounts.append({
            "id":      f"wise_{currency}",
            "name":    f"Wise {currency}",
            "inst":    "Wise",
            "balance": amt_cad,
            "type":    "International",
            "subtype": currency,
            "sync":    "just now",
        })

    # Add crypto wallet accounts
    for i, (chain, address, label, native_bal, native_sym, usd_val) in enumerate(crypto_balances):
        if native_bal and native_bal > 0:
            accounts.append({
                "id":      f"crypto_{chain}_{i}",
                "name":    label,
                "inst":    chain.title(),
                "balance": round(usd_val * USD_TO_CAD, 2),
                "type":    "Crypto",
                "subtype": f"{native_bal:.6f} {native_sym}",
                "sync":    "just now",
            })

    # ─────────────────────────────────────────────────────────────────────────
    # Net worth
    # ─────────────────────────────────────────────────────────────────────────
    total_assets = sum(a["balance"] for a in accounts if a["balance"] > 0)
    total_debt   = sum(a["balance"] for a in accounts if a["balance"] < 0)
    net_worth    = total_assets + total_debt  # debt is negative

    # ─────────────────────────────────────────────────────────────────────────
    # Transactions — serialize dates to strings
    # ─────────────────────────────────────────────────────────────────────────
    now = datetime.now()
    current_month = now.month
    current_year  = now.year

    serialized_txns = []
    for t in txns:
        dt = t["date"] if isinstance(t["date"], datetime) else datetime.strptime(t["date"], "%Y-%m-%d")
        cat = CATEGORY_ALIASES.get(t["category"], t["category"])
        serialized_txns.append({
            "date":     dt.strftime("%b %d"),
            "date_iso": dt.strftime("%Y-%m-%d"),
            "name":     t["name"],
            "amount":   round(t["amount"], 2),
            "category": cat,
            "account":  t["account"],
            "id":       t.get("id", ""),
            "ico":      CATEGORY_ICONS.get(cat, "📂"),
        })

    # ─────────────────────────────────────────────────────────────────────────
    # Monthly income / spending (current month)
    # ─────────────────────────────────────────────────────────────────────────
    monthly_income   = 0.0
    monthly_spending = 0.0
    category_spending = defaultdict(float)

    for t in txns:
        dt = t["date"] if isinstance(t["date"], datetime) else datetime.strptime(t["date"], "%Y-%m-%d")
        cat = CATEGORY_ALIASES.get(t["category"], t["category"])
        if dt.month == current_month and dt.year == current_year:
            amt = t["amount"]
            if amt < 0:  # spending (Plaid: positive = debit, but we flip in process_transactions... check)
                monthly_spending += abs(amt)
                category_spending[cat] += abs(amt)
            else:
                monthly_income += abs(amt)

    # fallback: if process_transactions keeps Plaid convention (positive = debit from account)
    if monthly_income < 1 and monthly_spending < 1:
        for t in txns:
            dt = t["date"] if isinstance(t["date"], datetime) else datetime.strptime(t["date"], "%Y-%m-%d")
            cat = CATEGORY_ALIASES.get(t["category"], t["category"])
            if dt.month == current_month and dt.year == current_year:
                amt = t["amount"]
                if amt > 0:  # Plaid: positive = money out
                    monthly_spending += amt
                    category_spending[cat] += amt
                else:
                    monthly_income += abs(amt)

    cash_flow = monthly_income - monthly_spending

    # ─────────────────────────────────────────────────────────────────────────
    # Budget — auto-build from spending categories
    # ─────────────────────────────────────────────────────────────────────────
    BUDGET_LIMITS = {
        "Groceries":        500,
        "Food/Dining":      300,
        "Transport":       200,
        "Gas":             150,
        "Shopping":        300,
        "Subscriptions":   100,
        "Gym":             100,
        "Health":          100,
        "Phone/Internet":  80,
        "Entertainment":   150,
        "Utilities":       130,
        "Housing":         1500,
        "Other":           200,
    }

    budget = []
    for cat, limit in BUDGET_LIMITS.items():
        spent = round(category_spending.get(cat, 0), 2)
        budget.append({
            "cat":   cat,
            "spent": spent,
            "limit": limit,
            "ico":   CATEGORY_ICONS.get(cat, "📂"),
        })
    # Sort by spent desc
    budget.sort(key=lambda x: x["spent"], reverse=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Spending by category (for donut chart)
    # ─────────────────────────────────────────────────────────────────────────
    top_cats = sorted(category_spending.items(), key=lambda x: x[1], reverse=True)[:8]

    # ─────────────────────────────────────────────────────────────────────────
    # Assemble final payload
    # ─────────────────────────────────────────────────────────────────────────
    return {
        # Summary metrics
        "netWorth":       round(net_worth, 2),
        "totalAssets":    round(total_assets, 2),
        "totalDebt":      round(abs(total_debt), 2),
        "monthlyIncome":  round(monthly_income, 2),
        "monthlySpending":round(monthly_spending, 2),
        "cashFlow":       round(cash_flow, 2),

        # Accounts
        "accounts": accounts,

        # Transactions (most recent first, all of them)
        "transactions": serialized_txns,

        # Budget
        "budget": budget,

        # Spending breakdown for chart
        "categorySpending": [{"cat": c, "amt": round(a, 2)} for c, a in top_cats],

        # Raw for potential future use
        "_generated": datetime.now().strftime("%B %d, %Y"),
        "_start_date": gen.START_DATE,
        "_end_date":   gen.END_DATE,
    }
