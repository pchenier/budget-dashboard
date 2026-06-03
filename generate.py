#!/usr/bin/env python3
"""
Budget Local — Dashboard Generator
Pulls Plaid + Wise + Phantom (Solana) → génère un dashboard HTML.

Usage:
    python generate.py           # génère + ouvre le browser
    python generate.py --no-open # génère seulement
"""

import json, os, sys, webbrowser, requests, csv as csv_module
from datetime import datetime, date
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

# ── Config depuis .env ────────────────────────────────────────────────────────
PLAID_CLIENT  = os.getenv("PLAID_CLIENT_ID", "")
PLAID_SECRET  = os.getenv("PLAID_SECRET", "")
PLAID_TOKEN   = os.getenv("PLAID_ACCESS_TOKEN", "")
PLAID_ENV     = os.getenv("PLAID_ENV", "production")
PLAID_BASE    = "https://production.plaid.com" if PLAID_ENV == "production" else "https://sandbox.plaid.com"

WISE_TOKEN    = os.getenv("WISE_TOKEN", "")
WISE_PROFILE  = int(os.getenv("WISE_PROFILE_ID", "0"))

PHANTOM_ADDR  = os.getenv("PHANTOM_WALLET", "")

QT_REFRESH    = os.getenv("QUESTRADE_REFRESH_TOKEN", "")

USD_TO_CAD    = float(os.getenv("USD_TO_CAD", "1.38"))
START_DATE    = os.getenv("START_DATE", "2025-01-01")
END_DATE      = date.today().isoformat()
OUTPUT_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
QT_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "questrade_cache.json")
ENV_PATH      = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
IMPORTS_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "imports")


# ── Validation ────────────────────────────────────────────────────────────────
def check_config():
    warnings = []
    if not PLAID_CLIENT or PLAID_CLIENT == "xxxxxxxxxxxxxxxxxxxxxxxx":
        warnings.append("PLAID_CLIENT_ID manquant dans .env")
    if not PLAID_SECRET or PLAID_SECRET == "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx":
        warnings.append("PLAID_SECRET manquant dans .env")
    if not PLAID_TOKEN or "xxxxxxxx" in PLAID_TOKEN:
        warnings.append("PLAID_ACCESS_TOKEN manquant — lance setup_plaid.py d'abord")
    if warnings:
        print("\n⚠️  Config Plaid incomplète (mode CSV seulement):")
        for w in warnings:
            print(f"   • {w}")
        print("\n→ Pour activer Plaid: copie .env.example → .env et remplis les valeurs")
        print("→ Mode CSV disponible: importe tes relevés via l'interface web\n")


# ── Plaid ─────────────────────────────────────────────────────────────────────
def plaid_post(endpoint, payload):
    r = requests.post(
        PLAID_BASE + endpoint,
        headers={"Content-Type": "application/json"},
        json={**payload, "client_id": PLAID_CLIENT, "secret": PLAID_SECRET},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def get_plaid_balances():
    if not PLAID_CLIENT or not PLAID_SECRET or not PLAID_TOKEN:
        print("  Plaid: skipped (pas configuré)")
        return {}
    print("  Plaid: balances...")
    try:
        data = plaid_post("/accounts/balance/get", {"access_token": PLAID_TOKEN})
        balances = {}
        for acc in data["accounts"]:
            aid = acc["account_id"]
            balances[aid] = {
                "name":    acc["name"],
                "current": acc["balances"]["current"] or 0,
                "type":    acc["type"],
                "subtype": acc["subtype"],
            }
        return balances
    except Exception as e:
        print(f"  Plaid: erreur balances → {e}")
        return {}


def get_plaid_transactions():
    if not PLAID_CLIENT or not PLAID_SECRET or not PLAID_TOKEN:
        print("  Plaid: skipped (pas configuré)")
        return []
    print("  Plaid: transactions...")
    try:
        all_txns, offset = [], 0
        while True:
            data = plaid_post("/transactions/get", {
                "access_token": PLAID_TOKEN,
                "start_date":   START_DATE,
                "end_date":     END_DATE,
                "options": {"count": 500, "offset": offset},
            })
            batch = data["transactions"]
            all_txns.extend(batch)
            if len(all_txns) >= data["total_transactions"] or not batch:
                break
            offset += len(batch)
        print(f"  Plaid: {len(all_txns)} transactions")
        return all_txns
    except Exception as e:
        print(f"  Plaid: erreur transactions → {e} (skipped)")
        return []


# ── Wise ──────────────────────────────────────────────────────────────────────
def get_wise_balances():
    if not WISE_TOKEN or not WISE_PROFILE:
        print("  Wise: skipped (pas configuré)")
        return {}, {}
    print("  Wise: balances...")
    try:
        r = requests.get(
            f"https://api.wise.com/v4/profiles/{WISE_PROFILE}/balances?types=STANDARD",
            headers={"Authorization": f"Bearer {WISE_TOKEN}"},
            timeout=15,
        )
        r.raise_for_status()
        result = {}
        balance_ids = {}   # currency → balance id (for statement fetch)
        for b in r.json():
            cur = b["totalWorth"]["currency"]
            amt = b["totalWorth"]["value"]
            result[cur] = amt
            balance_ids[cur] = b.get("id")
        return result, balance_ids
    except Exception as e:
        print(f"  Wise: erreur → {e}")
        return {}, {}


def get_wise_transactions(balance_ids):
    """Fetch transactions from Wise statement API for each balance."""
    if not WISE_TOKEN or not WISE_PROFILE or not balance_ids:
        return []
    print("  Wise: transactions...")
    all_txns = []
    headers = {"Authorization": f"Bearer {WISE_TOKEN}"}
    interval_start = START_DATE + "T00:00:00.000Z"
    interval_end   = END_DATE   + "T23:59:59.999Z"
    for currency, bid in balance_ids.items():
        if not bid:
            continue
        try:
            url = (
                f"https://api.wise.com/v1/profiles/{WISE_PROFILE}"
                f"/balance-statements/{bid}/statement.json"
                f"?intervalStart={interval_start}&intervalEnd={interval_end}"
            )
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            data = r.json()
            txns = data.get("transactions", [])
            for t in txns:
                raw_amt  = t.get("amount", {}).get("value", 0)
                cur_t    = t.get("amount", {}).get("currency", currency)
                date_str = t.get("date", "")[:10]   # "2024-01-15T..."
                t_type   = t.get("type", "")         # "DEBIT" or "CREDIT"
                details  = t.get("details", {})
                desc = (
                    details.get("merchant", {}).get("name", "")
                    or details.get("description", "")
                    or details.get("senderName", "")
                    or "Wise"
                )
                if not date_str:
                    continue
                # Convert to CAD if USD
                amt_cad = abs(raw_amt) * USD_TO_CAD if cur_t == "USD" else abs(raw_amt)
                # Plaid convention: positive = spending (debit), negative = income (credit)
                plaid_amt = amt_cad if t_type == "DEBIT" else -amt_cad
                ref = t.get("referenceNumber", "") or f"wise_{currency}_{date_str}_{raw_amt}"
                all_txns.append({
                    "date":     date_str,
                    "amount":   round(plaid_amt, 2),
                    "name":     desc.strip() or "Wise",
                    "category": None,
                    "account":  f"wise_{currency}",
                    "id":       ref,
                })
        except Exception as e:
            print(f"  Wise {currency}: erreur → {e}")
    print(f"  Wise: {len(all_txns)} transactions")
    return all_txns


# ── Phantom / Solana ──────────────────────────────────────────────────────────
def get_phantom_balance():
    if not PHANTOM_ADDR or PHANTOM_ADDR == "YourSolanaPublicKeyHere":
        print("  Phantom: skipped (pas configuré)")
        return 0.0, 0.0
    print("  Phantom: balance SOL...")
    try:
        r = requests.post("https://api.mainnet-beta.solana.com", json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getBalance",
            "params": [PHANTOM_ADDR],
        }, timeout=10)
        r.raise_for_status()
        lamports = r.json().get("result", {}).get("value", 0)
        sol = lamports / 1e9
        p = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
            timeout=10,
        )
        sol_usd = p.json().get("solana", {}).get("usd", 0) if p.ok else 0
        print(f"  Phantom: {sol:.4f} SOL @ ${sol_usd} USD")
        return sol, sol_usd
    except Exception as e:
        print(f"  Phantom: erreur → {e}")
        return 0.0, 0.0


# ── Questrade ─────────────────────────────────────────────────────────────────
def _qt_update_env_token(new_token):
    """Remplace QUESTRADE_REFRESH_TOKEN dans .env avec le nouveau token rotatif."""
    try:
        if not os.path.exists(ENV_PATH):
            return
        with open(ENV_PATH, "r") as f:
            lines = f.readlines()
        updated = []
        found = False
        for line in lines:
            if line.startswith("QUESTRADE_REFRESH_TOKEN="):
                updated.append(f"QUESTRADE_REFRESH_TOKEN={new_token}\n")
                found = True
            else:
                updated.append(line)
        if not found:
            updated.append(f"QUESTRADE_REFRESH_TOKEN={new_token}\n")
        with open(ENV_PATH, "w") as f:
            f.writelines(updated)
        # Aussi update la variable globale
        global QT_REFRESH
        QT_REFRESH = new_token
        print(f"  Questrade: nouveau refresh token sauvegardé dans .env")
    except Exception as e:
        print(f"  Questrade: impossible de sauvegarder le token → {e}")


def get_questrade_data():
    """
    Fetch comptes + positions + balances depuis Questrade.
    Sauvegarde un cache JSON local.
    Si le token est invalide/absent, retourne le cache existant (données stale).
    """
    cache = {}
    if os.path.exists(QT_CACHE_PATH):
        try:
            with open(QT_CACHE_PATH, "r") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    if not QT_REFRESH:
        if cache:
            print("  Questrade: pas de token — données du cache utilisées")
        else:
            print("  Questrade: skipped (QUESTRADE_REFRESH_TOKEN manquant)")
        return cache

    print("  Questrade: authentification...")
    try:
        r = requests.post(
            "https://login.questrade.com/oauth2/token",
            params={"grant_type": "refresh_token", "refresh_token": QT_REFRESH},
            timeout=15,
        )
        if not r.ok:
            print(f"  Questrade: token invalide ({r.status_code}) — données du cache utilisées")
            return cache
        auth = r.json()
        access_token = auth["access_token"]
        new_refresh  = auth["refresh_token"]
        api_server   = auth["api_server"]
        _qt_update_env_token(new_refresh)
        headers = {"Authorization": f"Bearer {access_token}"}

        print("  Questrade: comptes...")
        r_acc = requests.get(api_server + "v1/accounts", headers=headers, timeout=15)
        r_acc.raise_for_status()
        accounts = r_acc.json().get("accounts", [])

        all_positions = []
        all_balances  = []
        for acc in accounts:
            acc_id  = acc["number"]
            acc_type = acc.get("type", "")
            acc_name = f"{acc_type} ({acc_id})"

            # Positions
            rp = requests.get(api_server + f"v1/accounts/{acc_id}/positions", headers=headers, timeout=15)
            if rp.ok:
                for pos in rp.json().get("positions", []):
                    pos["_account"] = acc_name
                    all_positions.append(pos)

            # Balances
            rb = requests.get(api_server + f"v1/accounts/{acc_id}/balances", headers=headers, timeout=15)
            if rb.ok:
                bal_data = rb.json()
                for b in bal_data.get("combinedBalances", []):
                    b["_account"] = acc_name
                    all_balances.append(b)

        fetched_at = datetime.now().isoformat()
        data = {
            "fetched_at":  fetched_at,
            "accounts":    accounts,
            "positions":   all_positions,
            "balances":    all_balances,
        }
        with open(QT_CACHE_PATH, "w") as f:
            json.dump(data, f, indent=2)
        print(f"  Questrade: {len(all_positions)} positions, {len(accounts)} comptes — cache mis à jour")
        return data

    except Exception as e:
        print(f"  Questrade: erreur → {e}")
        if cache:
            print("  Questrade: données du cache utilisées comme fallback")
        return cache


# ── CSV Import ────────────────────────────────────────────────────────────────
def _parse_date(s):
    """Try several date formats, return YYYY-MM-DD or None."""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d",
                "%m-%d-%Y", "%d-%m-%Y", "%b %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_amount(s):
    """Parse a dollar string like '$1,234.56' or '-500.00' → float."""
    s = s.strip().replace("$", "").replace(",", "").replace(" ", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def get_csv_transactions():
    """Read all .csv files from imports/ and return list of normalized txn dicts."""
    if not os.path.isdir(IMPORTS_DIR):
        return []
    all_txns = []
    csv_files = sorted(f for f in os.listdir(IMPORTS_DIR) if f.lower().endswith(".csv"))
    for fname in csv_files:
        fpath = os.path.join(IMPORTS_DIR, fname)
        account_id = os.path.splitext(fname)[0]
        try:
            with open(fpath, encoding="utf-8-sig", errors="replace") as fh:
                reader = csv_module.DictReader(fh)
                headers = [h.strip() for h in (reader.fieldnames or [])]
                headers_lower = [h.lower() for h in headers]

                # Detect bank format
                def _h(*names):
                    """Return actual header matching one of the given names (case-insensitive)."""
                    for n in names:
                        for h, hl in zip(headers, headers_lower):
                            if hl == n.lower():
                                return h
                    return None

                # RBC: Transaction Date, Description 1, Description 2, CAD$, USD$
                # Scotia: Date, Description, Amount
                # TD: Date, Description, Debit, Credit
                # Desjardins: Date, Description, Débit, Crédit
                date_col = _h("Transaction Date", "Date")
                name_col = _h("Description 1", "Description", "Description 1")
                name_col2 = _h("Description 2")  # RBC secondary desc
                amount_col = _h("Amount", "CAD$")
                debit_col  = _h("Debit", "Débit")
                credit_col = _h("Credit", "Crédit")

                if not date_col:
                    # Generic fallback: find first date-like and first amount-like column
                    for h in headers:
                        if "date" in h.lower():
                            date_col = h
                            break
                    for h in headers:
                        if any(k in h.lower() for k in ["amount", "montant", "debit", "credit", "débit", "crédit"]):
                            amount_col = amount_col or h
                            break

                if not date_col:
                    print(f"  CSV {fname}: aucune colonne date trouvée — skipped")
                    continue

                row_num = 0
                for row in reader:
                    row_num += 1
                    date_raw = row.get(date_col, "").strip()
                    date_str = _parse_date(date_raw)
                    if not date_str:
                        continue
                    # Filter by date range
                    if date_str < START_DATE or date_str > END_DATE:
                        continue

                    # Name
                    name = ""
                    if name_col:
                        name = row.get(name_col, "").strip()
                    if not name and name_col2:
                        name = row.get(name_col2, "").strip()
                    if not name:
                        name = "CSV Import"

                    # Amount (Plaid convention: positive = spending, negative = income)
                    amount = None
                    if debit_col and credit_col:
                        # TD / Desjardins style: separate debit/credit columns
                        dval = _parse_amount(row.get(debit_col, ""))
                        cval = _parse_amount(row.get(credit_col, ""))
                        if dval is not None and dval != 0:
                            amount = abs(dval)    # spending = positive
                        elif cval is not None and cval != 0:
                            amount = -abs(cval)   # income = negative
                        else:
                            amount = 0.0
                    elif amount_col:
                        raw = _parse_amount(row.get(amount_col, ""))
                        if raw is None:
                            # Try USD$ for RBC
                            usd_col = _h("USD$")
                            if usd_col:
                                raw = _parse_amount(row.get(usd_col, ""))
                                if raw is not None:
                                    raw = raw * USD_TO_CAD
                        if raw is None:
                            continue
                        # RBC / Scotia: negative = spending in their convention
                        # They use negative for debits, positive for credits
                        amount = -raw  # flip to Plaid convention
                    else:
                        continue

                    all_txns.append({
                        "date":     date_str,
                        "name":     name,
                        "amount":   round(amount, 2),
                        "category": None,
                        "account":  account_id,
                        "id":       f"csv_{account_id}_{row_num}",
                    })
            print(f"  CSV {fname}: {len([t for t in all_txns if t['account'] == account_id])} transactions")
        except Exception as e:
            print(f"  CSV {fname}: erreur → {e}")
    return all_txns


# ── Catégories ────────────────────────────────────────────────────────────────
CATEGORY_RULES = [
    (["metro", "iga", "maxi", "provigo", "superc", "loblaws", "marché elite",
      "marche elite", "épicerie", "epicerie", "grocery", "boucherie",
      "fromagerie", "pc express", "costco", "super c", "rachelle", "naturalia"],
     "Épicerie"),
    (["restaurant", "resto", "mcdonalds", "tim horton", "subway", "pizza",
      "sushi", "burger", "café", "cafe", "coffee", "doordash", "uber eat",
      "ubereats", "uber", "skip", "wendy", "a&w", "popeye", "st-hubert",
      "scores", "cora", "boulangerie", "patisserie", "pâtisserie",
      "thai", "ramen", "pho", "dim sum", "bistro", "bar ", "bouffe", "food"],
     "Bouffe/Resto"),
    (["esso", "shell", "petro", "ultramar", "couche-tard", "gas",
      "essence", "fuel", "station-service"],
     "Gaz"),
    (["amazon", "ebay", "etsy", "walmart", "ikea", "zara", "h&m", "uniqlo",
      "best buy", "bestbuy", "winners", "marshalls", "simons", "indigo",
      "chapters", "archambault", "sport expert", "decathlon", "atmosphère",
      "canadian tire", "home depot", "rona", "réno-dépôt", "reno-depot",
      "pharmaprix", "jean coutu", "shoppers", "dollarama", "aliexpress",
      "shein", "temu", "revolve", "ssense", "asos", "nordstrom", "la baie",
      "bay", "gap", "old navy", "american eagle", "abercrombie"],
     "Shopping"),
    (["wealthsimple", "disnat", "questrade", "fidelity", "rbc direct",
      "td direct", "nbdb", "bmo investorline", "desjardins courtage",
      "invest", "placement", "bourse", "etf", "mutual fund",
      "crypto", "coinbase", "binance", "kraken", "shakepay", "ndax"],
     "Investissements"),
    (["intact assurance", "belairdirect", "td assurance", "sonnet",
      "desjardins assur", "intact auto", "saaq", "raq",
      "garage", "mécanique", "mecanique", "carwash", "lavage auto",
      "canadian tire auto", "midas", "jiffy", "oil change",
      "stationnement", "parking", "volkswagen", "bmw", "mercedes", "audi"],
     "Auto"),
    (["moto", "motocycle", "motorcycle", "revzilla", "motosport", "motovan",
      "casque", "helmet", "veste moto", "gear moto", "aprilia", "tuono"],
     "Moto"),
    (["openai", "anthropic", "github", "digitalocean", "aws",
      "amazon web", "google cloud", "azure", "heroku", "vercel",
      "netlify", "cloudflare", "namecheap", "godaddy", "shopify",
      "slack", "notion", "figma", "adobe", "microsoft 365",
      "office 365", "dropbox", "zapier", "airtable", "hubspot",
      "zoom", "loom", "grammarly", "1password", "twilio",
      "sendgrid", "stripe", "square", "computer", "laptop",
      "keyboard", "monitor", "webcam", "tech", "logiciel", "software"],
     "Business/Tech"),
    (["gym", "anytime fitness", "ymca", "écofit", "ecofit",
      "nautilus plus", "énergie cardio", "energie cardio",
      "yoga", "crossfit", "orange theory", "goodlife", "la fitness",
      "equinox", "sportif", "fitness", "muscle"],
     "Gym"),
    (["netflix", "spotify", "apple music", "youtube premium",
      "disney+", "disney plus", "crave", "paramount", "hbo",
      "prime video", "amazon prime", "dazn", "tidal",
      "cinema", "cinéma", "concert", "eventbrite", "ticketmaster",
      "jeux", "game", "playstation", "xbox", "steam", "nintendo",
      "twitch", "patreon", "loto", "casino", "nightclub", "livre", "book"],
     "Divertissement"),
    (["pharmaprix", "jean coutu", "shoppers drug", "clinique", "clinic",
      "médecin", "medecin", "dentiste", "dentist", "optique",
      "hospital", "hôpital", "pharmacie", "pharmacy",
      "médicament", "medicament", "santé", "sante", "physio", "psycho"],
     "Santé"),
    (["videotron", "vidéotron", "bell", "rogers", "telus", "fido",
      "koodo", "public mobile", "virgin mobile", "chatr", "lucky mobile",
      "fizz", "freedom", "internet", "cellulaire", "forfait"],
     "Télécom"),
    (["loyer", "rent", "hypothèque", "hypotheque", "mortgage", "condo"],
     "Logement"),
    (["atm", "withdraw", "retrait", "cash", "wire", "virement",
      "transfer", "interac", "e-transfer", "wise", "remittance",
      "western union", "moneygram"],
     "Cash/Virements"),
]


def categorize(name: str, plaid_cats: list) -> str:
    nl = name.lower()
    for keywords, cat in CATEGORY_RULES:
        if any(kw in nl for kw in keywords):
            return cat
    if plaid_cats:
        pc = " ".join(plaid_cats).lower()
        if "food" in pc or "restaurant" in pc or "grocery" in pc: return "Bouffe/Resto"
        if "travel" in pc or "gas" in pc:                         return "Gaz"
        if "shop" in pc or "retail" in pc:                        return "Shopping"
        if "gym" in pc or "sport" in pc:                          return "Gym"
        if "entertain" in pc or "recreation" in pc:               return "Divertissement"
        if "health" in pc or "medical" in pc or "pharmacy" in pc: return "Santé"
        if "telecom" in pc or "phone" in pc:                      return "Télécom"
        if "transfer" in pc or "payment" in pc:                   return "Cash/Virements"
    return "Autre"


def is_skip(txn) -> bool:
    name = txn.get("name", "").lower()
    cats = txn.get("category") or []
    cats_str = " ".join(cats).lower()
    if "paiement du" in name: return True
    if "mb-credit card" in name or "mb-loc pay" in name: return True
    if "credit card pay" in cats_str: return True
    if "wise" in name and any(kw in name for kw in ["transfer", "virement", "payment", "paiement"]): return True
    if ("correction" in name or "reversal" in name) and "uber" in name: return True
    return False


def process_transactions(raw_txns, wise_txns=None, csv_txns=None):
    cleaned = []
    for t in raw_txns:
        if is_skip(t):
            continue
        amt  = t["amount"]
        name = t.get("merchant_name") or t.get("name") or "Unknown"
        cat  = categorize(name, t.get("category") or [])
        dt   = datetime.strptime(t["date"], "%Y-%m-%d")
        cleaned.append({
            "date":    dt,
            "name":    name,
            "amount":  amt,
            "category": cat,
            "account": t["account_id"],
            "id":      t.get("transaction_id", ""),
        })
    # Ajouter les transactions Wise (déjà pré-formatées)
    for t in (wise_txns or []):
        cat = t.get("category") or categorize(t["name"], [])
        dt  = datetime.strptime(t["date"], "%Y-%m-%d")
        cleaned.append({
            "date":    dt,
            "name":    t["name"],
            "amount":  t["amount"],
            "category": cat,
            "account": t["account"],
            "id":      t["id"],
        })
    # Ajouter les transactions CSV
    for t in (csv_txns or []):
        cat = t.get("category") or categorize(t["name"], [])
        dt  = datetime.strptime(t["date"], "%Y-%m-%d")
        cleaned.append({
            "date":    dt,
            "name":    t["name"],
            "amount":  t["amount"],
            "category": cat,
            "account": t["account"],
            "id":      t["id"],
        })
    cleaned.sort(key=lambda x: x["date"], reverse=True)
    return cleaned


def fmt_cad(n):
    sign = "-" if n < 0 else ""
    return f"{sign}${abs(n):,.2f}"


# ── HTML ──────────────────────────────────────────────────────────────────────
DONUT_COLORS = [
    "#4f86f7","#f97316","#ef4444","#22c55e","#a855f7",
    "#06b6d4","#ec4899","#84cc16","#f59e0b","#3b82f6",
    "#10b981","#8b5cf6","#f43f5e","#14b8a6","#eab308",
    "#6366f1","#fb923c","#34d399","#e879f9","#38bdf8",
]

def build_html(balances, wise_bal, sol_balance, sol_usd, txns, qt_data=None):
    # ── Net worth ────────────────────────────────────────────────────────────
    checking_total = sum(a["current"] for a in balances.values() if a["type"] == "depository")
    credit_total   = sum(a["current"] for a in balances.values() if a["type"] == "credit")
    wise_usd_val   = wise_bal.get("USD", 0)
    wise_cad_val   = wise_bal.get("CAD", 0)
    wise_usd_cad   = wise_usd_val * USD_TO_CAD
    sol_cad        = sol_balance * sol_usd * USD_TO_CAD

    # Questrade — totalEquity CAD du premier compte (évite double-comptage)
    _qt = qt_data or {}
    _qt_bals = _qt.get("balances", [])
    _seen = set()
    qt_equity_cad = 0.0
    for b in _qt_bals:
        acc = b.get("_account", "")
        if b.get("currency") == "CAD" and acc not in _seen:
            qt_equity_cad += b.get("totalEquity", 0) or 0
            _seen.add(acc)

    net_worth = checking_total + wise_cad_val + wise_usd_cad + sol_cad + qt_equity_cad - credit_total

    # ── Account pills HTML ───────────────────────────────────────────────────
    pills_html = ""
    for acc in balances.values():
        is_credit = acc["type"] == "credit"
        cls = "debit" if is_credit else "credit"
        label = "Dû" if is_credit else ""
        val_str = f"{'-' if is_credit else ''}{fmt_cad(acc['current'])}"
        safe_id = acc['name'].replace(' ', '-').replace("'", "").lower()
        pills_html += f"""
    <div class="acc-pill" data-accid="{safe_id}">
      <span class="acc-label">{acc['name']}</span>
      <span class="acc-val {cls}">{val_str}</span>
    </div>"""
    if wise_usd_val or wise_cad_val:
        if wise_usd_val:
            pills_html += f"""
    <div class="acc-pill" data-accid="wise-usd">
      <span class="acc-label">Wise USD</span>
      <span class="acc-val wise">${wise_usd_val:,.2f} USD</span>
    </div>
    <div class="acc-pill" data-accid="wise-cad-conv">
      <span class="acc-label">Wise → CAD</span>
      <span class="acc-val wise">{fmt_cad(wise_usd_cad)}</span>
    </div>"""
        if wise_cad_val:
            pills_html += f"""
    <div class="acc-pill" data-accid="wise-cad">
      <span class="acc-label">Wise CAD</span>
      <span class="acc-val wise">{fmt_cad(wise_cad_val)}</span>
    </div>"""
    if sol_balance > 0:
        pills_html += f"""
    <div class="acc-pill" data-accid="phantom-sol">
      <span class="acc-label">Phantom SOL</span>
      <span class="acc-val wise">{sol_balance:.4f} SOL ≈ {fmt_cad(sol_cad)}</span>
    </div>"""
    if qt_equity_cad > 0:
        pills_html += f"""
    <div class="acc-pill" data-accid="questrade-tfsa">
      <span class="acc-label">Questrade TFSA</span>
      <span class="acc-val credit">{fmt_cad(qt_equity_cad)}</span>
    </div>"""

    # ── Transactions JSON ────────────────────────────────────────────────────
    txns_for_js = []
    for i, t in enumerate(txns):
        tid = t.get("id", "") or f"idx_{i}"
        txns_for_js.append({
            "date":     t["date"].strftime("%Y-%m-%d"),
            "amount":   round(t["amount"], 2),
            "name":     t["name"],
            "category": t["category"],
            "account":  t["account"],
            "id":       tid,
        })
    all_txns_json = json.dumps(txns_for_js)

    # ── Category colors map ──────────────────────────────────────────────────
    all_cats = list(dict.fromkeys(t["category"] for t in txns))
    donut_colors_js = json.dumps({cat: DONUT_COLORS[i % len(DONUT_COLORS)] for i, cat in enumerate(all_cats)})

    # ── Account name map + default colors ───────────────────────────────────
    # Default palette per account (stable order by first appearance)
    ACC_DEFAULT_PALETTE = [
        "#4fc978",  # vert
        "#c97ef7",  # purple
        "#f7c948",  # jaune
        "#4f86f7",  # bleu
        "#f7964f",  # orange
        "#4ff7e8",  # cyan
        "#f74fc9",  # rose
        "#f76e6e",  # rouge
        "#a8f74f",  # lime
        "#93c5fd",  # bleu pâle
    ]
    acc_ids_ordered = list(dict.fromkeys(t["account"] for t in txns))
    acc_name_map = {aid: balances[aid]["name"] if aid in balances else aid for aid in acc_ids_ordered}
    # Noms lisibles pour les comptes Wise et CSV
    for aid in acc_ids_ordered:
        if aid.startswith("wise_"):
            cur = aid.replace("wise_", "")
            acc_name_map[aid] = f"Wise {cur}"
        elif aid not in balances:
            # CSV account: use readable name (filename without ext)
            acc_name_map[aid] = aid.replace("_", " ").title()
    acc_default_colors = {aid: ACC_DEFAULT_PALETTE[i % len(ACC_DEFAULT_PALETTE)] for i, aid in enumerate(acc_ids_ordered)}
    acc_name_map_js   = json.dumps(acc_name_map)
    acc_default_colors_js = json.dumps(acc_default_colors)

    # ── Questrade data pour le tab Investissements ───────────────────────────
    qt_data = qt_data or {}
    qt_positions   = qt_data.get("positions", [])
    qt_balances    = qt_data.get("balances", [])
    qt_accounts    = qt_data.get("accounts", [])
    qt_fetched_at  = qt_data.get("fetched_at", "")
    qt_fetched_str = ""
    if qt_fetched_at:
        try:
            dt = datetime.fromisoformat(qt_fetched_at)
            qt_fetched_str = dt.strftime("%d %b %Y à %H:%M")
        except Exception:
            qt_fetched_str = qt_fetched_at[:16]

    # Calcul total portfolio Questrade en CAD — combinedBalances donne le compte
    # en CAD ET en USD (même argent converti), on prend juste le CAD total
    qt_total_cad = 0.0
    seen_accounts = set()
    for b in qt_balances:
        acc = b.get("_account", "")
        if b.get("currency") == "CAD" and acc not in seen_accounts:
            qt_total_cad += b.get("totalEquity", 0) or 0
            seen_accounts.add(acc)

    qt_positions_json = json.dumps(qt_positions)
    qt_balances_json  = json.dumps(qt_balances)
    qt_accounts_json  = json.dumps(qt_accounts)

    generated_at = datetime.now().strftime("%B %d, %Y at %H:%M")

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Budget Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@300;400;500;600;700&family=Geist+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --bg:          #0c0c0c;
    --surface:     #111111;
    --surface2:    #181818;
    --border:      #242424;
    --border-hi:   #333333;
    --lime:        #b8f566;
    --lime2:       #ceff7e;
    --lime-dim:    rgba(184,245,102,0.08);
    --lime-dim2:   rgba(184,245,102,0.15);
    --red:         #ff4757;
    --red-dim:     rgba(255,71,87,0.1);
    --blue:        #818cf8;
    --blue-dim:    rgba(129,140,248,0.1);
    --text:        #e8e8e8;
    --text2:       #aaaaaa;
    --muted:       #555555;
    --muted2:      #444444;
    --radius:      12px;
    --radius-sm:   8px;
    --sidebar-w:   210px;
    --mono:        'Geist Mono', 'SF Mono', monospace;
    --sans:        'Geist', system-ui, sans-serif;
    /* compat */
    --emerald:     #b8f566;
    --emerald2:    #ceff7e;
    --emerald-g:   linear-gradient(135deg,#b8f566,#ceff7e);
    --green:       #b8f566;
    --green2:      #a8e055;
  }}

  body {{
    font-family: var(--sans);
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    font-size: 14px;
  }}
  .mesh-bg {{ display: none; }}

  /* ── Layout ── */
  .app-shell {{
    display: flex;
    min-height: 100vh;
  }}

  /* ── Sidebar ── */
  .sidebar {{
    width: var(--sidebar-w);
    background: var(--surface);
    border-right: 1px solid var(--border);
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    position: sticky;
    top: 0;
    height: 100vh;
    overflow-y: auto;
  }}
  .sidebar-brand {{
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 22px 18px 18px;
    border-bottom: 1px solid var(--border);
  }}
  .sidebar-logo {{
    color: var(--lime);
    font-size: 1.1rem;
    font-weight: 800;
    line-height: 1;
  }}
  .sidebar-title {{
    font-size: 0.9rem;
    font-weight: 700;
    color: var(--text);
    letter-spacing: -0.01em;
  }}

  .sidebar-nav {{
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 14px 10px;
    flex: 1;
  }}
  .snav-btn {{
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 9px 10px;
    border-radius: var(--radius-sm);
    background: none;
    border: none;
    cursor: pointer;
    font-family: var(--sans);
    font-size: 0.8rem;
    font-weight: 500;
    color: var(--muted);
    transition: all 0.15s;
    text-align: left;
    width: 100%;
    letter-spacing: 0.01em;
  }}
  .snav-btn svg {{ opacity: 0.5; flex-shrink: 0; }}
  .snav-btn:hover {{
    background: var(--surface2);
    color: var(--text2);
  }}
  .snav-btn:hover svg {{ opacity: 0.8; }}
  .snav-btn.active {{
    background: var(--lime-dim);
    color: var(--lime);
    font-weight: 600;
  }}
  .snav-btn.active svg {{ opacity: 1; color: var(--lime); }}

  /* Sidebar sections */
  .sidebar-section {{ padding: 0 10px; margin-top: 6px; }}
  .sidebar-section-label {{
    font-size: 0.6rem;
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--muted2);
    padding: 0 4px;
    margin-bottom: 7px;
    display: block;
  }}
  .period-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 3px;
  }}
  .period-btn {{
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--muted);
    border-radius: 6px;
    padding: 6px 2px;
    font-size: 0.68rem;
    font-family: var(--sans);
    cursor: pointer;
    transition: all 0.15s;
    text-align: center;
    font-weight: 500;
  }}
  .period-btn:hover {{
    border-color: var(--lime);
    color: var(--lime);
  }}
  .period-btn.active {{
    background: var(--lime-dim);
    border-color: var(--lime);
    color: var(--lime);
    font-weight: 600;
  }}

  .month-picker-wrap {{ padding: 0 0px; }}
  .month-picker-btn {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    width: 100%;
    padding: 8px 10px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.72rem;
    font-family: var(--sans);
    color: var(--muted);
    transition: all 0.15s;
  }}
  .month-picker-btn:hover {{ border-color: var(--lime); color: var(--lime); }}

  .sidebar-footer {{
    padding: 14px 10px;
    border-top: 1px solid var(--border);
    margin-top: auto;
  }}
  .generated {{
    font-size: 0.58rem;
    color: var(--muted2);
    font-family: var(--mono);
    margin-bottom: 10px;
    padding: 0 4px;
    display: block;
  }}
  .refresh-btn {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 9px 16px;
    background: var(--lime);
    border: none;
    border-radius: var(--radius-sm);
    color: #0c0c0c;
    font-family: var(--sans);
    font-size: 0.78rem;
    font-weight: 700;
    cursor: pointer;
    transition: all 0.15s;
    width: 100%;
    letter-spacing: 0.03em;
  }}
  .refresh-btn:hover {{
    background: var(--lime2);
    transform: translateY(-1px);
    box-shadow: 0 4px 16px rgba(184,245,102,0.25);
  }}

  /* ── Main content ── */
  .content {{
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    border-left: 20px solid #050505;
  }}

  /* ── Header ── */
  .header {{
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 24px 32px;
  }}
  .header-top {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    flex-wrap: wrap;
    gap: 20px;
  }}
  .header h1 {{
    font-size: 0.6rem;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 8px;
  }}
  .net-worth-amount {{
    font-family: var(--mono);
    font-size: clamp(1.8rem, 3vw, 2.6rem);
    font-weight: 700;
    letter-spacing: -0.04em;
    line-height: 1;
    color: var(--lime);
    display: flex;
    align-items: baseline;
    gap: 3px;
  }}
  .net-worth-amount .currency {{
    font-size: 0.4em;
    color: var(--muted);
    font-weight: 500;
  }}

  /* Accounts */
  .accounts-grid {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 20px;
  }}
  .acc-pill {{
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 10px 14px;
    display: flex;
    flex-direction: column;
    min-width: 115px;
    transition: all 0.15s;
    cursor: default;
  }}
  .acc-pill:hover {{
    border-color: var(--lime);
    background: var(--lime-dim);
    transform: translateY(-1px);
  }}
  .acc-pill .acc-label {{
    font-size: 0.56rem;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 5px;
  }}
  .acc-pill .acc-val {{
    font-family: var(--mono);
    font-size: 0.9rem;
    font-weight: 600;
    color: var(--text);
  }}
  .acc-pill .acc-val.debit {{ color: var(--red); }}
  .acc-pill .acc-val.wise {{ color: var(--blue); }}

  /* ── Tabs ── */
  .tabs-nav {{
    display: flex;
    border-bottom: 1px solid var(--border);
    margin: 0 0 28px;
    padding: 0 32px;
    background: var(--surface);
    position: sticky;
    top: 0;
    z-index: 50;
  }}
  .tab-btn {{
    background: none;
    border: none;
    color: var(--muted);
    cursor: pointer;
    font-family: var(--sans);
    font-size: 0.8rem;
    font-weight: 500;
    padding: 14px 16px;
    position: relative;
    transition: color 0.15s;
    letter-spacing: 0.02em;
  }}
  .tab-btn:hover {{ color: var(--text2); }}
  .tab-btn.active {{ color: var(--text); font-weight: 600; }}
  .tab-btn.active::after {{
    content: '';
    position: absolute;
    bottom: -1px; left: 0; right: 0;
    height: 2px;
    background: var(--lime);
    border-radius: 2px 2px 0 0;
    box-shadow: 0 0 8px rgba(184,245,102,0.4);
  }}
  .tab-panel {{ display: none; }}
  .tab-panel.active {{ display: block; }}

  /* ── Main area ── */
  .main {{
    max-width: 1100px;
    margin: 0 auto;
    padding: 28px 32px 60px;
    width: 100%;
  }}
  .section-title {{
    font-size: 0.6rem;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 12px;
  }}

  /* ── Glass cards ── */
  .glass {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    transition: all 0.15s;
  }}
  .glass:hover {{
    border-color: var(--border-hi);
    background: var(--surface2);
  }}
  .glass-spotlight {{ position: relative; overflow: hidden; }}
  .glass-spotlight::after {{ content: none; }}

  .nw-label {{
    font-size: 0.6rem;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 8px;
  }}

  /* ── Monthly cards ── */
  .monthly-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: 8px;
    margin-bottom: 20px;
  }}
  .month-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px;
    transition: all 0.15s;
  }}
  .month-card:hover {{
    border-color: var(--lime);
    background: var(--surface2);
  }}
  .month-card::before, .month-card::after {{ content: none; }}
  .month-card h3 {{
    font-size: 0.6rem;
    font-weight: 600;
    margin-bottom: 12px;
    color: var(--muted);
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }}
  .month-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 5px 0;
    font-size: 0.78rem;
    border-bottom: 1px solid var(--border);
  }}
  .month-row:last-child {{ border-bottom: none; }}
  .net-row {{
    font-weight: 700;
    font-size: 0.86rem;
    margin-top: 4px;
    font-family: var(--mono);
  }}
  .green {{ color: var(--lime); }}
  .red {{ color: var(--red); }}

  /* ── Charts ── */
  .charts-row {{
    display: grid;
    grid-template-columns: 1fr 1.7fr;
    gap: 8px;
    margin-bottom: 20px;
  }}
  @media(max-width:700px) {{ .charts-row {{ grid-template-columns: 1fr; }} }}
  .chart-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 18px;
    transition: all 0.15s;
  }}
  .chart-card:hover {{ border-color: var(--border-hi); }}
  .chart-card h2 {{
    font-size: 0.6rem;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 14px;
  }}
  .chart-wrapper {{ position: relative; height: 240px; }}
  .donut-layout {{ display: flex; gap: 18px; align-items: flex-start; flex-wrap: wrap; }}
  .donut-legend {{
    flex: 1;
    min-width: 120px;
    display: flex;
    flex-direction: column;
    gap: 5px;
    max-height: 240px;
    overflow-y: auto;
    padding-right: 4px;
  }}
  .donut-legend::-webkit-scrollbar {{ width: 2px; }}
  .donut-legend::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 4px; }}
  .legend-item {{
    display: flex;
    align-items: center;
    gap: 7px;
    font-size: 0.72rem;
    font-weight: 500;
    color: var(--text2);
    transition: opacity 0.15s;
    cursor: default;
  }}
  .legend-item:hover {{ opacity: 0.7; }}
  .legend-dot {{ width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }}
  .legend-val {{
    margin-left: auto;
    color: var(--text);
    font-family: var(--mono);
    font-size: 0.68rem;
    font-weight: 600;
  }}

  .acc-legend-item {{
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 0.68rem;
    font-weight: 500;
    color: var(--muted);
    cursor: pointer;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 3px 10px;
    transition: all 0.15s;
  }}
  .acc-legend-item:hover {{ border-color: var(--lime); color: var(--lime); }}
  .acc-legend-dot {{ width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }}

  /* ── Table ── */
  .table-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 18px;
    margin-bottom: 20px;
    overflow-x: auto;
  }}
  .table-card h2 {{
    font-size: 0.6rem;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 14px;
  }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{
    font-size: 0.6rem;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
    padding: 8px 12px;
    text-align: left;
    border-bottom: 1px solid var(--border);
  }}
  td {{
    padding: 10px 12px;
    font-size: 0.8rem;
    border-bottom: 1px solid var(--border);
    vertical-align: middle;
    transition: background 0.1s;
    color: var(--text2);
  }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: var(--surface2); color: var(--text); }}
  .badge {{
    background: var(--lime-dim);
    border: 1px solid rgba(184,245,102,0.2);
    color: var(--lime);
    border-radius: 5px;
    padding: 2px 8px;
    font-size: 0.6rem;
    font-weight: 600;
    white-space: nowrap;
    cursor: pointer;
    transition: all 0.15s;
    font-family: var(--mono);
  }}
  .badge:hover {{ background: var(--lime-dim2); border-color: var(--lime); }}
  td.amount, .mono {{ font-family: var(--mono); }}

  /* ── Budget ── */
  .budget-layout {{ display: grid; grid-template-columns: 1fr 270px; gap: 20px; align-items: start; }}
  @media(max-width:900px) {{ .budget-layout {{ grid-template-columns: 1fr; }} }}
  .budget-row {{
    display: grid;
    grid-template-columns: 16px 22px 1fr auto auto;
    align-items: center;
    gap: 10px;
    padding: 12px 0;
    border-bottom: 1px solid var(--border);
    cursor: grab;
  }}
  .budget-row:last-child {{ border-bottom: none; }}
  .budget-row.drag-over {{ background: rgba(184,245,102,0.04); border-radius:6px; }}
  .brow-drag {{ color: var(--muted); font-size: 14px; cursor: grab; user-select:none; }}
  .brow-icon {{ font-size: 15px; text-align:center; }}
  .brow-body {{ display:flex; flex-direction:column; gap:6px; min-width:0; }}
  .brow-name {{ font-size: 12px; font-weight: 600; color: var(--text); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; letter-spacing:0.02em; }}
  .brow-track {{
    height: 5px;
    background: rgba(255,255,255,0.07);
    border-radius: 99px;
    overflow: hidden;
    position: relative;
  }}
  .brow-fill {{
    height: 100%;
    border-radius: 99px;
    transition: width 1.1s cubic-bezier(0.16,1,0.3,1);
    position: relative;
  }}
  .brow-fill::after {{
    content: '';
    position: absolute;
    top: 0; right: 0; bottom: 0;
    width: 20px;
    background: linear-gradient(90deg, transparent, rgba(255,255,255,0.25));
    border-radius: 99px;
  }}
  .brow-right {{
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 2px;
    flex-shrink: 0;
  }}
  .brow-spent {{ font-family: var(--mono); font-size: 13px; font-weight: 700; line-height:1; }}
  .brow-target {{ font-family: var(--mono); font-size: 10px; color: var(--muted); cursor: pointer; }}
  .brow-target:hover {{ color: var(--lime); }}
  .brow-over {{ font-family: var(--mono); font-size: 10px; color: #ff4444; font-weight:600; }}
  .brow-del {{
    background: none; border: none; color: var(--muted);
    font-size: 13px; cursor: pointer; padding: 2px 4px;
    border-radius: 4px; transition: color 0.15s, background 0.15s;
    flex-shrink: 0;
  }}
  .brow-del:hover {{ color: #ff4444; background: rgba(255,68,68,0.08); }}
  .budget-divider {{
    font-size: 10px; font-weight: 700; letter-spacing: 0.12em;
    text-transform: uppercase; color: var(--muted);
    padding: 14px 0 6px; border-bottom: none !important;
    cursor: default;
  }}
  .budget-cat {{ flex: 1; font-size: 0.82rem; font-weight: 500; color: var(--text2); }}
  .budget-bar-wrap {{
    flex: 2; height: 4px; background: var(--border);
    border-radius: 99px; overflow: hidden;
  }}
  .budget-bar {{ height: 100%; border-radius: 99px; background: var(--lime); transition: width 0.4s; }}
  .budget-bar.over {{ background: var(--red); }}
  .budget-amt {{ font-family: var(--mono); font-size: 0.76rem; color: var(--text); min-width: 68px; text-align: right; }}
  .budget-target {{ font-family: var(--mono); font-size: 0.7rem; color: var(--muted); min-width: 58px; text-align: right; }}
  .budget-edit {{
    background: none; border: 1px solid var(--border);
    border-radius: 5px; padding: 3px 7px;
    font-size: 0.68rem; color: var(--muted); cursor: pointer; transition: all 0.15s;
  }}
  .budget-edit:hover {{ border-color: var(--lime); color: var(--lime); }}

  /* Budget header buttons */
  .bud-btn {{
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    background: none;
    border: 1px solid rgba(255,255,255,0.1);
    color: var(--muted);
    padding: 5px 10px;
    cursor: pointer;
    border-radius: 4px;
    transition: border-color 0.15s, color 0.15s, background 0.15s;
    white-space: nowrap;
  }}
  .bud-btn:hover {{
    border-color: var(--lime);
    color: var(--lime);
  }}
  .bud-btn-accent {{
    border-color: var(--lime);
    color: var(--lime);
  }}
  .bud-btn-accent:hover {{
    background: var(--lime);
    color: #0c0c0c;
  }}

  .summary-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px;
  }}
  .summary-card h3 {{
    font-size: 0.6rem;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 12px;
  }}
  .summary-row {{
    display: flex;
    justify-content: space-between;
    padding: 6px 0;
    font-size: 0.8rem;
    border-bottom: 1px solid var(--border);
    color: var(--text2);
  }}
  .summary-row:last-child {{ border-bottom: none; }}
  .summary-total {{
    display: flex;
    justify-content: space-between;
    padding: 10px 0 0;
    font-size: 0.9rem;
    font-weight: 700;
    font-family: var(--mono);
    color: var(--text);
  }}

  /* Investments */
  .inv-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(185px, 1fr));
    gap: 8px;
    margin-bottom: 20px;
  }}
  .inv-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 18px;
    transition: all 0.15s;
  }}
  .inv-card:hover {{ border-color: var(--lime); background: var(--surface2); }}
  .inv-card h3 {{
    font-size: 0.6rem;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 10px;
  }}
  .inv-val {{
    font-family: var(--mono);
    font-size: 1.4rem;
    font-weight: 700;
    color: var(--lime);
    letter-spacing: -0.02em;
    margin-bottom: 4px;
  }}
  .inv-sub {{ font-size: 0.7rem; color: var(--muted); }}
  .inv-badge {{
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 0.66rem;
    font-weight: 600;
    padding: 3px 8px;
    border-radius: 99px;
    margin-top: 8px;
    font-family: var(--mono);
  }}
  .inv-badge.up {{ background: var(--lime-dim); color: var(--lime); }}
  .inv-badge.down {{ background: var(--red-dim); color: var(--red); }}

  /* Modal */
  .modal-overlay {{
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.7);
    backdrop-filter: blur(6px);
    z-index: 1000;
    display: flex;
    align-items: center;
    justify-content: center;
  }}
  .modal {{
    background: var(--surface);
    border: 1px solid var(--border-hi);
    border-radius: var(--radius);
    padding: 24px;
    width: 360px;
    box-shadow: 0 24px 64px rgba(0,0,0,0.5);
  }}
  .modal h3 {{ font-size: 1rem; font-weight: 700; margin-bottom: 16px; color: var(--text); }}
  .modal-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }}
  .modal-cat {{
    padding: 9px 12px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border);
    background: var(--surface2);
    cursor: pointer;
    font-size: 0.8rem;
    font-weight: 500;
    color: var(--text2);
    transition: all 0.15s;
    text-align: left;
  }}
  .modal-cat:hover {{ border-color: var(--lime); color: var(--lime); background: var(--lime-dim); }}
  .modal-cancel {{
    margin-top: 10px;
    width: 100%;
    padding: 10px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border);
    background: none;
    cursor: pointer;
    font-size: 0.8rem;
    color: var(--muted);
    transition: all 0.15s;
    font-family: var(--sans);
  }}
  .modal-cancel:hover {{ color: var(--text); border-color: var(--border-hi); }}

  /* Doc modal */
  .doc-modal {{
    background: var(--surface);
    border: 1px solid var(--border-hi);
    border-radius: var(--radius);
    padding: 24px;
    width: 480px;
    max-width: 95vw;
    max-height: 85vh;
    overflow-y: auto;
    box-shadow: 0 24px 64px rgba(0,0,0,0.6);
  }}
  .doc-modal h3 {{ font-size: 1rem; font-weight: 700; margin: 0 0 4px; color: var(--text); }}
  .doc-modal .doc-sub {{ font-size: 0.75rem; color: var(--muted); margin-bottom: 16px; }}
  .doc-upload-zone {{
    border: 2px dashed var(--border-hi);
    border-radius: var(--radius-sm);
    padding: 24px;
    text-align: center;
    cursor: pointer;
    transition: all 0.2s;
    margin-bottom: 16px;
    background: var(--surface2);
  }}
  .doc-upload-zone:hover {{ border-color: var(--lime); background: var(--lime-dim); }}
  .doc-upload-zone .doc-zone-icon {{ font-size: 2rem; margin-bottom: 8px; }}
  .doc-upload-zone p {{ font-size: 0.8rem; color: var(--muted); margin: 0; }}
  .doc-upload-zone strong {{ color: var(--lime); }}
  .doc-btn-row {{ display: flex; gap: 8px; margin-bottom: 16px; }}
  .doc-btn {{
    flex: 1;
    padding: 10px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border-hi);
    background: var(--surface2);
    color: var(--text);
    font-family: var(--sans);
    font-size: 0.8rem;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s;
    display: flex; align-items: center; justify-content: center; gap: 6px;
  }}
  .doc-btn:hover {{ border-color: var(--lime); color: var(--lime); background: var(--lime-dim); }}
  .doc-btn.primary {{ border-color: var(--lime); color: #000; background: var(--lime); }}
  .doc-btn.primary:hover {{ background: #d4ff80; border-color: #d4ff80; }}
  .doc-list {{ display: flex; flex-direction: column; gap: 8px; margin-bottom: 16px; }}
  .doc-item {{
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border);
    background: var(--surface2);
  }}
  .doc-item-thumb {{
    width: 44px; height: 44px;
    border-radius: 4px;
    object-fit: cover;
    flex-shrink: 0;
    cursor: pointer;
    border: 1px solid var(--border);
  }}
  .doc-item-thumb.pdf-thumb {{
    display: flex; align-items: center; justify-content: center;
    font-size: 1.4rem; background: #1a1a1a; color: #e55;
  }}
  .doc-item-info {{ flex: 1; min-width: 0; }}
  .doc-item-name {{ font-size: 0.8rem; font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .doc-item-meta {{ font-size: 0.7rem; color: var(--muted); margin-top: 2px; }}
  .doc-item-del {{
    background: none; border: none; cursor: pointer;
    color: #555; font-size: 1rem; padding: 4px;
    transition: color 0.15s;
    flex-shrink: 0;
  }}
  .doc-item-del:hover {{ color: #e55; }}
  .doc-preview-overlay {{
    position: fixed; inset: 0; z-index: 2000;
    background: rgba(0,0,0,0.9); backdrop-filter: blur(8px);
    display: flex; align-items: center; justify-content: center; cursor: zoom-out;
  }}
  .doc-preview-overlay img {{ max-width: 90vw; max-height: 90vh; border-radius: 8px; object-fit: contain; }}

  /* Toast */
  .toast {{
    position: fixed;
    bottom: 24px;
    right: 24px;
    z-index: 9999;
    background: var(--surface);
    border: 1px solid var(--border-hi);
    color: var(--text);
    border-radius: var(--radius-sm);
    padding: 12px 20px;
    font-size: 0.8rem;
    font-weight: 500;
    box-shadow: 0 8px 32px rgba(0,0,0,0.4);
    animation: slideUp 0.2s ease;
  }}
  .toast.green {{ background: var(--lime-dim); border-color: var(--lime); color: var(--lime); }}

  @keyframes slideUp {{ from {{ opacity:0; transform:translateY(8px); }} to {{ opacity:1; transform:translateY(0); }} }}
  .num-flash {{ animation: numFlash 0.3s cubic-bezier(0.16,1,0.3,1); }}
  @keyframes numFlash {{ 0%{{opacity:0.3;transform:translateY(4px);}} 100%{{opacity:1;transform:translateY(0);}} }}
  @keyframes spin-icon {{ to {{ transform: rotate(360deg); }} }}
  @keyframes shimmer {{ 0%{{background-position:-200% 0;}} 100%{{background-position:200% 0;}} }}
</style>
</head>
<body>

<div class="mesh-bg"></div>

<div class="app-shell">

  <!-- ── Sidebar ── -->
  <aside class="sidebar">
    <div class="sidebar-brand">
      <span class="sidebar-logo">◈</span>
      <span class="sidebar-title">Budget</span>
    </div>

    <nav class="sidebar-nav">
      <button class="snav-btn active" onclick="switchTab('overview')">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
        Vue générale
      </button>
      <button class="snav-btn" onclick="switchTab('budget')">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 12h20M2 6h20M2 18h12"/></svg>
        Budget
      </button>
      <button class="snav-btn" onclick="switchTab('txns')">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 6l4-4 4 4M16 18l-4 4-4-4M12 2v20"/></svg>
        Transactions
      </button>
      <button class="snav-btn" onclick="switchTab('invest')">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>
        Investissements
      </button>
    </nav>

    <div class="sidebar-section-label">Période</div>
    <div class="sidebar-period">
      <button class="period-btn active" data-days="30"   onclick="setPeriod(30)">30j</button>
      <button class="period-btn"        data-days="60"   onclick="setPeriod(60)">60j</button>
      <button class="period-btn"        data-days="90"   onclick="setPeriod(90)">90j</button>
      <button class="period-btn"        data-days="180"  onclick="setPeriod(180)">6 mois</button>
      <button class="period-btn"        data-days="365"  onclick="setPeriod(365)">12 mois</button>
      <button class="period-btn"        data-days="9999" onclick="setPeriod(9999)">Tout</button>
    </div>

    <div class="sidebar-section-label" style="margin-top:8px">Mois</div>
    <div style="position:relative">
      <button id="cal-btn" onclick="toggleCal()" style="width:100%;background:rgba(255,255,255,0.035);border:1px solid var(--border);border-radius:10px;padding:8px 12px;color:var(--muted2);font-family:var(--sans);font-size:12px;cursor:pointer;display:flex;align-items:center;gap:6px;transition:all 0.2s">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>
        <span id="cal-label">Choisir un mois</span>
      </button>
      <div id="cal-picker" style="display:none;position:absolute;top:calc(100% + 8px);left:0;z-index:200;background:#0f0f1c;border:1px solid rgba(255,255,255,0.08);border-radius:14px;padding:16px;box-shadow:0 16px 48px rgba(0,0,0,.8);min-width:240px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
          <button onclick="calShiftYear(-1)" style="background:none;border:none;color:var(--muted2);font-size:16px;cursor:pointer;padding:0 6px">‹</button>
          <span id="cal-year-label" style="font-size:12px;font-weight:500;color:var(--text);letter-spacing:0.06em">2026</span>
          <button onclick="calShiftYear(1)"  style="background:none;border:none;color:var(--muted2);font-size:16px;cursor:pointer;padding:0 6px">›</button>
        </div>
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:5px;margin-bottom:12px" id="cal-months"></div>
        <div style="border-top:1px solid rgba(255,255,255,0.05);padding-top:10px">
          <div style="font-size:10px;color:var(--muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:.08em">Plage custom</div>
          <div style="display:flex;gap:6px;align-items:center">
            <input id="range-from" type="date" onchange="applyRange()" style="flex:1;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:7px;padding:5px 7px;color:var(--text);font-family:var(--sans);font-size:11px">
            <span style="color:var(--muted)">→</span>
            <input id="range-to"   type="date" onchange="applyRange()" style="flex:1;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:7px;padding:5px 7px;color:var(--text);font-family:var(--sans);font-size:11px">
          </div>
          <button id="range-clear" onclick="clearRange()" style="display:none;margin-top:6px;background:none;border:none;color:var(--muted);font-size:11px;font-family:var(--sans);cursor:pointer">✕ Effacer</button>
        </div>
      </div>
    </div>

    <div class="sidebar-footer">
      <div class="generated">Generated {generated_at}</div>
      <button id="refresh-btn" onclick="doRefresh()" class="refresh-btn" style="width:100%;justify-content:center;margin-top:8px">
        <span id="refresh-icon" style="font-size:14px">&#8635;</span> Refresh
      </button>
    </div>
  </aside>

  <!-- ── Main content ── -->
  <main class="content">

    <!-- Net worth header -->
    <div class="nw-header">
      <div>
        <div class="nw-label">Net Worth</div>
        <div class="net-worth-amount" id="nw-display"><span class="currency">$</span><span id="nw-number">{fmt_cad(net_worth).replace('$','').strip()}</span></div>
      </div>
      <div class="accounts-grid">
        {pills_html}
      </div>
    </div>

    <!-- Tab panels -->
    <div id="tab-overview" class="tab-panel active">

      <!-- ── Income/Expenses summary row ── -->
      <div id="income-summary" style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:24px"></div>

      <div class="section-title">Résumé mensuel</div>
      <div class="monthly-grid" id="monthly-grid"></div>
      <div class="charts-row">
        <div class="chart-card">
          <h2>Dépenses par catégorie</h2>
          <div class="donut-layout">
            <div style="width:180px;height:180px;flex-shrink:0;position:relative">
              <canvas id="donutChart"></canvas>
            </div>
            <div class="donut-legend" id="donutLegend"></div>
          </div>
        </div>
        <div class="chart-card">
          <h2>Tendance hebdomadaire (net)</h2>
          <div class="chart-wrapper">
          <canvas id="weeklyChart"></canvas>
        </div>
        </div>
      </div>

      <!-- ── Top 10 marchands ── -->
      <div class="chart-card" style="margin-top:16px">
        <h2>Top 10 marchands</h2>
        <div id="top-merchants"></div>
      </div>

    </div>

  <!-- Tab: Budget -->
  <div id="tab-budget" class="tab-panel">
    <div class="budget-layout">

      <!-- Left: rows -->
      <div class="budget-left">
        <div class="budget-header">
          <span id="budget-title" style="font-size:11px;font-weight:500;letter-spacing:0.18em;text-transform:uppercase;color:var(--muted)">Budget · 30 jours</span>
          <div style="display:flex;gap:5px">
            <button onclick="addCustomCat()" class="bud-btn bud-btn-accent">+ Cat</button>
            <button onclick="addBudgetDivider()" class="bud-btn">+ Section</button>
            <button onclick="restoreHidden()" class="bud-btn">Cachées</button>
            <button onclick="restoreDeleted()" class="bud-btn">Supprimées</button>
          </div>
        </div>
        <div id="budget-bars"></div>
      </div>

      <!-- Right: score -->
      <div class="budget-right" id="budget-suggestions"></div>

    </div>
  </div>

  <!-- Tab: Transactions -->
  <div id="tab-txns" class="tab-panel">

    <!-- CSV Import Card -->
    <div class="table-card" style="margin-bottom:0">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:8px">
        <h2 style="margin:0;font-size:1rem">📂 Importer un relevé CSV</h2>
        <span style="font-size:11px;color:#555">Formats supportés: RBC, Scotia, TD, Desjardins, générique</span>
      </div>
      <div id="csv-drop-zone"
        ondragover="event.preventDefault();this.style.borderColor='#4f86f7'"
        ondragleave="this.style.borderColor='#333'"
        ondrop="event.preventDefault();this.style.borderColor='#333';handleCsvFiles(event.dataTransfer.files)"
        style="border:2px dashed #333;border-radius:10px;padding:20px;text-align:center;cursor:pointer;transition:border-color .2s"
        onclick="document.getElementById('csv-file-input').click()">
        <div style="color:#666;font-size:13px;margin-bottom:8px">Glisse un fichier CSV ici ou</div>
        <button style="background:#23232a;border:1px solid #444;border-radius:6px;color:#f1f1f1;padding:6px 16px;font-size:13px;cursor:pointer;font-family:'Geist',sans-serif">
          Choisir un fichier .csv
        </button>
        <input id="csv-file-input" type="file" accept=".csv" style="display:none"
          onchange="handleCsvFiles(this.files)">
      </div>
      <div id="csv-status" style="margin-top:10px;font-size:13px;color:#4fc978;display:none"></div>
    </div>

    <div class="table-card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:10px">
        <h2 style="margin:0">Toutes les transactions</h2>
        <div id="acc-legend" style="display:flex;flex-wrap:wrap;gap:8px;align-items:center"></div>
      </div>
      <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px">
        <input id="txn-search" type="text" placeholder="Rechercher…"
          oninput="renderTxns(this.value,document.getElementById('txn-acct').value,document.getElementById('txn-cat').value)"
          style="flex:1;min-width:180px;background:#181818;border:1px solid #333;border-radius:8px;padding:8px 12px;color:#f1f1f1;font-family:'Geist',sans-serif">
        <select id="txn-acct" onchange="renderTxns(document.getElementById('txn-search').value,this.value,document.getElementById('txn-cat').value)"
          style="background:#181818;border:1px solid #333;border-radius:8px;padding:8px 12px;color:#f1f1f1;font-family:'Geist',sans-serif">
          <option value="">Tous les comptes</option>
        </select>
        <select id="txn-cat" onchange="renderTxns(document.getElementById('txn-search').value,document.getElementById('txn-acct').value,this.value)"
          style="background:#181818;border:1px solid #333;border-radius:8px;padding:8px 12px;color:#f1f1f1;font-family:'Geist',sans-serif">
          <option value="">Toutes catégories</option>
        </select>
        <span id="txn-count" style="align-self:center;color:#777;font-size:13px"></span>
        <button onclick="exportFilteredCsv()" style="background:#181818;border:1px solid #b8f566;border-radius:8px;padding:7px 14px;color:#b8f566;font-family:'Geist',sans-serif;font-size:12px;font-weight:600;cursor:pointer;align-self:center">⬇ Exporter CSV</button>
      </div>
      <div style="overflow-x:auto">
        <table>
          <thead><tr>
            <th>Date</th><th>Marchand</th><th>Catégorie</th><th>Compte</th>
            <th style="text-align:right">Montant</th>
            <th style="text-align:center;width:36px" title="Documents attachés">📎</th>
          </tr></thead>
          <tbody id="txn-tbody"></tbody>
        </table>
      </div>
    </div>

    <!-- Modal documents -->
    <div id="doc-modal-overlay" class="modal-overlay" style="display:none" onclick="if(event.target===this)closeDocModal()">
      <div class="doc-modal" onclick="event.stopPropagation()">
        <h3 id="doc-modal-title">Documents</h3>
        <div id="doc-modal-sub" class="doc-sub"></div>

        <!-- Zone drag & drop / clic -->
        <div class="doc-upload-zone" id="doc-drop-zone"
          onclick="document.getElementById('doc-file-input').click()"
          ondragover="event.preventDefault();this.style.borderColor='var(--lime)'"
          ondragleave="this.style.borderColor=''"
          ondrop="handleDocDrop(event)">
          <div class="doc-zone-icon">📄</div>
          <p>Glisser un fichier ici ou <strong>cliquer pour parcourir</strong></p>
          <p style="margin-top:6px;font-size:0.7rem">JPG, PNG, PDF, HEIC — max 10 MB</p>
        </div>

        <!-- Boutons caméra / galerie (utile mobile) -->
        <div class="doc-btn-row">
          <button class="doc-btn" onclick="document.getElementById('doc-camera-input').click()">
            📷 Prendre une photo
          </button>
          <button class="doc-btn" onclick="document.getElementById('doc-file-input').click()">
            🗂 Depuis la galerie
          </button>
        </div>

        <!-- Liste docs existants -->
        <div id="doc-list" class="doc-list"></div>

        <!-- Fermer -->
        <button class="modal-cancel" onclick="closeDocModal()">Fermer</button>
      </div>
    </div>

    <!-- Inputs cachés -->
    <input type="file" id="doc-file-input" accept="image/*,application/pdf,.heic,.heif"
      multiple style="display:none" onchange="handleDocFiles(this.files)">
    <input type="file" id="doc-camera-input" accept="image/*" capture="environment"
      style="display:none" onchange="handleDocFiles(this.files)">

    <!-- Tab: Règles de catégorie -->
    <div class="table-card" style="margin-top:0">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
        <h2 style="margin:0">Règles de catégorie</h2>
        <span style="font-size:12px;color:#444">Appliquées automatiquement à tous les marchands</span>
      </div>
      <div id="rules-panel">
        <p style="color:#444;font-size:13px">Aucune règle — change la catégorie d'une transaction pour en créer une.</p>
      </div>
    </div>

    <!-- Abonnements détectés -->
    <div class="table-card" style="margin-top:0">
      <div onclick="toggleAbonnements()" style="display:flex;justify-content:space-between;align-items:center;cursor:pointer;user-select:none">
        <h2 style="margin:0">Abonnements détectés</h2>
        <span id="abo-chevron" style="font-size:18px;color:#555;transition:transform .2s">▶</span>
      </div>
      <div id="abo-panel" style="display:none;margin-top:16px">
        <div id="abo-list"></div>
      </div>
    </div>
  </div>

  <!-- Tab: Investissements -->
  <div id="tab-invest" class="tab-panel">
    <div class="table-card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;flex-wrap:wrap;gap:12px">
        <div>
          <h2 style="margin:0 0 4px">Portfolio Questrade</h2>
          {f'<span style="font-size:11px;color:#555">Données du {qt_fetched_str}</span>' if qt_fetched_str else '<span style="font-size:11px;color:#f76e6e">Aucune donnée — ajouter QUESTRADE_REFRESH_TOKEN dans .env</span>'}
        </div>
        <div id="qt-total-cad" style="font-size:1.8rem;font-weight:800;background:linear-gradient(90deg,#fff,#93c5fd);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text">
          {fmt_cad(qt_total_cad) if qt_total_cad else "—"}
        </div>
      </div>

      <!-- Balances par compte -->
      <div id="qt-balances" style="display:flex;flex-wrap:wrap;gap:12px;margin-bottom:28px"></div>

      <!-- Positions table -->
      <div style="overflow-x:auto">
        <table id="qt-table">
          <thead><tr>
            <th>Symbole</th><th>Description</th><th>Compte</th>
            <th style="text-align:right">Qté</th>
            <th style="text-align:right">Prix moyen</th>
            <th style="text-align:right">Prix actuel</th>
            <th style="text-align:right">Valeur marché</th>
            <th style="text-align:right">G/P $</th>
            <th style="text-align:right">G/P %</th>
          </tr></thead>
          <tbody id="qt-tbody"></tbody>
        </table>
      </div>
    </div>
  </div>

</div>

<script>
// ── Data ──────────────────────────────────────────────────────────────────────
const ALL_TXNS          = {all_txns_json};
const CAT_COLORS        = {donut_colors_js};
const ACC_NAME_MAP      = {acc_name_map_js};
const ACC_DEFAULT_COLORS= {acc_default_colors_js};
const QT_POSITIONS      = {qt_positions_json};
const QT_BALANCES       = {qt_balances_json};
const QT_ACCOUNTS       = {qt_accounts_json};
const OVERRIDES    = JSON.parse(localStorage.getItem('catOverrides') || '{{}}');
const NAMES        = JSON.parse(localStorage.getItem('nameOverrides') || '{{}}');
const SELF_PAYS    = JSON.parse(localStorage.getItem('selfPayIds') || '[]');
const SELF_PAY_SET = new Set(SELF_PAYS);
window.OVERRIDES   = OVERRIDES;

// ── Couleurs comptes (custom ou défaut) ───────────────────────────────────────
function getAccColors() {{
  const saved = JSON.parse(localStorage.getItem('accColors') || '{{}}');
  return Object.assign({{}}, ACC_DEFAULT_COLORS, saved);
}}
function setAccColor(accId, color) {{
  const saved = JSON.parse(localStorage.getItem('accColors') || '{{}}');
  saved[accId] = color;
  localStorage.setItem('accColors', JSON.stringify(saved));
}}
function resetAccColor(accId) {{
  const saved = JSON.parse(localStorage.getItem('accColors') || '{{}}');
  delete saved[accId];
  localStorage.setItem('accColors', JSON.stringify(saved));
}}
function getAccName(accId) {{
  return ACC_NAME_MAP[accId] || accId;
}}

// ── Self-payment toggle ────────────────────────────────────────────────────────
function toggleSelfPay(id) {{
  if (SELF_PAY_SET.has(id)) {{
    SELF_PAY_SET.delete(id);
    const idx = SELF_PAYS.indexOf(id);
    if (idx !== -1) SELF_PAYS.splice(idx, 1);
  }} else {{
    SELF_PAY_SET.add(id);
    SELF_PAYS.push(id);
  }}
  localStorage.setItem('selfPayIds', JSON.stringify(SELF_PAYS));
  renderTxns(
    document.getElementById('txn-search')?.value||'',
    document.getElementById('txn-acct')?.value||'',
    document.getElementById('txn-cat')?.value||''
  );
  if (window.initializedTabs?.has('budget')) renderBudget(getFilteredTxns(currentDays));
  if (window.initializedTabs?.has('overview')) refreshAll();
}}

// ── Tab switching ─────────────────────────────────────────────────────────────
let initializedTabs = new Set(['overview']);

function switchTab(name) {{
  document.querySelectorAll('.tab-btn, .snav-btn').forEach((b,i) => {{
    b.classList.toggle('active', ['overview','budget','txns','invest'][i % 4] === name);
  }});
  document.querySelectorAll('.snav-btn').forEach((b,i) => {{
    b.classList.toggle('active', ['overview','budget','txns','invest'][i] === name);
  }});
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  if (!initializedTabs.has(name)) {{
    initializedTabs.add(name);
    const txns = getFilteredTxns(currentDays);
    if (name === 'budget')  renderBudget(txns);
    if (name === 'txns')    initTxns();
    if (name === 'invest')  initInvest();
  }} else if (name === 'txns') {{
    renderTxns(document.getElementById('txn-search')?.value||'', document.getElementById('txn-acct')?.value||'', document.getElementById('txn-cat')?.value||'');
  }}
}}

// ── Period filter ─────────────────────────────────────────────────────────────
let currentDays = 30, customFrom = null, customTo = null;
let _calYear = new Date().getFullYear();
const MONTH_NAMES_SHORT = ['Jan','Fév','Mar','Avr','Mai','Jun','Jul','Aoû','Sep','Oct','Nov','Déc'];

function getFilteredTxns(days) {{
  if (customFrom && customTo) {{
    return ALL_TXNS.filter(t => {{
      const d = new Date(t.date + 'T00:00:00');
      return d >= customFrom && d <= customTo;
    }});
  }}
  const cutoff = new Date(); cutoff.setDate(cutoff.getDate() - days);
  return ALL_TXNS.filter(t => new Date(t.date) >= cutoff);
}}

function setPeriod(days) {{
  customFrom = null; customTo = null;
  currentDays = days;
  const rc = document.getElementById('range-clear'); if(rc) rc.style.display='none';
  const lbl = document.getElementById('cal-label'); if(lbl) lbl.textContent='Mois';
  const btn = document.getElementById('cal-btn'); if(btn) btn.style.borderColor='#333';
  document.getElementById('range-from').value = '';
  document.getElementById('range-to').value = '';
  document.querySelectorAll('.period-btn').forEach(b => b.classList.toggle('active', parseInt(b.dataset.days) === days));
  refreshAll();
}}

function toggleCal() {{
  const p = document.getElementById('cal-picker');
  if (p.style.display === 'none') {{
    p.style.display = 'block'; renderCalMonths();
    document.addEventListener('click', _calOutside, true);
  }} else {{
    p.style.display = 'none';
    document.removeEventListener('click', _calOutside, true);
  }}
}}
function _calOutside(e) {{
  const picker = document.getElementById('cal-picker');
  const btn    = document.getElementById('cal-btn');
  if (picker && !picker.contains(e.target) && !btn.contains(e.target)) {{
    picker.style.display = 'none';
    document.removeEventListener('click', _calOutside, true);
  }}
}}
function calShiftYear(d) {{ _calYear += d; renderCalMonths(); }}
function renderCalMonths() {{
  document.getElementById('cal-year-label').textContent = _calYear;
  document.getElementById('cal-months').innerHTML = MONTH_NAMES_SHORT.map((m,i) => {{
    const ym = `${{_calYear}}-${{String(i+1).padStart(2,'0')}}`;
    return `<button onclick="calMonth('${{ym}}')" style="background:#1a1a1a;border:1px solid #2a2a2a;border-radius:7px;padding:6px 4px;color:#bbb;font-family:'Geist',sans-serif;font-size:11px;cursor:pointer" onmouseover="this.style.background='#2563eb';this.style.color='#fff'" onmouseout="this.style.background='#1a1a1a';this.style.color='#bbb'">${{m}}</button>`;
  }}).join('');
}}
function calMonth(ym) {{
  const [y,m] = ym.split('-');
  customFrom = new Date(`${{ym}}-01`);
  customTo   = new Date(parseInt(y), parseInt(m), 0); customTo.setHours(23,59,59);
  document.getElementById('range-from').value = customFrom.toISOString().slice(0,10);
  document.getElementById('range-to').value   = customTo.toISOString().slice(0,10);
  document.getElementById('range-clear').style.display = 'block';
  document.getElementById('cal-label').textContent = MONTH_NAMES_SHORT[parseInt(m)-1] + ' ' + y;
  document.getElementById('cal-btn').style.borderColor = '#2563eb';
  document.getElementById('cal-picker').style.display = 'none';
  document.removeEventListener('click', _calOutside, true);
  document.querySelectorAll('.period-btn').forEach(b => b.classList.remove('active'));
  refreshAll();
}}
function applyRange() {{
  const from = document.getElementById('range-from').value;
  const to   = document.getElementById('range-to').value;
  if (!from || !to) return;
  customFrom = new Date(from + 'T00:00:00'); customTo = new Date(to + 'T23:59:59');
  document.getElementById('range-clear').style.display = 'block';
  document.getElementById('cal-label').textContent = from.slice(5) + ' → ' + to.slice(5);
  document.getElementById('cal-btn').style.borderColor = '#2563eb';
  document.querySelectorAll('.period-btn').forEach(b => b.classList.remove('active'));
  refreshAll();
}}
function clearRange() {{
  customFrom = null; customTo = null;
  document.getElementById('range-clear').style.display = 'none';
  document.getElementById('cal-label').textContent = 'Mois';
  document.getElementById('cal-btn').style.borderColor = '#333';
  document.getElementById('range-from').value = '';
  document.getElementById('range-to').value = '';
  setPeriod(30);
}}

// ── Monthly cards ─────────────────────────────────────────────────────────────
function renderMonthly(txns) {{
  const months = {{}};
  txns.forEach(t => {{
    const mk = t.date.substring(0,7);
    if (!months[mk]) months[mk] = {{in:0,out:0}};
    if (t.amount < 0) months[mk].in += Math.abs(t.amount);
    else months[mk].out += t.amount;
  }});
  const MN = {{'01':'Jan','02':'Fév','03':'Mar','04':'Avr','05':'Mai','06':'Jun','07':'Jul','08':'Aoû','09':'Sep','10':'Oct','11':'Nov','12':'Déc'}};
  const sorted = Object.entries(months).sort((a,b) => a[0].localeCompare(b[0]));
  document.getElementById('monthly-grid').innerHTML = sorted.map(([mk,d]) => {{
    const net = d.in - d.out;
    const nc  = net >= 0 ? '#4fc978' : '#f76e6e';
    const [yr,mo] = mk.split('-');
    const sr = d.in > 0 ? ((d.in - d.out) / d.in * 100) : 0;
    const srTxt = sr > 0 ? `${{sr.toFixed(0)}}%` : `${{sr.toFixed(0)}}%`;
    const srColor = sr >= 0 ? '#b8f566' : '#ff4444';
    return `<div class="month-card">
      <h3>${{MN[mo]}} ${{yr}}</h3>
      <div class="month-row"><span>Total IN</span><span class="green">+$${{d.in.toFixed(2)}}</span></div>
      <div class="month-row"><span>Total OUT</span><span class="red">-$${{d.out.toFixed(2)}}</span></div>
      <div class="month-row net-row"><span>NET</span><span style="color:${{nc}}">$${{net.toFixed(2)}}</span></div>
      <div class="month-row" style="margin-top:6px;border-top:1px solid #222;padding-top:6px"><span style="color:#555;font-size:11px">Taux d'épargne</span><span style="color:${{srColor}};font-family:'Geist Mono',monospace;font-size:12px;font-weight:700">${{srTxt}}</span></div>
    </div>`;
  }}).join('');
}}

// ── Donut ─────────────────────────────────────────────────────────────────────
let donutChart = null;
if (!window._donutHidden) window._donutHidden = new Set();
function renderDonut(txns) {{
  const cats = {{}};
  txns.filter(t => t.amount > 0).forEach(t => {{
    const cat = OVERRIDES[t.id] || t.category;
    cats[cat] = (cats[cat]||0) + t.amount;
  }});
  const skip = new Set(['Cash/Virements','Investissements','Autre']);
  const filtered = Object.entries(cats).filter(([k]) => !skip.has(k)).sort((a,b) => b[1]-a[1]);
  const labels = filtered.map(e => e[0]);
  const values = filtered.map(e => Math.round(e[1]*100)/100);
  const colors = labels.map(l => CAT_COLORS[l] || '#aaa');
  if (donutChart) donutChart.destroy();
  const ctx = document.getElementById('donutChart').getContext('2d');
  donutChart = new Chart(ctx, {{
    type: 'doughnut',
    data: {{ labels, datasets: [{{ data: values, backgroundColor: colors, borderWidth: 2, borderColor: '#111111', hoverOffset: 6 }}] }},
    options: {{ cutout: '70%', plugins: {{ legend: {{ display: false }}, tooltip: {{ callbacks: {{ label: c => ` ${{c.label}}: $${{c.parsed.toLocaleString('fr-CA',{{minimumFractionDigits:2}})}} CAD` }} }} }}, animation: {{ duration: 600, easing: 'easeInOutQuart' }} }}
  }});
  const total = values.reduce((a,b)=>a+b,0);
  document.getElementById('donutLegend').innerHTML = labels.map((lbl,i) => {{
    const pct = total > 0 ? (values[i]/total*100).toFixed(1) : '0.0';
    return `<button onclick="toggleDonutCat('${{lbl}}')" style="display:flex;flex-direction:column;align-items:flex-start;gap:2px;background:rgba(255,255,255,0.06);border:1px solid ${{colors[i]}}55;border-radius:10px;padding:8px 12px;cursor:pointer;text-align:left;transition:background 0.15s" onmouseover="this.style.background='rgba(255,255,255,0.1)'" onmouseout="this.style.background='rgba(255,255,255,0.06)'">
      <div style="display:flex;align-items:center;gap:6px"><span style="width:8px;height:8px;border-radius:50%;background:${{colors[i]}};flex-shrink:0"></span><span style="font-size:11px;font-weight:600;color:#f1f1f1;font-family:'Geist',sans-serif">${{lbl}}</span></div>
      <span style="font-size:12px;font-weight:700;color:${{colors[i]}};font-family:'Geist Mono',monospace;padding-left:14px;letter-spacing:-0.02em">${{pct}}%</span>
      <span style="font-size:10px;color:#aaa;font-family:'Geist Mono',monospace;padding-left:14px;letter-spacing:-0.01em">$${{values[i].toLocaleString('fr-CA',{{minimumFractionDigits:0}})}}</span>
    </button>`;
  }}).join('');
}}
function toggleDonutCat(lbl) {{
  if (window._donutHidden.has(lbl)) window._donutHidden.delete(lbl);
  else window._donutHidden.add(lbl);
  renderDonut(getFilteredTxns(currentDays));
}}

// ── Weekly chart ──────────────────────────────────────────────────────────────
let weeklyChart = null;
function renderWeekly(txns) {{
  const weeks = {{}};
  txns.forEach(t => {{
    const d = new Date(t.date); const day = d.getDay();
    const mon = new Date(d); mon.setDate(d.getDate() - ((day+6)%7));
    const key = mon.toISOString().substring(0,10);
    if (!weeks[key]) weeks[key] = 0;
    weeks[key] += t.amount < 0 ? Math.abs(t.amount) : -t.amount;
  }});
  const sorted = Object.entries(weeks).sort((a,b)=>a[0].localeCompare(b[0]));
  const wLabels = sorted.map(([k]) => {{ const d=new Date(k); return `${{d.getDate()}}/${{d.getMonth()+1}}`; }});
  const wNets   = sorted.map(e => Math.round(e[1]*100)/100);
  if (weeklyChart) weeklyChart.destroy();
  weeklyChart = new Chart(document.getElementById('weeklyChart').getContext('2d'), {{
    type: 'line',
    data: {{ labels: wLabels, datasets: [{{ label: 'Net (CAD)', data: wNets, borderColor: '#2563eb', backgroundColor: 'rgba(37,99,235,0.12)', pointBackgroundColor: wNets.map(v => v>=0?'#4fc978':'#f76e6e'), pointRadius: 4, pointHoverRadius: 7, fill: true, tension: 0.35, borderWidth: 2 }}] }},
    options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }}, tooltip: {{ callbacks: {{ label: c => ` Net: $${{c.parsed.y.toLocaleString('fr-CA',{{minimumFractionDigits:2}})}}` }} }} }}, scales: {{ x: {{ ticks: {{ color:'#666',font:{{size:10,family:'Geist'}},maxRotation:45,autoSkip:true,maxTicksLimit:14 }},grid:{{color:'rgba(255,255,255,0.04)'}} }}, y: {{ ticks: {{ color:'#666',font:{{size:10,family:'Geist'}},callback:v=>'$'+v.toLocaleString('fr-CA') }},grid:{{color:'rgba(255,255,255,0.06)'}} }} }} }}
  }});
}}

// ── Budget ────────────────────────────────────────────────────────────────────
const SKIP_CATS = new Set(['Cash/Virements','Investissements','Paiement propre']);
const CAT_ICONS = {{
  'Bouffe/Resto':'🍔','Épicerie':'🛒','Gaz':'⛽','Shopping':'🛍️',
  'Divertissement':'🎮','Gym':'💪','Télécom':'📱','Business/Tech':'💻',
  'Auto':'🚗','Moto':'🏍️','Santé':'💊','Cadeaux':'🎁',
  'Logement':'🏠','Autre':'💸',
}};

const getBudgetTargets = () => JSON.parse(localStorage.getItem('budgetTargets') || '{{}}');
const saveBudgetTargets = t => localStorage.setItem('budgetTargets', JSON.stringify(t));
const getCustomCats    = () => JSON.parse(localStorage.getItem('budgetCustomCats') || '[]');
const saveCustomCats   = c => localStorage.setItem('budgetCustomCats', JSON.stringify(c));
const getHiddenCats    = () => JSON.parse(localStorage.getItem('budgetHidden') || '[]');
const saveHiddenCats   = h => localStorage.setItem('budgetHidden', JSON.stringify(h));
const getCatOrder      = () => JSON.parse(localStorage.getItem('budgetOrder') || '[]');
const saveCatOrder     = o => localStorage.setItem('budgetOrder', JSON.stringify(o));

function getBudgetDividers() {{ return JSON.parse(localStorage.getItem('budgetDividers') || '{{}}'); }}
function saveBudgetDividers(d) {{ localStorage.setItem('budgetDividers', JSON.stringify(d)); }}

function addBudgetDivider() {{
  const name = prompt('Nom de la section (ex: Maison, Transport…)');
  if (!name || !name.trim()) return;
  const id = '__div__:' + name.trim();
  const divs = getBudgetDividers();
  divs[id] = name.trim();
  saveBudgetDividers(divs);
  // Inject into order at end
  const order = getCatOrder();
  if (!order.includes(id)) order.push(id);
  saveCatOrder(order);
  renderBudget(getFilteredTxns(currentDays));
}}

function deleteDivider(id) {{
  const divs = getBudgetDividers();
  delete divs[id];
  saveBudgetDividers(divs);
  const order = getCatOrder().filter(x => x !== id);
  saveCatOrder(order);
  renderBudget(getFilteredTxns(currentDays));
}}

function editTarget(cat) {{
  const targets = getBudgetTargets();
  const val = prompt(`Budget mensuel cible pour "${{cat}}" (CAD) :`, targets[cat] || '');
  if (val === null) return;
  if (val === '' || val === '0') {{ delete targets[cat]; }}
  else {{ const n = parseFloat(val.replace(/[^0-9.]/g,'')); if (!isNaN(n) && n > 0) targets[cat] = n; }}
  saveBudgetTargets(targets);
  renderBudget(getFilteredTxns(currentDays));
}}

function addCustomCat() {{
  const name = prompt('Nom de la nouvelle catégorie budget :');
  if (!name || !name.trim()) return;
  const amt = prompt(`Budget mensuel cible pour "${{name.trim()}}" (CAD) :`);
  const n   = parseFloat((amt||'').replace(/[^0-9.]/g,''));
  const cats = getCustomCats();
  if (!cats.includes(name.trim())) {{ cats.push(name.trim()); saveCustomCats(cats); }}
  if (!isNaN(n) && n > 0) {{ const t = getBudgetTargets(); t[name.trim()] = n; saveBudgetTargets(t); }}
  syncCatDropdown();  // sync dropdown transactions
  renderBudget(getFilteredTxns(currentDays));
}}

function restoreHidden() {{ saveHiddenCats([]); renderBudget(getFilteredTxns(currentDays)); }}
function hideCatRow(cat) {{ saveHiddenCats([...getHiddenCats(), cat]); renderBudget(getFilteredTxns(currentDays)); }}
function restoreDeleted() {{ saveDeletedCats(new Set()); renderBudget(getFilteredTxns(currentDays)); syncCatDropdown(); }}

// Drag-and-drop
let dragSrc = null;
function onDragStart(e) {{ dragSrc = e.currentTarget; e.dataTransfer.effectAllowed = 'move'; e.currentTarget.style.opacity = '0.4'; }}
function onDragEnd(e)   {{ e.currentTarget.style.opacity = '1'; document.querySelectorAll('.budget-row').forEach(r => r.classList.remove('drag-over')); }}
function onDragOver(e)  {{ e.preventDefault(); e.dataTransfer.dropEffect = 'move'; document.querySelectorAll('.budget-row').forEach(r => r.classList.remove('drag-over')); e.currentTarget.classList.add('drag-over'); }}
function onDrop(e) {{
  e.preventDefault();
  if (dragSrc === e.currentTarget) return;
  const container = document.getElementById('budget-bars');
  const rows = [...container.querySelectorAll('.budget-row')];
  const fi = rows.indexOf(dragSrc), ti = rows.indexOf(e.currentTarget);
  if (fi < 0 || ti < 0) return;
  if (fi < ti) e.currentTarget.after(dragSrc); else e.currentTarget.before(dragSrc);
  saveCatOrder([...container.querySelectorAll('.budget-row')].map(r => r.dataset.cat));
  e.currentTarget.classList.remove('drag-over');
}}

function renderBudget(txns) {{
  const targets    = getBudgetTargets();
  const customCats = getCustomCats();
  const hidden     = getHiddenCats();
  const savedOrder = getCatOrder();
  const dividers   = getBudgetDividers();

  // Avg monthly par cat sur toutes les données
  const allMonths = {{}};
  ALL_TXNS.filter(t => t.amount > 0 && !SKIP_CATS.has(OVERRIDES[t.id] || t.category) && !SELF_PAY_SET.has(t.id)).forEach(t => {{
    const mk = t.date.substring(0,7); const cat = OVERRIDES[t.id] || t.category;
    if (!allMonths[mk]) allMonths[mk] = {{}};
    allMonths[mk][cat] = (allMonths[mk][cat]||0) + t.amount;
  }});
  const numMonths = Math.max(Object.keys(allMonths).length, 1);
  const avgMonthly = {{}};
  Object.values(allMonths).forEach(m => Object.entries(m).forEach(([c,a]) => {{ avgMonthly[c] = (avgMonthly[c]||0) + a; }}));
  Object.keys(avgMonthly).forEach(k => avgMonthly[k] /= numMonths);

  // Dépenses période filtrée
  const spent = {{}};
  txns.filter(t => t.amount > 0 && !SKIP_CATS.has(OVERRIDES[t.id] || t.category) && !SELF_PAY_SET.has(t.id)).forEach(t => {{
    const cat = OVERRIDES[t.id] || t.category;
    spent[cat] = (spent[cat]||0) + t.amount;
  }});

  const ratio = Math.min(currentDays, 90) / 30;
  let allCats = [...new Set([...Object.keys(spent), ...customCats])].filter(c => !hidden.includes(c));

  if (savedOrder.length > 0) {{
    const om = {{}}; savedOrder.forEach((c,i) => om[c]=i);
    allCats.sort((a,b) => {{ const ia=om[a]??9999, ib=om[b]??9999; return ia!==ib ? ia-ib : (spent[b]||0)-(spent[a]||0); }});
  }} else {{
    allCats.sort((a,b) => (spent[b]||0)-(spent[a]||0));
  }}

  // Build full ordered list including dividers
  let fullList;
  if (savedOrder.length > 0) {{
    const catSet = new Set(allCats);
    const divSet = new Set(Object.keys(dividers));
    fullList = savedOrder.filter(id => catSet.has(id) || divSet.has(id));
    // append any cats not yet in savedOrder
    allCats.forEach(c => {{ if (!savedOrder.includes(c)) fullList.push(c); }});
  }} else {{
    fullList = allCats;
  }}

  const bars = fullList.map(id => {{
    // Divider
    if (id.startsWith('__div__:')) {{
      const label = dividers[id] || id.replace('__div__:','');
      return `
      <div class="budget-divider budget-row" data-cat="${{id}}" draggable="true"
        ondragstart="onDragStart(event)" ondragend="onDragEnd(event)"
        ondragover="onDragOver(event)" ondrop="onDrop(event)">
        <span class="bdiv-drag">⠿</span>
        <span class="bdiv-label">${{label}}</span>
        <div class="bdiv-line"></div>
        <button class="bdiv-del" onclick="deleteDivider('${{id}}')">✕</button>
      </div>`;
    }}
    const cat = id;
    const s       = spent[cat]||0;
    const monthly = targets[cat] || avgMonthly[cat] || 0;
    const b       = monthly * ratio;
    const pct     = b > 0 ? Math.min((s/b)*100, 100) : (s>0?100:0);
    const over    = b > 0 && s > b;
    const overAmt = over ? s-b : 0;
    const color   = pct < 60 ? 'var(--lime)' : pct < 85 ? '#f59e0b' : '#ff4444';
    const icon    = CAT_ICONS[cat] || '💳';
    const proj    = currentDays < 30 && s > 0 ? (s/currentDays*30) : null;
    const hasTarget = !!targets[cat];
    const targetTxt = hasTarget ? `$${{monthly.toFixed(0)}}` : `~$${{monthly.toFixed(0)}}`;
    const overTxt   = over ? `+$${{overAmt.toFixed(0)}}` : (proj && !over ? `→$${{proj.toFixed(0)}}` : '');
    return `
    <div class="budget-row" data-cat="${{cat}}" draggable="true"
      ondragstart="onDragStart(event)" ondragend="onDragEnd(event)"
      ondragover="onDragOver(event)" ondrop="onDrop(event)">
      <span class="brow-drag">⠿</span>
      <span class="brow-icon">${{icon}}</span>
      <div class="brow-body">
        <div class="brow-name">${{cat}}</div>
        <div class="brow-track"><div class="brow-fill" style="width:0%;background:${{color}}" data-pct="${{pct}}"></div></div>
      </div>
      <div class="brow-right">
        <span class="brow-spent" style="color:${{color}}">$${{s.toFixed(0)}}</span>
        <span class="brow-target" onclick="editTarget('${{cat}}')">${{targetTxt}}</span>
        ${{overTxt ? `<span class="brow-over">${{overTxt}}</span>` : ''}}
      </div>
      <div style="display:flex;gap:4px;flex-shrink:0">
        <button class="brow-del" onclick="hideCatRow('${{cat}}')" title="Cacher">◌</button>
        <button class="brow-del" onclick="deleteCatFull('${{cat}}')">✕</button>
      </div>
    </div>`;
  }}).join('');

  document.getElementById('budget-bars').innerHTML = bars || '<p style="color:#555">Aucune dépense dans cette période.</p>';
  // Animate bars in
  requestAnimationFrame(() => requestAnimationFrame(() => {{
    document.querySelectorAll('.brow-fill[data-pct]').forEach(el => {{
      el.style.width = el.dataset.pct + '%';
    }});
  }}));

  // Restaurer cachées
  let restore = document.getElementById('budget-restore');
  if (!restore) {{ restore = document.createElement('div'); restore.id='budget-restore'; document.getElementById('budget-bars').parentElement.appendChild(restore); }}
  const hc = hidden.length;
  restore.innerHTML = hc > 0 ? `<button onclick="restoreHidden()" style="background:none;border:none;color:#444;font-size:12px;font-family:'Geist',sans-serif;cursor:pointer;margin-top:8px">↩ Restaurer ${{hc}} catégorie${{hc>1?'s':''}} masquée${{hc>1?'s':''}}</button>` : '';

  document.getElementById('budget-title').textContent = `Budget · ${{currentDays >= 9999 ? 'Tout' : currentDays + ' jours'}}`;

  // ── Gamification ─────────────────────────────────────────────────────────
  let suggEl = document.getElementById('budget-suggestions');
  if (!suggEl) {{ suggEl = document.createElement('div'); suggEl.id='budget-suggestions'; document.getElementById('budget-bars').parentElement.appendChild(suggEl); }}

  const savedIncome = parseFloat(localStorage.getItem('finance_monthly_income')||'0');

  const benchmarks = {{'Épicerie':0.15,'Bouffe/Resto':0.10,'Gaz':0.12,'Shopping':0.05,'Télécom':0.05,'Divertissement':0.05,'Santé':0.08,'Gym':0.05}};

  const catsWithTarget = Object.entries(targets).filter(([,v])=>v>0);
  let scorePoints=0, maxPoints=0;
  const catResults={{}};
  for (const [cat,tgt] of catsWithTarget) {{
    maxPoints += 20;
    const sMon = ratio > 0 ? (spent[cat]||0)/ratio : (spent[cat]||0);
    const p2   = sMon/tgt;
    let pts, status;
    if      (p2<=0.75) {{ pts=20; status='ace'; }}
    else if (p2<=1.00) {{ pts=14; status='ok'; }}
    else if (p2<=1.20) {{ pts=5;  status='over'; }}
    else               {{ pts=0;  status='fail'; }}
    scorePoints += pts; catResults[cat] = {{pct:p2, status}};
  }}

  let spendRatio=null, spentMon=null;
  if (savedIncome > 0) {{
    const totalSpent = Object.entries(spent).filter(([c])=>c!=='Investissements').reduce((s,[,v])=>s+v,0);
    spentMon   = ratio > 0 ? totalSpent/ratio : totalSpent;
    spendRatio = spentMon/savedIncome;
    maxPoints += 20;
    if      (spendRatio<=0.55) scorePoints+=20;
    else if (spendRatio<=0.65) scorePoints+=16;
    else if (spendRatio<=0.70) scorePoints+=12;
    else if (spendRatio<=0.80) scorePoints+=6;
  }}

  const score = maxPoints > 0 ? Math.round((scorePoints/maxPoints)*100) : null;
  const catsOk = Object.values(catResults).filter(r=>r.status==='ace'||r.status==='ok').length;

  let grade, gc, rl, rc2;
  if      (score===null)  {{ grade='—'; gc='#444';    rl='#222';    rc2='Fixe tes cibles budget'; }}
  else if (score>=90)     {{ grade='S'; gc='#f0c040'; rl='#f0c040'; rc2='Légendaire'; }}
  else if (score>=80)     {{ grade='A'; gc='#4fc978'; rl='#4fc978'; rc2='Excellent'; }}
  else if (score>=65)     {{ grade='B'; gc='#93c5fd'; rl='#93c5fd'; rc2='Dans le game'; }}
  else if (score>=50)     {{ grade='C'; gc='#f59e0b'; rl='#f59e0b'; rc2='À surveiller'; }}
  else if (score>=30)     {{ grade='D'; gc='#fb923c'; rl='#fb923c'; rc2='Danger zone'; }}
  else                    {{ grade='F'; gc='#f76e6e'; rl='#f76e6e'; rc2='Budget explosé'; }}

  const R=72, CIRC=2*Math.PI*R;
  const fillOff = score !== null ? CIRC*(1-score/100) : CIRC;

  const suggs = [];
  if (savedIncome > 0 && spendRatio !== null) {{
    if (spendRatio > 0.70) {{
      const ov = spentMon - savedIncome*0.70;
      suggs.push({{type:'warning', msg:`Tu dépenses ${{Math.round(spendRatio*100)}}% de ton revenu (idéal ≤70%).`, impact:`Réduire de $${{ov.toFixed(0)}} libèrerait du cash.`}});
    }} else if (spendRatio < 0.50) {{
      suggs.push({{type:'good', msg:`Seulement ${{Math.round(spendRatio*100)}}% du revenu en dépenses.`}});
    }}
    for (const [cat, idealPct] of Object.entries(benchmarks)) {{
      const s = spent[cat]||0; if (!s) continue;
      const sMon2 = ratio>0?s/ratio:s;
      const pct2  = sMon2/savedIncome;
      if (pct2 > idealPct) {{
        const ov = sMon2 - savedIncome*idealPct;
        suggs.push({{type:'warning', msg:`${{cat}} : ${{Math.round(pct2*100)}}% du revenu (idéal ≤${{Math.round(idealPct*100)}}%).`, impact:`Cibler $${{(savedIncome*idealPct).toFixed(0)}}/mois — économies $${{ov.toFixed(0)}}.`}});
      }}
    }}
    const sr = 1 - spendRatio;
    if      (sr < 0.10) suggs.push({{type:'tip', msg:`Épargne sous 10%.`, impact:`Cible 20% → $${{(savedIncome*0.20).toFixed(0)}}/mois.`}});
    else if (sr >= 0.20) suggs.push({{type:'good', msg:`Taux d'épargne ${{Math.round(sr*100)}}% — au-dessus de la cible.`}});
  }} else if (!savedIncome && catsWithTarget.length > 0) {{
    for (const [cat, res] of Object.entries(catResults)) {{
      if (res.status==='fail'||res.status==='over') {{
        const s = ratio>0?(spent[cat]||0)/ratio:(spent[cat]||0);
        suggs.push({{type:'warning', msg:`${{cat}} dépasse la cible de $${{(s-(targets[cat]||0)).toFixed(0)}}.`, impact:`Cible : $${{(targets[cat]||0).toFixed(0)}}/mois`}});
      }}
    }}
  }}
  if (suggs.length===0 && (savedIncome>0||catsWithTarget.length>0)) suggs.push({{type:'good', msg:'Toutes les cibles sont respectées. Continue.'}});

  const suggHTML = suggs.slice(0,3).map(s => {{
    const clr  = s.type==='warning'?'var(--red)':s.type==='good'?'var(--emerald)':'#818cf8';
    const dot  = s.type==='warning'?'●':s.type==='good'?'●':'●';
    return `<div style="display:flex;align-items:flex-start;gap:8px;padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.03)">
      <span style="color:${{clr}};font-size:7px;margin-top:4px;flex-shrink:0">${{dot}}</span>
      <div>
        <span style="font-size:11.5px;color:var(--text)">${{s.msg}}</span>
        ${{s.impact ? `<span style="font-size:11px;color:var(--muted2);margin-left:6px">${{s.impact}}</span>` : ''}}
      </div>
    </div>`;
  }}).join('');

  const badgesHTML = catsWithTarget.length > 0 ? `<div style="display:flex;flex-wrap:wrap;gap:5px;margin-top:12px">${{
    Object.entries(catResults).map(([cat,res]) => {{
      const c  = res.status==='ace'?'var(--emerald)':res.status==='ok'?'#818cf8':res.status==='over'?'#f59e0b':'var(--red)';
      const ic = res.status==='ace'?'✓':res.status==='ok'?'✓':res.status==='over'?'↑':'✕';
      return `<span style="display:inline-flex;align-items:center;gap:3px;font-size:10px;font-weight:500;padding:3px 8px;border-radius:20px;background:rgba(255,255,255,0.04);color:${{c}};border:1px solid rgba(255,255,255,0.06)">${{ic}} ${{cat}}</span>`;
    }}).join('')
  }}</div>` : '';

  // ── Investissements par mois ───────────────────────────────────────────────
  const investByMonth = {{}};
  ALL_TXNS.filter(t => (OVERRIDES[t.id]||t.category) === 'Investissements' && t.amount > 0 && !SELF_PAY_SET.has(t.id))
    .forEach(t => {{
      const mk = t.date.substring(0,7);
      investByMonth[mk] = (investByMonth[mk]||0) + t.amount;
    }});
  const investMonths = Object.entries(investByMonth).sort((a,b)=>b[0].localeCompare(a[0])).slice(0,6);
  const investAvg = investMonths.length > 0
    ? investMonths.reduce((s,[,v])=>s+v,0) / investMonths.length : 0;

  const investHTML = investMonths.length > 0 ? `
  <div style="margin-top:16px;padding-top:14px;border-top:1px solid var(--border)">
    <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:10px">
      <span style="font-size:10px;font-weight:500;letter-spacing:0.18em;text-transform:uppercase;color:var(--muted)">Investissements</span>
      <span style="font-family:var(--mono);font-size:10px;color:var(--muted2)">moy. $${{investAvg.toFixed(0)}}/mois</span>
    </div>
    ${{investMonths.map(([mk, amt]) => {{
      const label = new Date(mk+'-01').toLocaleDateString('fr-CA',{{month:'short',year:'2-digit'}});
      const maxAmt = Math.max(...investMonths.map(([,v])=>v));
      const pct = maxAmt > 0 ? (amt/maxAmt*100) : 0;
      const clr = amt >= investAvg ? 'var(--emerald)' : 'var(--muted2)';
      return `<div style="display:flex;align-items:center;gap:8px;margin-bottom:5px">
        <span style="font-size:10px;color:var(--muted);font-family:var(--mono);width:40px;flex-shrink:0">${{label}}</span>
        <div style="flex:1;height:2px;background:rgba(255,255,255,0.05);border-radius:2px;overflow:hidden">
          <div style="width:${{pct}}%;height:100%;background:${{clr}};border-radius:2px;transition:width .5s cubic-bezier(0.16,1,0.3,1)"></div>
        </div>
        <span style="font-family:var(--mono);font-size:11px;font-weight:600;color:${{clr}};width:52px;text-align:right;flex-shrink:0">$${{amt.toFixed(0)}}</span>
      </div>`;
    }}).join('')}}
  </div>` : '';

  suggEl.innerHTML = `
    <div style="margin-bottom:20px">
      <div style="display:flex;align-items:center;gap:14px;margin-bottom:16px">
        <div style="position:relative;width:72px;height:72px;flex-shrink:0">
          <svg width="72" height="72" viewBox="0 0 72 72" style="transform:rotate(-90deg)">
            <circle cx="36" cy="36" r="30" fill="none" stroke="rgba(255,255,255,0.05)" stroke-width="5"/>
            <circle cx="36" cy="36" r="30" fill="none" stroke="${{rl}}" stroke-width="5"
              stroke-dasharray="${{(2*Math.PI*30).toFixed(1)}}" stroke-dashoffset="${{(2*Math.PI*30*(1-(score||0)/100)).toFixed(1)}}"
              stroke-linecap="round" style="transition:stroke-dashoffset .9s cubic-bezier(.4,0,.2,1);opacity:0.85"/>
          </svg>
          <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center">
            <span style="font-family:var(--mono);font-size:22px;font-weight:700;color:${{gc}}">${{grade}}</span>
          </div>
        </div>
        <div>
          <div style="font-size:15px;font-weight:600;color:${{gc}};margin-bottom:3px">${{rc2}}</div>
          <div style="font-size:11px;color:var(--muted)">${{catsWithTarget.length>0 ? `${{catsOk}} / ${{catsWithTarget.length}} objectifs` : 'Fixe des cibles pour scorer'}}</div>
        </div>
      </div>

      ${{badgesHTML}}

      <div style="margin-top:16px;padding-top:14px;border-top:1px solid var(--border);display:flex;align-items:center;gap:8px">
        <span style="font-size:11px;color:var(--muted);flex:1">Revenu net / mois</span>
        <input id="income-input" type="number" value="${{savedIncome||''}}" placeholder="4500"
          oninput="(function(v){{const n=parseFloat(v)||0;localStorage.setItem('finance_monthly_income',n);renderBudget(getFilteredTxns(currentDays));}})(this.value)"
          style="width:64px;background:transparent;border:none;font-family:var(--mono);font-size:13px;font-weight:600;color:var(--text);outline:none;text-align:right"/>
        <span style="font-size:11px;color:var(--muted)">CAD</span>
      </div>
    </div>

    ${{investHTML}}

    ${{suggs.length>0 ? `
    <div style="border-top:1px solid var(--border);padding-top:14px">
      <div style="font-size:10px;font-weight:500;letter-spacing:0.18em;text-transform:uppercase;color:var(--muted);margin-bottom:10px">Insights</div>
      <div style="display:flex;flex-direction:column;gap:0">
        ${{suggs.slice(0,4).map(s => {{
          const clr = s.type==='warning'?'var(--red)':s.type==='good'?'var(--emerald)':'#818cf8';
          return `<div style="padding:9px 0;border-bottom:1px solid rgba(255,255,255,0.03);display:flex;gap:8px;align-items:flex-start">
            <span style="width:5px;height:5px;border-radius:50%;background:${{clr}};flex-shrink:0;margin-top:4px"></span>
            <div>
              <div style="font-size:11.5px;color:var(--text);line-height:1.4">${{s.msg}}</div>
              ${{s.impact ? `<div style="font-size:10.5px;color:var(--muted2);margin-top:2px">${{s.impact}}</div>` : ''}}
            </div>
          </div>`;
        }}).join('')}}
      </div>
    </div>` : ''}}
  `;
}}

// ── Transactions tab ──────────────────────────────────────────────────────────
// ── Source de vérité unique pour les catégories ───────────────────────────────
const getDeletedCats = () => new Set(JSON.parse(localStorage.getItem('deletedCats') || '[]'));
const saveDeletedCats = s => localStorage.setItem('deletedCats', JSON.stringify([...s]));

function getAllCats() {{
  const deleted  = getDeletedCats();
  const fromTxns = ALL_TXNS.map(t => OVERRIDES[t.id] || t.category).filter(c => !deleted.has(c));
  const fromCustom = getCustomCats().filter(c => !deleted.has(c));
  return [...new Set([...fromTxns, ...fromCustom])].sort();
}}

function deleteCatFull(cat) {{
  if (!confirm(`Supprimer la catégorie "${{cat}}" ?\n\nToutes les transactions dans cette catégorie seront réassignées à "Autre".`)) return;
  // Réassigner les overrides de cette cat → "Autre"
  let changed = false;
  ALL_TXNS.forEach(t => {{
    if ((OVERRIDES[t.id] || t.category) === cat) {{
      OVERRIDES[t.id] = 'Autre';
      changed = true;
    }}
  }});
  if (changed) localStorage.setItem('catOverrides', JSON.stringify(OVERRIDES));
  // Retirer des custom cats
  const custom = getCustomCats().filter(c => c !== cat);
  saveCustomCats(custom);
  // Ajouter aux supprimées (pour cacher les cats built-in)
  const deleted = getDeletedCats();
  deleted.add(cat);
  saveDeletedCats(deleted);
  // Retirer des cachées (plus nécessaire)
  const hidden = getHiddenCats().filter(c => c !== cat);
  saveHiddenCats(hidden);
  syncCatDropdown();
  renderBudget(getFilteredTxns(currentDays));
  if (initializedTabs.has('txns')) renderTxns(
    document.getElementById('txn-search')?.value||'',
    document.getElementById('txn-acct')?.value||'',
    document.getElementById('txn-cat')?.value||''
  );
}}

function syncCatDropdown() {{
  const sel     = document.getElementById('txn-cat');
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">Toutes catégories</option>';
  getAllCats().forEach(c => {{
    const o = document.createElement('option');
    o.value = c; o.textContent = c;
    if (c === current) o.selected = true;
    sel.appendChild(o);
  }});
}}

function initTxns() {{
  const acctSel = document.getElementById('txn-acct');
  const accts   = [...new Set(ALL_TXNS.map(t => t.account))];
  accts.forEach(a => {{ const o=document.createElement('option'); o.value=a; o.textContent=getAccName(a); acctSel.appendChild(o); }});
  syncCatDropdown();
  renderAccLegend();
  renderTxns('','','');
  renderRules();
  renderAbonnements();
}}

function renderAccLegend() {{
  const el = document.getElementById('acc-legend');
  if (!el) return;
  const accts  = [...new Set(ALL_TXNS.map(t => t.account))];
  const colors = getAccColors();
  el.innerHTML = accts.map(a => {{
    const color = colors[a] || '#777';
    const name  = getAccName(a);
    return `<span class="acc-legend-item" title="Cliquer pour changer la couleur" onclick="pickAccColor('${{a}}',this)">
      <span class="acc-legend-dot" id="dot-${{a}}" style="background:${{color}}"></span>
      <span>${{name}}</span>
    </span>`;
  }}).join('');
}}

function pickAccColor(accId, el) {{
  const inp = document.createElement('input');
  inp.type = 'color';
  const colors = getAccColors();
  inp.value = colors[accId] || '#777777';
  inp.style.cssText = 'position:absolute;width:0;height:0;opacity:0';
  el.appendChild(inp);
  inp.click();
  inp.addEventListener('input', () => {{
    setAccColor(accId, inp.value);
    renderAccLegend();
    renderTxns(
      document.getElementById('txn-search')?.value||'',
      document.getElementById('txn-acct')?.value||'',
      document.getElementById('txn-cat')?.value||''
    );
  }});
  inp.addEventListener('change', () => {{ inp.remove(); }});
  inp.addEventListener('blur',   () => {{ inp.remove(); }});
}}

function renderTxns(filter='', acct='', cat='') {{
  const q      = filter.toLowerCase();
  const pool   = getFilteredTxns(currentDays);
  const colors = getAccColors();
  const rows   = pool.filter(t => {{
    const ec = OVERRIDES[t.id]||t.category;
    const en = NAMES[t.id]||t.name;
    if (acct && t.account !== acct) return false;
    if (cat  && ec !== cat)          return false;
    if (q && !en.toLowerCase().includes(q) && !ec.toLowerCase().includes(q)) return false;
    return true;
  }});
  document.getElementById('txn-tbody').innerHTML = rows.map(t => {{
    const ec       = OVERRIDES[t.id]||t.category;
    const en       = NAMES[t.id]||t.name;
    const isSelf   = SELF_PAY_SET.has(t.id);
    const cls      = isSelf ? '' : (t.amount < 0 ? 'green' : 'red');
    const amt      = t.amount < 0 ? `+$${{Math.abs(t.amount).toFixed(2)}}` : `-$${{t.amount.toFixed(2)}}`;
    const ns       = NAMES[t.id] ? 'color:#93c5fd;' : '';
    const dotColor = colors[t.account] || '#777';
    const accName  = getAccName(t.account);
    const selfStyle= isSelf ? 'opacity:0.45;text-decoration:line-through;' : '';
    const selfTip  = isSelf ? 'Marqué comme paiement propre (exclu du budget) — cliquer pour annuler' : 'Marquer comme paiement propre (ex: virement à soi-même)';
    const txnDocs = getTxnDocs(t.id);
    const hasDoc  = txnDocs.length > 0;
    const docIcon = hasDoc
      ? `<span style="color:#b8f566;font-size:1rem;cursor:pointer" title="${{txnDocs.length}} doc${{txnDocs.length>1?'s':''}} attaché${{txnDocs.length>1?'s':''}}" onclick="openDocModal('${{t.id}}','${{en.replace(/'/g,"\\\\'")}}')"">📎</span>`
      : `<span style="color:#333;font-size:1rem;cursor:pointer" title="Attacher un document" onclick="openDocModal('${{t.id}}','${{en.replace(/'/g,"\\\\'")}}')"">📎</span>`;
    return `<tr style="${{selfStyle}}">
      <td>${{t.date}}</td>
      <td><span onclick="editName('${{t.id}}',this)" style="cursor:pointer;${{ns}}" title="Cliquer pour renommer">${{en}}</span></td>
      <td>
        <span class="badge" onclick="editCat('${{t.id}}','${{ec}}',this)">${{ec}}</span>
        <span onclick="toggleSelfPay('${{t.id}}')" title="${{selfTip}}" style="cursor:pointer;font-size:0.7rem;margin-left:4px;opacity:${{isSelf?1:0.3}};transition:opacity 0.2s" onmouseover="this.style.opacity=1" onmouseout="this.style.opacity='${{isSelf?1:0.3}}'">🔄</span>
      </td>
      <td>
        <span style="display:inline-flex;align-items:center;gap:5px;font-size:0.8rem;color:#aaa">
          <span style="width:8px;height:8px;border-radius:50%;background:${{dotColor}};display:inline-block;flex-shrink:0"></span>
          ${{accName}}
        </span>
      </td>
      <td class="${{cls}}" style="text-align:right;${{isSelf?'color:#555;':''}}">
        ${{isSelf ? `<span style="font-size:0.7rem;color:#555">🔄 propre</span>` : amt}}
      </td>
      <td style="text-align:center">${{docIcon}}</td>
    </tr>`;
  }}).join('');
  document.getElementById('txn-count').textContent = `${{rows.length}} transactions`;
}}

// ─── Documents par transaction ───────────────────────────────────────────────

let _docTxnId   = null;
let _docTxnName = '';

function getTxnDocs(txnId) {{
  try {{ return JSON.parse(localStorage.getItem('txnDocs_'+txnId)||'[]'); }}
  catch {{ return []; }}
}}

function saveTxnDocs(txnId, docs) {{
  if (docs.length === 0) localStorage.removeItem('txnDocs_'+txnId);
  else localStorage.setItem('txnDocs_'+txnId, JSON.stringify(docs));
}}

function openDocModal(txnId, txnName) {{
  _docTxnId   = txnId;
  _docTxnName = txnName;
  document.getElementById('doc-modal-title').textContent = '📎 Documents — ' + txnName;
  const docs = getTxnDocs(txnId);
  document.getElementById('doc-modal-sub').textContent =
    docs.length === 0 ? 'Aucun document attaché' : docs.length + ' document' + (docs.length>1?'s':'') + ' attaché' + (docs.length>1?'s':'');
  renderDocList();
  document.getElementById('doc-modal-overlay').style.display = 'flex';
  document.body.style.overflow = 'hidden';
}}

function closeDocModal() {{
  document.getElementById('doc-modal-overlay').style.display = 'none';
  document.body.style.overflow = '';
  _docTxnId = null;
  // Re-render pour mettre à jour l'icône 📎 dans la table
  renderTxns(
    document.getElementById('txn-search')?.value||'',
    document.getElementById('txn-acct')?.value||'',
    document.getElementById('txn-cat')?.value||''
  );
}}

function renderDocList() {{
  if (!_docTxnId) return;
  const docs = getTxnDocs(_docTxnId);
  const el = document.getElementById('doc-list');
  document.getElementById('doc-modal-sub').textContent =
    docs.length === 0 ? 'Aucun document attaché' : docs.length + ' document' + (docs.length>1?'s':'') + ' attaché' + (docs.length>1?'s':'');
  if (docs.length === 0) {{ el.innerHTML = ''; return; }}
  el.innerHTML = docs.map((d, i) => {{
    const isPdf = d.mime === 'application/pdf' || d.name.toLowerCase().endsWith('.pdf');
    const thumb = isPdf
      ? `<div class="doc-item-thumb pdf-thumb">📄</div>`
      : `<img src="${{d.data}}" class="doc-item-thumb" onclick="previewDoc(${{i}})" alt="${{d.name}}">`;
    const sizeLabel = d.size < 1024*1024
      ? (d.size/1024).toFixed(0)+' KB'
      : (d.size/1024/1024).toFixed(1)+' MB';
    return `<div class="doc-item">
      ${{thumb}}
      <div class="doc-item-info">
        <div class="doc-item-name" title="${{d.name}}">${{d.name}}</div>
        <div class="doc-item-meta">${{sizeLabel}} · ${{d.addedAt||''}}</div>
      </div>
      ${{isPdf ? `<a href="${{d.data}}" download="${{d.name}}" class="doc-btn" style="flex:none;padding:6px 10px;font-size:0.75rem;text-decoration:none">⬇</a>` : ''}}
      <button class="doc-item-del" onclick="deleteDoc(${{i}})" title="Supprimer">✕</button>
    </div>`;
  }}).join('');
}}

function handleDocDrop(e) {{
  e.preventDefault();
  document.getElementById('doc-drop-zone').style.borderColor = '';
  if (e.dataTransfer?.files?.length) handleDocFiles(e.dataTransfer.files);
}}

function handleDocFiles(files) {{
  if (!_docTxnId || !files?.length) return;
  const docs = getTxnDocs(_docTxnId);
  let pending = files.length;
  Array.from(files).forEach(file => {{
    if (file.size > 10*1024*1024) {{
      showToast('Fichier trop grand (max 10 MB) : ' + file.name);
      pending--; return;
    }}
    const reader = new FileReader();
    reader.onload = ev => {{
      docs.push({{
        name    : file.name,
        mime    : file.type,
        size    : file.size,
        data    : ev.target.result,
        addedAt : new Date().toLocaleDateString('fr-CA')
      }});
      pending--;
      if (pending === 0) {{
        saveTxnDocs(_docTxnId, docs);
        renderDocList();
        showToast('✓ ' + files.length + ' document' + (files.length>1?'s':'') + ' ajouté' + (files.length>1?'s':''));
      }}
    }};
    reader.readAsDataURL(file);
  }});
  // Reset input
  document.getElementById('doc-file-input').value   = '';
  document.getElementById('doc-camera-input').value = '';
}}

function deleteDoc(idx) {{
  if (!_docTxnId) return;
  const docs = getTxnDocs(_docTxnId);
  docs.splice(idx, 1);
  saveTxnDocs(_docTxnId, docs);
  renderDocList();
}}

function previewDoc(idx) {{
  if (!_docTxnId) return;
  const docs = getTxnDocs(_docTxnId);
  const d = docs[idx];
  if (!d) return;
  const ov = document.createElement('div');
  ov.className = 'doc-preview-overlay';
  ov.onclick = () => ov.remove();
  const img = document.createElement('img');
  img.src = d.data;
  img.alt = d.name;
  ov.appendChild(img);
  document.body.appendChild(ov);
}}

function editName(id, el) {{
  const current = NAMES[id] || ALL_TXNS.find(t=>t.id===id)?.name || '';
  const inp = document.createElement('input');
  inp.value = current;
  inp.style.cssText = 'background:#111;border:1px solid #2563eb;border-radius:4px;padding:2px 6px;color:#f1f1f1;font-family:Geist,sans-serif;font-size:13px;width:200px';
  el.replaceWith(inp); inp.focus(); inp.select();
  function commit() {{
    const val = inp.value.trim();
    if (val && val !== current) {{ NAMES[id]=val; localStorage.setItem('nameOverrides',JSON.stringify(NAMES)); }}
    renderTxns(document.getElementById('txn-search')?.value||'',document.getElementById('txn-acct')?.value||'',document.getElementById('txn-cat')?.value||'');
  }}
  inp.addEventListener('keydown', e => {{ e.stopPropagation(); if(e.key==='Enter') commit(); if(e.key==='Escape') commit(); }});
  inp.addEventListener('blur', commit);
}}

function editCat(id, currentCat, el) {{
  const allCats = getAllCats();
  const sel = document.createElement('select');
  sel.style.cssText = 'background:#111;border:1px solid #2563eb;border-radius:4px;padding:2px 6px;color:#93c5fd;font-family:Geist,sans-serif;font-size:11px;font-weight:600';

  // Option spéciale "+ Nouvelle catégorie"
  const newOpt = document.createElement('option');
  newOpt.value = '__new__'; newOpt.textContent = '+ Nouvelle catégorie';
  sel.appendChild(newOpt);

  // Option "🗑 Supprimer cette catégorie"
  const delOpt = document.createElement('option');
  delOpt.value = '__delete__'; delOpt.textContent = '🗑 Supprimer cette catégorie';
  sel.appendChild(delOpt);

  // Séparateur
  const sep = document.createElement('option'); sep.disabled = true; sep.textContent = '──────────'; sel.appendChild(sep);

  allCats.forEach(c => {{
    const o = document.createElement('option');
    o.value = c; o.textContent = c;
    if (c === currentCat) o.selected = true;
    sel.appendChild(o);
  }});

  el.replaceWith(sel); sel.focus();
  let committed = false;

  function applyNewCat(val) {{
    if (val === currentCat) {{
      renderTxns(document.getElementById('txn-search')?.value||'', document.getElementById('txn-acct')?.value||'', document.getElementById('txn-cat')?.value||'');
      return;
    }}
    // Ajouter la nouvelle cat aux custom cats si pas déjà présente
    const custom = getCustomCats();
    if (!getAllCats().includes(val)) {{
      custom.push(val); saveCustomCats(custom);
    }}
    const txn      = ALL_TXNS.find(t => t.id === id);
    const merchant = txn ? (NAMES[txn.id] || txn.name) : null;
    const matches  = merchant ? ALL_TXNS.filter(t => (NAMES[t.id]||t.name) === merchant) : [];
    OVERRIDES[id]  = val;
    syncCatDropdown();  // sync le dropdown filtre
    if (matches.length > 1) {{
      showBulkToast(merchant, val, matches, currentCat, id);
    }} else {{
      localStorage.setItem('catOverrides', JSON.stringify(OVERRIDES));
      renderTxns(document.getElementById('txn-search')?.value||'', document.getElementById('txn-acct')?.value||'', document.getElementById('txn-cat')?.value||'');
      if (initializedTabs.has('budget')) renderBudget(getFilteredTxns(currentDays));
    }}
  }}

  function commit() {{
    if (committed) return; committed = true;
    const val = sel.value;
    if (val === '__delete__') {{
      // Remettre le badge avant de delete pour éviter le DOM zombie
      renderTxns(document.getElementById('txn-search')?.value||'', document.getElementById('txn-acct')?.value||'', document.getElementById('txn-cat')?.value||'');
      deleteCatFull(currentCat);
      return;
    }} else if (val === '__new__') {{
      // Inline création d'une nouvelle cat
      const name = prompt('Nom de la nouvelle catégorie :');
      if (!name || !name.trim()) {{
        renderTxns(document.getElementById('txn-search')?.value||'', document.getElementById('txn-acct')?.value||'', document.getElementById('txn-cat')?.value||'');
        return;
      }}
      applyNewCat(name.trim());
    }} else {{
      applyNewCat(val);
    }}
  }}
  sel.addEventListener('change', commit);
  sel.addEventListener('blur', () => {{ if (!committed) commit(); }});
}}

// ── Bulk category toast ───────────────────────────────────────────────────────
function showBulkToast(merchant, newCat, matches, oldCat, triggerId) {{
  // Remove any existing toast
  document.getElementById('bulk-toast')?.remove();

  const toast = document.createElement('div');
  toast.id = 'bulk-toast';
  toast.style.cssText = `
    position:fixed; bottom:28px; left:50%; transform:translateX(-50%);
    background:#1a1a1a; border:1px solid #2563eb; border-radius:14px;
    padding:18px 24px; z-index:9999; min-width:360px; max-width:520px;
    box-shadow:0 8px 40px rgba(0,0,0,.7); font-family:'Geist',sans-serif;sans-serif;
    animation: slide-up .25s cubic-bezier(.4,0,.2,1);
  `;

  const others = matches.filter(t => t.id !== triggerId);

  toast.innerHTML = `
    <style>@keyframes slide-up {{ from {{ opacity:0;transform:translateX(-50%) translateY(20px) }} to {{ opacity:1;transform:translateX(-50%) translateY(0) }} }}</style>
    <div style="font-size:13px;font-weight:700;color:#f1f1f1;margin-bottom:6px">
      Appliquer à toutes les transactions de <span style="color:#93c5fd">"${{merchant}}"</span> ?
    </div>
    <div style="font-size:12px;color:#666;margin-bottom:16px">
      ${{matches.length}} transaction${{matches.length>1?'s':''}} · <span style="color:#4fc978">→ ${{newCat}}</span>
    </div>
    <div style="display:flex;gap:10px">
      <button id="bulk-yes" style="flex:1;padding:9px;background:#1e3a8a;border:none;border-radius:8px;color:#fff;font-family:'Geist',sans-serif;font-size:13px;font-weight:600;cursor:pointer">
        ✓ Appliquer aux ${{matches.length}}
      </button>
      <button id="bulk-no" style="padding:9px 16px;background:#181818;border:1px solid #333;border-radius:8px;color:#777;font-family:'Geist',sans-serif;font-size:13px;cursor:pointer">
        Juste celle-ci
      </button>
      <button id="bulk-cancel" style="padding:9px 16px;background:#181818;border:1px solid #333;border-radius:8px;color:#777;font-family:'Geist',sans-serif;font-size:13px;cursor:pointer">
        ✕
      </button>
    </div>
  `;

  document.body.appendChild(toast);

  function save() {{
    localStorage.setItem('catOverrides', JSON.stringify(OVERRIDES));
    syncCatDropdown();
    renderTxns(document.getElementById('txn-search')?.value||'',document.getElementById('txn-acct')?.value||'',document.getElementById('txn-cat')?.value||'');
    if (initializedTabs.has('budget')) renderBudget(getFilteredTxns(currentDays));
    renderRules();
    toast.remove();
  }}

  document.getElementById('bulk-yes').onclick = () => {{
    matches.forEach(t => {{ OVERRIDES[t.id] = newCat; }});
    save();
  }};
  document.getElementById('bulk-no').onclick = () => {{
    // OVERRIDES[triggerId] already set above
    save();
  }};
  document.getElementById('bulk-cancel').onclick = () => {{
    delete OVERRIDES[triggerId];
    toast.remove();
    renderTxns(document.getElementById('txn-search')?.value||'',document.getElementById('txn-acct')?.value||'',document.getElementById('txn-cat')?.value||'');
  }};

  // Auto-dismiss after 8s
  setTimeout(() => {{ if (document.getElementById('bulk-toast')) {{ OVERRIDES[triggerId] = oldCat; toast.remove(); }} }}, 8000);
}}

// ── Rules panel ───────────────────────────────────────────────────────────────
function renderRules() {{
  const panel = document.getElementById('rules-panel');
  if (!panel) return;

  // Build rules: merchant → overridden category (where ALL txns of that merchant share same override)
  const merchantMap = {{}};
  ALL_TXNS.forEach(t => {{
    const name = NAMES[t.id] || t.name;
    if (!merchantMap[name]) merchantMap[name] = {{ ids:[], cats:new Set(), origCat: t.category }};
    merchantMap[name].ids.push(t.id);
    const effective = OVERRIDES[t.id] || t.category;
    merchantMap[name].cats.add(effective);
  }});

  // Only show merchants where at least one txn has an override
  const rules = Object.entries(merchantMap)
    .filter(([name, d]) => d.ids.some(id => OVERRIDES[id]))
    .map(([name, d]) => {{
      const overriddenCat = OVERRIDES[d.ids.find(id => OVERRIDES[id])];
      const allSame = d.ids.filter(id => OVERRIDES[id]).every(id => OVERRIDES[id] === overriddenCat);
      return {{ name, count: d.ids.length, cat: overriddenCat, allSame, ids: d.ids, origCat: d.origCat }};
    }})
    .sort((a,b) => b.count - a.count);

  if (rules.length === 0) {{
    panel.innerHTML = `<p style="color:#444;font-size:13px">Aucune règle — change la catégorie d'une transaction pour en créer une.</p>`;
    return;
  }}

  panel.innerHTML = rules.map(r => `
    <div style="display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid #1a1a1a">
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;font-weight:600;color:#f1f1f1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${{r.name}}</div>
        <div style="font-size:11px;color:#555;margin-top:2px">${{r.ids.filter(id=>OVERRIDES[id]).length}} / ${{r.count}} transaction${{r.count>1?'s':''}}</div>
      </div>
      <div style="display:flex;align-items:center;gap:8px;flex-shrink:0">
        <span style="font-size:11px;color:#666">→</span>
        <span style="background:rgba(37,99,235,0.2);border:1px solid rgba(37,99,235,0.35);color:#93c5fd;border-radius:6px;padding:3px 9px;font-size:11px;font-weight:600">${{r.cat}}</span>
        <button onclick="clearRule('${{r.name}}')" style="background:none;border:none;color:#333;cursor:pointer;font-size:14px;padding:2px 4px" title="Supprimer la règle" onmouseover="this.style.color='#f76e6e'" onmouseout="this.style.color='#333'">✕</button>
      </div>
    </div>
  `).join('');
}}

function clearRule(merchantName) {{
  ALL_TXNS.forEach(t => {{
    if ((NAMES[t.id]||t.name) === merchantName) delete OVERRIDES[t.id];
  }});
  localStorage.setItem('catOverrides', JSON.stringify(OVERRIDES));
  renderTxns(document.getElementById('txn-search')?.value||'',document.getElementById('txn-acct')?.value||'',document.getElementById('txn-cat')?.value||'');
  if (initializedTabs.has('budget')) renderBudget(getFilteredTxns(currentDays));
  renderRules();
}}


// ── Income / Expenses summary ─────────────────────────────────────────────────
function renderIncomeSummary(txns) {{
  const el = document.getElementById('income-summary');
  if (!el) return;
  const skip = new Set(['Cash/Virements','Investissements','Paiement propre']);
  let revenus = 0, depenses = 0;
  txns.forEach(t => {{
    if (SELF_PAY_SET.has(t.id)) return;
    const ec = OVERRIDES[t.id] || t.category;
    if (skip.has(ec)) return;
    if (t.amount < 0) revenus  += Math.abs(t.amount);
    else              depenses += t.amount;
  }});
  const net = revenus - depenses;
  const nc  = net >= 0 ? '#b8f566' : '#ff4444';
  const fmt = v => v.toLocaleString('fr-CA', {{minimumFractionDigits:2, maximumFractionDigits:2}});
  const card = (label, val, color) => `
    <div style="background:#141414;border:1px solid #222;border-radius:14px;padding:18px 22px;text-align:center">
      <div style="font-family:'Geist Mono',monospace;font-size:1.5rem;font-weight:800;color:${{color}};letter-spacing:-0.03em">$${{fmt(Math.abs(val))}}</div>
      <div style="font-size:11px;color:#555;margin-top:6px;letter-spacing:.08em;text-transform:uppercase">${{label}}</div>
    </div>`;
  el.innerHTML =
    card('Revenus', revenus,  '#b8f566') +
    card('Dépenses', depenses,'#f1f1f1') +
    card('Net',      net,     nc);
}}

// ── Top 10 marchands ──────────────────────────────────────────────────────────
function renderTopMerchants(txns) {{
  const el = document.getElementById('top-merchants');
  if (!el) return;
  const skip = new Set(['Cash/Virements','Investissements','Revenus/Depots','Paiement propre']);
  const merchants = {{}};
  txns.forEach(t => {{
    if (t.amount <= 0) return;
    if (SELF_PAY_SET.has(t.id)) return;
    const ec = OVERRIDES[t.id] || t.category;
    if (skip.has(ec)) return;
    const name = NAMES[t.id] || t.name;
    merchants[name] = (merchants[name]||0) + t.amount;
  }});
  const sorted = Object.entries(merchants).sort((a,b)=>b[1]-a[1]).slice(0,10);
  if (!sorted.length) {{ el.innerHTML = '<p style="color:#555;font-size:13px">Aucune donnée</p>'; return; }}
  const max = sorted[0][1];
  el.innerHTML = sorted.map(([name, total]) => {{
    const pct = Math.round((total/max)*100);
    const fmt = total.toLocaleString('fr-CA',{{minimumFractionDigits:2,maximumFractionDigits:2}});
    return `<div style="display:grid;grid-template-columns:1fr auto;align-items:center;gap:12px;margin-bottom:10px">
      <div>
        <div style="font-size:12px;color:#ccc;margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${{name}}</div>
        <div style="background:#1a1a1a;border-radius:4px;height:6px;overflow:hidden">
          <div style="width:${{pct}}%;background:#b8f566;height:100%;border-radius:4px;transition:width .4s ease"></div>
        </div>
      </div>
      <div style="font-family:'Geist Mono',monospace;font-size:13px;font-weight:700;color:#b8f566;white-space:nowrap">$${{fmt}}</div>
    </div>`;
  }}).join('');
}}

// ── Abonnements détectés ──────────────────────────────────────────────────────
function toggleAbonnements() {{
  const panel   = document.getElementById('abo-panel');
  const chevron = document.getElementById('abo-chevron');
  if (!panel) return;
  const open = panel.style.display !== 'none';
  panel.style.display   = open ? 'none' : 'block';
  chevron.style.transform = open ? '' : 'rotate(90deg)';
  if (!open) renderAbonnements();
}}

function renderAbonnements() {{
  const el = document.getElementById('abo-list');
  if (!el) return;

  const skip = new Set(['Cash/Virements','Investissements','Revenus/Depots','Paiement propre']);
  const merchants = {{}};
  ALL_TXNS.forEach(t => {{
    if (t.amount <= 0) return;
    const ec = OVERRIDES[t.id] || t.category;
    if (skip.has(ec)) return;
    const name = NAMES[t.id] || t.name;
    if (!merchants[name]) merchants[name] = [];
    merchants[name].push({{ amount: t.amount, date: t.date }});
  }});

  const subs = [];
  Object.entries(merchants).forEach(([name, txs]) => {{
    if (txs.length < 2) return;
    const avg = txs.reduce((s,t)=>s+t.amount,0) / txs.length;
    const allClose = txs.every(t => Math.abs(t.amount - avg)/avg <= 0.05);
    if (!allClose) return;

    // Detect frequency from dates
    const dates = txs.map(t=>new Date(t.date)).sort((a,b)=>a-b);
    let freq = 'mensuel';
    if (dates.length >= 2) {{
      const gaps = [];
      for (let i=1;i<dates.length;i++) gaps.push((dates[i]-dates[i-1])/(1000*60*60*24));
      const avgGap = gaps.reduce((a,b)=>a+b,0)/gaps.length;
      if (avgGap < 10)       freq = 'hebdomadaire';
      else if (avgGap < 20)  freq = 'bihebdomadaire';
      else                   freq = 'mensuel';
    }}

    subs.push({{ name, avg, count: txs.length, total: txs.reduce((s,t)=>s+t.amount,0), freq }});
  }});

  subs.sort((a,b)=>b.total-a.total);

  if (!subs.length) {{
    el.innerHTML = '<p style="color:#555;font-size:13px">Aucun abonnement récurrent détecté.</p>';
    return;
  }}

  const fmt = v => v.toLocaleString('fr-CA',{{minimumFractionDigits:2,maximumFractionDigits:2}});
  el.innerHTML = subs.map(s => `
    <div style="display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid #1a1a1a;gap:12px">
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;font-weight:600;color:#f1f1f1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${{s.name}}</div>
        <div style="font-size:11px;color:#555;margin-top:2px">${{s.count}}× · ${{s.freq}}</div>
      </div>
      <div style="text-align:right;flex-shrink:0">
        <div style="font-family:'Geist Mono',monospace;font-size:13px;font-weight:700;color:#b8f566">$${{fmt(s.avg)}}/mois</div>
        <div style="font-size:11px;color:#555;margin-top:1px">Total $${{fmt(s.total)}}</div>
      </div>
    </div>
  `).join('');
}}

// ── Export CSV ────────────────────────────────────────────────────────────────
function exportFilteredCsv() {{
  const filter = document.getElementById('txn-search')?.value||'';
  const acct   = document.getElementById('txn-acct')?.value||'';
  const cat    = document.getElementById('txn-cat')?.value||'';
  const q      = filter.toLowerCase();
  const pool   = getFilteredTxns(currentDays);
  const rows   = pool.filter(t => {{
    const ec = OVERRIDES[t.id]||t.category;
    const en = NAMES[t.id]||t.name;
    if (acct && t.account !== acct) return false;
    if (cat  && ec !== cat)         return false;
    if (q && !en.toLowerCase().includes(q) && !ec.toLowerCase().includes(q)) return false;
    return true;
  }});
  const headers = ['Date','Marchand','Catégorie','Compte','Montant'];
  const lines = [headers.join(',')].concat(rows.map(t => {{
    const ec  = OVERRIDES[t.id]||t.category;
    const en  = NAMES[t.id]||t.name;
    const amt = t.amount < 0 ? Math.abs(t.amount) : -t.amount;
    const acc = getAccName(t.account);
    const esc = v => '"' + String(v).replace(/"/g,'""') + '"';
    return [t.date, esc(en), esc(ec), esc(acc), amt.toFixed(2)].join(',');
  }}));
  const blob = new Blob([lines.join('\\r\\n')], {{type:'text/csv;charset=utf-8;'}});
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href     = url;
  a.download = `transactions_${{new Date().toISOString().substring(0,10)}}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}}

// ── Refresh all ───────────────────────────────────────────────────────────────
function refreshAll() {{
  const txns = getFilteredTxns(currentDays);
  renderMonthly(txns); renderDonut(txns); renderWeekly(txns);
  renderIncomeSummary(txns); renderTopMerchants(txns);
  if (initializedTabs.has('budget')) renderBudget(txns);
  if (initializedTabs.has('txns'))   renderTxns(
    document.getElementById('txn-search')?.value||'',
    document.getElementById('txn-acct')?.value||'',
    document.getElementById('txn-cat')?.value||''
  );
}}

window.addEventListener('DOMContentLoaded', () => refreshAll());

// ── Investissements (Questrade) ───────────────────────────────────────────────
function initInvest() {{
  // Balances par compte
  const balEl = document.getElementById('qt-balances');
  if (balEl && QT_BALANCES.length) {{
    const byAcc = {{}};
    QT_BALANCES.forEach(b => {{
      const acc = b._account || 'Inconnu';
      if (!byAcc[acc]) byAcc[acc] = [];
      byAcc[acc].push(b);
    }});
    balEl.innerHTML = Object.entries(byAcc).map(([acc, bals]) => {{
      const cad = bals.find(b => b.currency === 'CAD');
      const usd = bals.find(b => b.currency === 'USD');
      const totalCAD = cad?.totalEquity || 0;
      const cashCAD  = cad?.cash || 0;
      return `<div style="background:#181818;border:1px solid #222;border-radius:12px;padding:16px 20px;min-width:200px">
        <div style="font-size:0.7rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#555;margin-bottom:8px">${{acc}}</div>
        <div style="font-size:1.3rem;font-weight:800;color:#f1f1f1">$${{totalCAD.toLocaleString('fr-CA',{{minimumFractionDigits:2,maximumFractionDigits:2}})}}</div>
        <div style="font-size:0.75rem;color:#555;margin-top:4px">Liquidités : $${{cashCAD.toLocaleString('fr-CA',{{minimumFractionDigits:2,maximumFractionDigits:2}})}}</div>
        ${{usd ? `<div style="font-size:0.7rem;color:#444;margin-top:2px">USD : $${{(usd.totalEquity||0).toLocaleString('en-US',{{minimumFractionDigits:2,maximumFractionDigits:2}})}}</div>` : ''}}
      </div>`;
    }}).join('');
  }} else if (balEl) {{
    balEl.innerHTML = '<p style="color:#444;font-size:13px">Aucune balance — token Questrade requis</p>';
  }}

  // Positions table
  const tbody = document.getElementById('qt-tbody');
  if (!tbody) return;
  if (!QT_POSITIONS.length) {{
    tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:#444;padding:24px">Aucune position — token Questrade requis</td></tr>';
    return;
  }}
  // Sort by market value desc
  const sorted = [...QT_POSITIONS].sort((a,b) => (b.currentMarketValue||0) - (a.currentMarketValue||0));
  tbody.innerHTML = sorted.map(p => {{
    const gp     = p.openPnl ?? (p.currentMarketValue - (p.averageEntryPrice * p.openQuantity));
    const gpPct  = p.averageEntryPrice > 0 ? (gp / (p.averageEntryPrice * p.openQuantity)) * 100 : 0;
    const cls    = gp >= 0 ? 'green' : 'red';
    const mv     = p.currentMarketValue || 0;
    const qty    = p.openQuantity || 0;
    const avg    = p.averageEntryPrice || 0;
    const cur    = p.currentPrice || 0;
    const sym    = p.symbol || '';
    const desc   = p.symbolId ? (p.description || sym) : sym;
    const acc    = p._account || '';
    return `<tr>
      <td style="font-weight:700;color:#f1f1f1">${{sym}}</td>
      <td style="color:#aaa;font-size:0.82rem;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{desc}}">${{desc}}</td>
      <td style="color:#666;font-size:0.78rem">${{acc}}</td>
      <td style="text-align:right">${{qty % 1 === 0 ? qty : qty.toFixed(4)}}</td>
      <td style="text-align:right;color:#aaa">$${{avg.toFixed(2)}}</td>
      <td style="text-align:right;color:#aaa">$${{cur.toFixed(2)}}</td>
      <td style="text-align:right;font-weight:600">$${{mv.toLocaleString('fr-CA',{{minimumFractionDigits:2,maximumFractionDigits:2}})}}</td>
      <td style="text-align:right" class="${{cls}}">${{gp >= 0 ? '+' : ''}}$${{Math.abs(gp).toFixed(2)}}</td>
      <td style="text-align:right" class="${{cls}}">${{gp >= 0 ? '+' : ''}}${{gpPct.toFixed(1)}}%</td>
    </tr>`;
  }}).join('');
}}

// ── CSV Import ──────────────────────────────────────────────────────────────
function handleCsvFiles(files) {{
  if (!files || files.length === 0) return;
  const file = files[0];
  const statusEl = document.getElementById('csv-status');
  statusEl.style.display = 'block';
  statusEl.style.color = '#aaa';
  statusEl.textContent = '⏳ Envoi en cours...';

  const fd = new FormData();
  fd.append('file', file);

  fetch('/upload-csv', {{method: 'POST', body: fd}})
    .then(r => r.json())
    .then(d => {{
      if (d.ok) {{
        statusEl.style.color = '#4fc978';
        statusEl.textContent = d.rows + ' transactions importées — Refresh en cours...';
        // Poll until regeneration done
        const poll = setInterval(() => {{
          fetch('/status').then(r=>r.json()).then(s => {{
            if (!s.running) {{
              clearInterval(poll);
              location.reload();
            }}
          }}).catch(() => clearInterval(poll));
        }}, 1500);
        // Fallback: reload after 8s regardless
        setTimeout(() => location.reload(), 8000);
      }} else {{
        statusEl.style.color = '#f76e6e';
        statusEl.textContent = '❌ Erreur: ' + (d.error || 'upload échoué');
      }}
    }})
    .catch(err => {{
      statusEl.style.color = '#f76e6e';
      statusEl.textContent = '❌ Serveur non disponible — lance python3 server.py';
    }});
}}

// ── Refresh button ─────────────────────────────────────────────────────────────
function doRefresh() {{
  const btn  = document.getElementById('refresh-btn');
  const icon = document.getElementById('refresh-icon');
  btn.disabled = true;
  btn.style.opacity = '0.5';
  icon.style.display = 'inline-block';
  icon.style.animation = 'spin-icon 1s linear infinite';

  fetch('/refresh', {{method:'POST'}})
    .then(r => r.json())
    .then(d => {{
      if (d.ok) {{
        const poll = setInterval(() => {{
          fetch('/status').then(r=>r.json()).then(s => {{
            if (!s.running) {{
              clearInterval(poll);
              location.reload();
            }}
          }}).catch(() => clearInterval(poll));
        }}, 1500);
      }} else {{
        alert(d.msg || 'Erreur refresh');
        btn.disabled = false; btn.style.opacity = '1';
      }}
    }})
    .catch(() => {{
      btn.disabled = false; btn.style.opacity = '1';
      icon.style.animation = '';
    }});
}}

// ── Account pills drag & drop ──────────────────────────────────────────────────
(function() {{
  const STORAGE_KEY = 'pillOrder_v1';
  let dragEl = null;

  function saveOrder() {{
    const ids = [...document.querySelectorAll('.accounts-grid .acc-pill')].map(p => p.dataset.accid);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(ids));
  }}

  function applyOrder() {{
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
    if (!saved) return;
    const grid = document.querySelector('.accounts-grid');
    if (!grid) return;
    saved.forEach(id => {{
      const el = grid.querySelector(`[data-accid="${{id}}"]`);
      if (el) grid.appendChild(el);
    }});
  }}

  function initPillDrag() {{
    const grid = document.querySelector('.accounts-grid');
    if (!grid) return;
    applyOrder();
    grid.querySelectorAll('.acc-pill').forEach(pill => {{
      pill.setAttribute('draggable', 'true');
      pill.addEventListener('dragstart', e => {{
        dragEl = pill;
        setTimeout(() => pill.classList.add('dragging'), 0);
        e.dataTransfer.effectAllowed = 'move';
      }});
      pill.addEventListener('dragend', () => {{
        pill.classList.remove('dragging');
        grid.querySelectorAll('.acc-pill').forEach(p => p.classList.remove('drag-over'));
        dragEl = null;
        saveOrder();
      }});
      pill.addEventListener('dragover', e => {{
        e.preventDefault();
        if (!dragEl || dragEl === pill) return;
        grid.querySelectorAll('.acc-pill').forEach(p => p.classList.remove('drag-over'));
        pill.classList.add('drag-over');
      }});
      pill.addEventListener('drop', e => {{
        e.preventDefault();
        if (!dragEl || dragEl === pill) return;
        const pills = [...grid.querySelectorAll('.acc-pill')];
        const fromIdx = pills.indexOf(dragEl);
        const toIdx = pills.indexOf(pill);
        grid.insertBefore(dragEl, fromIdx < toIdx ? pill.nextSibling : pill);
        grid.querySelectorAll('.acc-pill').forEach(p => p.classList.remove('drag-over'));
        saveOrder();
      }});
    }});
  }}

  window.addEventListener('DOMContentLoaded', () => setTimeout(initPillDrag, 100));
}})();

// ── Odometer digit roll ────────────────────────────────────────────────────────
function odometerReveal(el) {{
  const text = el.textContent.trim();
  el.innerHTML = '';
  el.style.cssText += 'display:inline-flex;align-items:baseline;';

  text.split('').forEach((char, i) => {{
    if (!/\d/.test(char)) {{
      const sep = document.createElement('span');
      sep.textContent = char;
      sep.style.opacity = '0.35';
      el.appendChild(sep);
      return;
    }}
    const digit = parseInt(char);
    const slot = document.createElement('span');
    slot.style.cssText = 'display:inline-block;overflow:hidden;height:1.05em;';
    const reel = document.createElement('span');
    reel.style.cssText = 'display:flex;flex-direction:column;will-change:transform;';
    // 0→9 stacked, showing digit d = translateY(-(d/10)*100%)
    for (let d = 0; d <= 9; d++) {{
      const n = document.createElement('span');
      n.textContent = d;
      n.style.cssText = 'display:block;height:1.05em;line-height:1.05;text-align:center;';
      reel.appendChild(n);
    }}
    slot.appendChild(reel);
    el.appendChild(slot);
    // Start at 0, animate to target digit
    reel.style.transform = 'translateY(0)';
    const delay = i * 55;
    setTimeout(() => {{
      reel.style.transition = `transform 1.1s cubic-bezier(0.16,1,0.3,1)`;
      reel.style.transform = `translateY(-${{digit * 10}}%)`;
    }}, delay + 80);
  }});
}}

window.addEventListener('DOMContentLoaded', () => {{
  const nwEl = document.getElementById('nw-number');
  if (nwEl) odometerReveal(nwEl);

  refreshAll();
  setTimeout(applyEmerald, 400);
}});
// ── Spotlight mouse tracking ───────────────────────────────────────────────────
document.addEventListener('mousemove', e => {{
  document.querySelectorAll('.month-card, .chart-card, .table-card, .acc-pill').forEach(el => {{
    const rect = el.getBoundingClientRect();
    el.style.setProperty('--mx', ((e.clientX - rect.left) / rect.width * 100).toFixed(1) + '%');
    el.style.setProperty('--my', ((e.clientY - rect.top)  / rect.height * 100).toFixed(1) + '%');
  }});
}});

// ── Apply emerald shimmer to all .green elements ───────────────────────────────
function applyEmerald() {{
  document.querySelectorAll('.green').forEach(el => {{
    if (!el.classList.contains('emerald-shimmer')) {{
      el.classList.add('emerald-shimmer');
    }}
  }});
}}

// ── Init animations on load ────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {{
  // Scramble net worth
  const nwEl = document.getElementById('nw-number');
  if (nwEl) {{
    const final = nwEl.textContent.trim();
    setTimeout(() => scramble(nwEl, final, 900), 150);
  }}

  refreshAll();
  setTimeout(applyEmerald, 400);
}});
</script>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    check_config()
    print("\n📊 Budget Dashboard — génération en cours...\n")
    print("🔗 Connexion aux APIs...")
    balances = get_plaid_balances()
    wise_bal, wise_balance_ids = get_wise_balances()
    sol_bal, sol_usd = get_phantom_balance()
    print("\n📥 Transactions Plaid...")
    raw_txns = get_plaid_transactions()
    print("\n📥 Transactions Wise...")
    wise_txns = get_wise_transactions(wise_balance_ids)
    print("\n📥 Transactions CSV...")
    csv_txns = get_csv_transactions()
    print("\n⚙️  Traitement...")
    txns = process_transactions(raw_txns, wise_txns, csv_txns)
    print(f"  {len(txns)} transactions après filtrage")
    print("\n📥 Questrade...")
    qt_data = get_questrade_data()
    print("\n🎨 Génération HTML...")
    html = build_html(balances, wise_bal, sol_bal, sol_usd, txns, qt_data)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✅ Dashboard → {OUTPUT_PATH}\n")
    if "--no-open" not in sys.argv:
        webbrowser.open(f"file://{OUTPUT_PATH}")
