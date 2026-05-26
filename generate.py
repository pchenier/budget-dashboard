#!/usr/bin/env python3
"""
Budget Local — Dashboard Generator
Pulls Plaid + Wise + Phantom (Solana) → génère un dashboard HTML.

Usage:
    python generate.py           # génère + ouvre le browser
    python generate.py --no-open # génère seulement
"""

import json, os, sys, webbrowser, requests
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

USD_TO_CAD    = float(os.getenv("USD_TO_CAD", "1.38"))
START_DATE    = os.getenv("START_DATE", "2025-01-01")
END_DATE      = date.today().isoformat()
OUTPUT_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")


# ── Validation ────────────────────────────────────────────────────────────────
def check_config():
    errors = []
    if not PLAID_CLIENT or PLAID_CLIENT == "xxxxxxxxxxxxxxxxxxxxxxxx":
        errors.append("PLAID_CLIENT_ID manquant dans .env")
    if not PLAID_SECRET or PLAID_SECRET == "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx":
        errors.append("PLAID_SECRET manquant dans .env")
    if not PLAID_TOKEN or "xxxxxxxx" in PLAID_TOKEN:
        errors.append("PLAID_ACCESS_TOKEN manquant — lance setup_plaid.py d'abord")
    if errors:
        print("\n❌ Config incomplète:")
        for e in errors:
            print(f"   • {e}")
        print("\n→ Copie .env.example → .env et remplis les valeurs\n")
        sys.exit(1)


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
    print("  Plaid: balances...")
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


def get_plaid_transactions():
    print("  Plaid: transactions...")
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


# ── Wise ──────────────────────────────────────────────────────────────────────
def get_wise_balances():
    if not WISE_TOKEN or not WISE_PROFILE:
        print("  Wise: skipped (pas configuré)")
        return {}
    print("  Wise: balances...")
    try:
        r = requests.get(
            f"https://api.wise.com/v4/profiles/{WISE_PROFILE}/balances?types=STANDARD",
            headers={"Authorization": f"Bearer {WISE_TOKEN}"},
            timeout=15,
        )
        r.raise_for_status()
        result = {}
        for b in r.json():
            cur = b["totalWorth"]["currency"]
            amt = b["totalWorth"]["value"]
            result[cur] = amt
        return result
    except Exception as e:
        print(f"  Wise: erreur → {e}")
        return {}


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


def process_transactions(raw_txns):
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
    return cleaned


def fmt_cad(n):
    sign = "-" if n < 0 else ""
    return f"{sign}${abs(n):,.2f}"


# ── HTML ──────────────────────────────────────────────────────────────────────
DONUT_COLORS = [
    "#4f86f7","#f7c948","#f76e6e","#4fc978","#c97ef7",
    "#f7964f","#4ff7e8","#f74fc9","#a8f74f","#f74f4f",
    "#4f4ff7","#f7f74f","#aaaaaa","#888888","#55efc4",
    "#fd79a8","#6c5ce7","#00b894","#e17055","#74b9ff",
]

def build_html(balances, wise_bal, sol_balance, sol_usd, txns):
    # ── Net worth ────────────────────────────────────────────────────────────
    checking_total = sum(a["current"] for a in balances.values() if a["type"] == "depository")
    credit_total   = sum(a["current"] for a in balances.values() if a["type"] == "credit")
    wise_usd_val   = wise_bal.get("USD", 0)
    wise_cad_val   = wise_bal.get("CAD", 0)
    wise_usd_cad   = wise_usd_val * USD_TO_CAD
    sol_cad        = sol_balance * sol_usd * USD_TO_CAD
    net_worth      = checking_total + wise_cad_val + wise_usd_cad + sol_cad - credit_total

    # ── Account pills HTML ───────────────────────────────────────────────────
    pills_html = ""
    for acc in balances.values():
        is_credit = acc["type"] == "credit"
        cls = "debit" if is_credit else "credit"
        label = "Dû" if is_credit else ""
        val_str = f"{'-' if is_credit else ''}{fmt_cad(acc['current'])}"
        pills_html += f"""
    <div class="acc-pill">
      <span class="acc-label">{acc['name']}</span>
      <span class="acc-val {cls}">{val_str}</span>
    </div>"""
    if wise_usd_val or wise_cad_val:
        if wise_usd_val:
            pills_html += f"""
    <div class="acc-pill">
      <span class="acc-label">Wise USD</span>
      <span class="acc-val wise">${wise_usd_val:,.2f} USD</span>
    </div>
    <div class="acc-pill">
      <span class="acc-label">Wise → CAD</span>
      <span class="acc-val wise">{fmt_cad(wise_usd_cad)}</span>
    </div>"""
        if wise_cad_val:
            pills_html += f"""
    <div class="acc-pill">
      <span class="acc-label">Wise CAD</span>
      <span class="acc-val wise">{fmt_cad(wise_cad_val)}</span>
    </div>"""
    if sol_balance > 0:
        pills_html += f"""
    <div class="acc-pill">
      <span class="acc-label">Phantom SOL</span>
      <span class="acc-val wise">{sol_balance:.4f} SOL ≈ {fmt_cad(sol_cad)}</span>
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

    generated_at = datetime.now().strftime("%B %d, %Y at %H:%M")

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Budget Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --bg:       #0a0a0a;
    --surface:  #111111;
    --surface2: #181818;
    --border:   #222222;
    --accent:   #1e3a8a;
    --accent2:  #2563eb;
    --text:     #f1f1f1;
    --muted:    #777;
    --green:    #4fc978;
    --red:      #f76e6e;
    --radius:   16px;
  }}
  body {{ font-family: 'Montserrat', sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; padding: 0 0 60px; }}

  /* Header */
  .header {{ background: linear-gradient(135deg, #0d1b4b 0%, #1e3a8a 50%, #0d2060 100%); padding: 40px 40px 48px; position: relative; overflow: hidden; }}
  .header::after {{ content: ''; position: absolute; bottom: -30px; left: 0; right: 0; height: 60px; background: var(--bg); border-radius: 50% 50% 0 0 / 20px 20px 0 0; }}
  .header-top {{ display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 20px; }}
  .header h1 {{ font-size: 1.1rem; font-weight: 500; color: rgba(255,255,255,0.6); letter-spacing: 0.15em; text-transform: uppercase; margin-bottom: 8px; }}
  .net-worth-amount {{ font-size: clamp(2.5rem, 6vw, 4rem); font-weight: 800; letter-spacing: -0.02em; background: linear-gradient(90deg, #fff 0%, #93c5fd 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }}
  .generated {{ font-size: 0.75rem; color: rgba(255,255,255,0.4); align-self: flex-end; }}
  .accounts-grid {{ display: flex; flex-wrap: wrap; gap: 14px; margin-top: 28px; }}
  .acc-pill {{ background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.12); border-radius: 50px; padding: 10px 20px; display: flex; flex-direction: column; align-items: center; min-width: 140px; backdrop-filter: blur(4px); }}
  .acc-pill .acc-label {{ font-size: 0.68rem; font-weight: 600; letter-spacing: 0.1em; text-transform: uppercase; color: rgba(255,255,255,0.5); margin-bottom: 4px; }}
  .acc-pill .acc-val {{ font-size: 1.1rem; font-weight: 700; }}
  .acc-pill .acc-val.debit  {{ color: #f76e6e; }}
  .acc-pill .acc-val.credit {{ color: #4fc978; }}
  .acc-pill .acc-val.wise   {{ color: #93c5fd; }}

  /* Main */
  .main {{ max-width: 1200px; margin: 0 auto; padding: 50px 20px 0; }}
  .section-title {{ font-size: 0.75rem; font-weight: 700; letter-spacing: 0.2em; text-transform: uppercase; color: var(--muted); margin-bottom: 16px; padding-left: 4px; }}

  /* Monthly */
  .monthly-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 40px; }}
  .month-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; transition: transform 0.2s, box-shadow 0.2s; }}
  .month-card:hover {{ transform: translateY(-2px); box-shadow: 0 8px 32px rgba(37,99,235,0.15); }}
  .month-card h3 {{ font-size: 1.1rem; font-weight: 700; margin-bottom: 16px; color: #fff; }}
  .month-row {{ display: flex; justify-content: space-between; align-items: center; padding: 8px 0; font-size: 0.88rem; border-bottom: 1px solid var(--border); }}
  .month-row:last-child {{ border-bottom: none; }}
  .net-row {{ font-weight: 700; font-size: 1rem; margin-top: 4px; }}
  .green {{ color: var(--green); }}
  .red   {{ color: var(--red); }}

  /* Charts */
  .charts-row {{ display: grid; grid-template-columns: 1fr 1.6fr; gap: 16px; margin-bottom: 40px; }}
  @media (max-width: 700px) {{ .charts-row {{ grid-template-columns: 1fr; }} }}
  .chart-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; }}
  .chart-card h2 {{ font-size: 0.85rem; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted); margin-bottom: 20px; }}
  .chart-wrapper {{ position: relative; height: 300px; }}
  .donut-layout {{ display: flex; gap: 20px; align-items: flex-start; flex-wrap: wrap; }}
  .donut-legend {{ flex: 1; min-width: 140px; display: flex; flex-direction: column; gap: 8px; max-height: 300px; overflow-y: auto; padding-right: 4px; }}
  .donut-legend::-webkit-scrollbar {{ width: 3px; }}
  .donut-legend::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 4px; }}
  .legend-item {{ display: flex; align-items: center; gap: 8px; font-size: 0.75rem; font-weight: 500; }}
  .legend-dot {{ width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }}
  .legend-val {{ margin-left: auto; color: var(--muted); font-size: 0.7rem; }}

  /* Table */
  .table-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; margin-bottom: 40px; overflow-x: auto; }}
  .table-card h2 {{ font-size: 0.85rem; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted); margin-bottom: 20px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ font-size: 0.7rem; font-weight: 600; letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted); padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--border); }}
  td {{ padding: 12px 12px; font-size: 0.88rem; border-bottom: 1px solid rgba(255,255,255,0.04); vertical-align: middle; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: rgba(255,255,255,0.02); }}
  .badge {{ background: rgba(37,99,235,0.2); border: 1px solid rgba(37,99,235,0.35); color: #93c5fd; border-radius: 6px; padding: 3px 9px; font-size: 0.68rem; font-weight: 600; white-space: nowrap; cursor: pointer; }}

  /* Period buttons */
  .period-btn {{ background: #181818; border: 1px solid #333; color: #777; border-radius: 8px; padding: 6px 16px; font-size: 13px; font-family: Montserrat, sans-serif; cursor: pointer; transition: all .15s; }}
  .period-btn:hover {{ border-color: #2563eb; color: #93c5fd; }}
  .period-btn.active {{ background: #1e3a8a; border-color: #2563eb; color: #fff; font-weight: 600; }}

  /* Budget drag */
  .budget-row.drag-over {{ border: 1px dashed #2563eb; border-radius: 8px; padding: 4px 8px; }}

  /* Tabs */
  .tabs-nav {{ display: flex; gap: 0; border-bottom: 1px solid #222; margin: 0 0 32px; padding: 0 40px; background: var(--surface); }}
  .tab-btn {{ background: none; border: none; color: #555; cursor: pointer; font-family: Montserrat, sans-serif; font-size: 13px; font-weight: 600; padding: 16px 20px; position: relative; transition: color .15s; letter-spacing: .03em; text-transform: uppercase; }}
  .tab-btn:hover {{ color: #aaa; }}
  .tab-btn.active {{ color: #f1f1f1; }}
  .tab-btn.active::after {{ content: ''; position: absolute; bottom: -1px; left: 0; right: 0; height: 2px; background: #2563eb; border-radius: 2px 2px 0 0; }}
  .tab-panel {{ display: none; }}
  .tab-panel.active {{ display: block; }}
  @keyframes spin-icon {{ to {{ transform: rotate(360deg); }} }}
</style>
</head>
<body>

<div class="header">
  <div class="header-top">
    <div>
      <h1>Net Worth</h1>
      <div class="net-worth-amount">{fmt_cad(net_worth)}</div>
    </div>
    <div style="display:flex;flex-direction:column;align-items:flex-end;gap:10px">
      <div class="generated">Generated {generated_at}</div>
      <button id="refresh-btn" onclick="doRefresh()" style="display:inline-flex;align-items:center;gap:6px;padding:8px 18px;background:rgba(255,255,255,0.1);border:1px solid rgba(255,255,255,0.2);border-radius:8px;color:#fff;font-family:Montserrat,sans-serif;font-size:13px;font-weight:600;cursor:pointer;transition:all .2s" onmouseover="this.style.background='rgba(255,255,255,0.18)'" onmouseout="this.style.background='rgba(255,255,255,0.1)'">
        <span id="refresh-icon">🔄</span> Refresh
      </button>
    </div>
  </div>
  <div class="accounts-grid">
    {pills_html}
  </div>
</div>

<nav class="tabs-nav">
  <button class="tab-btn active" onclick="switchTab('overview')">Vue générale</button>
  <button class="tab-btn"        onclick="switchTab('budget')">Budget</button>
  <button class="tab-btn"        onclick="switchTab('txns')">Transactions</button>
</nav>

<div class="main">

  <!-- Period filter -->
  <div id="period-bar" style="display:flex;gap:8px;margin-bottom:24px;flex-wrap:wrap;align-items:center">
    <button class="period-btn active" data-days="30"   onclick="setPeriod(30)">30j</button>
    <button class="period-btn"        data-days="60"   onclick="setPeriod(60)">60j</button>
    <button class="period-btn"        data-days="90"   onclick="setPeriod(90)">90j</button>
    <button class="period-btn"        data-days="180"  onclick="setPeriod(180)">6 mois</button>
    <button class="period-btn"        data-days="365"  onclick="setPeriod(365)">12 mois</button>
    <button class="period-btn"        data-days="9999" onclick="setPeriod(9999)">Tout</button>
    <div style="position:relative;margin-left:4px">
      <button id="cal-btn" onclick="toggleCal()" style="background:#181818;border:1px solid #333;border-radius:8px;padding:7px 14px;color:#aaa;font-family:Montserrat;font-size:12px;cursor:pointer;display:flex;align-items:center;gap:6px">
        📅 <span id="cal-label">Mois</span>
      </button>
      <div id="cal-picker" style="display:none;position:absolute;top:calc(100% + 8px);left:0;z-index:200;background:#151515;border:1px solid #2a2a2a;border-radius:14px;padding:18px;box-shadow:0 8px 32px rgba(0,0,0,.6);min-width:260px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
          <button onclick="calShiftYear(-1)" style="background:none;border:none;color:#777;font-size:18px;cursor:pointer;padding:0 6px">‹</button>
          <span id="cal-year-label" style="font-size:13px;font-weight:600;color:#f1f1f1">2026</span>
          <button onclick="calShiftYear(1)"  style="background:none;border:none;color:#777;font-size:18px;cursor:pointer;padding:0 6px">›</button>
        </div>
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:14px" id="cal-months"></div>
        <div style="border-top:1px solid #222;padding-top:12px">
          <div style="font-size:11px;color:#555;margin-bottom:8px;text-transform:uppercase;letter-spacing:.05em">Plage custom</div>
          <div style="display:flex;gap:8px;align-items:center">
            <input id="range-from" type="date" onchange="applyRange()" style="flex:1;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:7px;padding:6px 8px;color:#f1f1f1;font-family:Montserrat;font-size:12px">
            <span style="color:#444">→</span>
            <input id="range-to"   type="date" onchange="applyRange()" style="flex:1;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:7px;padding:6px 8px;color:#f1f1f1;font-family:Montserrat;font-size:12px">
          </div>
          <button id="range-clear" onclick="clearRange()" style="display:none;margin-top:8px;background:none;border:none;color:#555;font-size:11px;font-family:Montserrat;cursor:pointer">✕ Effacer la plage</button>
        </div>
      </div>
    </div>
  </div>

  <!-- Tab: Vue générale -->
  <div id="tab-overview" class="tab-panel active">
    <div class="section-title">Résumé mensuel</div>
    <div class="monthly-grid" id="monthly-grid"></div>
    <div class="charts-row">
      <div class="chart-card">
        <h2>Dépenses par catégorie</h2>
        <div class="donut-layout">
          <div style="width:200px;height:200px;flex-shrink:0;position:relative;">
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
  </div>

  <!-- Tab: Budget -->
  <div id="tab-budget" class="tab-panel">
    <div class="table-card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;flex-wrap:wrap;gap:12px">
        <h2 id="budget-title">Budget · 30 jours</h2>
        <button onclick="addCustomCat()" style="background:#1e3a8a;border:none;color:#fff;border-radius:8px;padding:7px 16px;font-family:Montserrat;font-size:12px;font-weight:600;cursor:pointer">+ Ajouter catégorie</button>
      </div>
      <p style="font-size:12px;color:#444;margin-bottom:20px">Clique sur le montant cible pour modifier · Glisse pour réordonner</p>
      <div id="budget-bars"></div>
    </div>
  </div>

  <!-- Tab: Transactions -->
  <div id="tab-txns" class="tab-panel">
    <div class="table-card">
      <h2 style="margin-bottom:16px">Toutes les transactions</h2>
      <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px">
        <input id="txn-search" type="text" placeholder="Rechercher…"
          oninput="renderTxns(this.value,document.getElementById('txn-acct').value,document.getElementById('txn-cat').value)"
          style="flex:1;min-width:180px;background:#181818;border:1px solid #333;border-radius:8px;padding:8px 12px;color:#f1f1f1;font-family:Montserrat">
        <select id="txn-acct" onchange="renderTxns(document.getElementById('txn-search').value,this.value,document.getElementById('txn-cat').value)"
          style="background:#181818;border:1px solid #333;border-radius:8px;padding:8px 12px;color:#f1f1f1;font-family:Montserrat">
          <option value="">Tous les comptes</option>
        </select>
        <select id="txn-cat" onchange="renderTxns(document.getElementById('txn-search').value,document.getElementById('txn-acct').value,this.value)"
          style="background:#181818;border:1px solid #333;border-radius:8px;padding:8px 12px;color:#f1f1f1;font-family:Montserrat">
          <option value="">Toutes catégories</option>
        </select>
        <span id="txn-count" style="align-self:center;color:#777;font-size:13px"></span>
      </div>
      <div style="overflow-x:auto">
        <table>
          <thead><tr>
            <th>Date</th><th>Marchand</th><th>Catégorie</th><th>Compte</th>
            <th style="text-align:right">Montant</th>
          </tr></thead>
          <tbody id="txn-tbody"></tbody>
        </table>
      </div>
    </div>

    <!-- Règles de catégorie -->
    <div class="table-card" style="margin-top:0">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
        <h2 style="margin:0">Règles de catégorie</h2>
        <span style="font-size:12px;color:#444">Appliquées automatiquement à tous les marchands</span>
      </div>
      <div id="rules-panel">
        <p style="color:#444;font-size:13px">Aucune règle — change la catégorie d'une transaction pour en créer une.</p>
      </div>
    </div>
  </div>

</div>

<script>
// ── Data ──────────────────────────────────────────────────────────────────────
const ALL_TXNS     = {all_txns_json};
const CAT_COLORS   = {donut_colors_js};
const OVERRIDES    = JSON.parse(localStorage.getItem('catOverrides') || '{{}}');
const NAMES        = JSON.parse(localStorage.getItem('nameOverrides') || '{{}}');
window.OVERRIDES   = OVERRIDES;

// ── Tab switching ─────────────────────────────────────────────────────────────
let initializedTabs = new Set(['overview']);

function switchTab(name) {{
  document.querySelectorAll('.tab-btn').forEach((b,i) => {{
    b.classList.toggle('active', ['overview','budget','txns'][i] === name);
  }});
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  // period-bar visible on ALL tabs
  if (!initializedTabs.has(name)) {{
    initializedTabs.add(name);
    const txns = getFilteredTxns(currentDays);
    if (name === 'budget') renderBudget(txns);
    if (name === 'txns')   initTxns();
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
    return `<button onclick="calMonth('${{ym}}')" style="background:#1a1a1a;border:1px solid #2a2a2a;border-radius:7px;padding:6px 4px;color:#bbb;font-family:Montserrat;font-size:11px;cursor:pointer" onmouseover="this.style.background='#2563eb';this.style.color='#fff'" onmouseout="this.style.background='#1a1a1a';this.style.color='#bbb'">${{m}}</button>`;
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
    return `<div class="month-card">
      <h3>${{MN[mo]}} ${{yr}}</h3>
      <div class="month-row"><span>Total IN</span><span class="green">+$${{d.in.toFixed(2)}}</span></div>
      <div class="month-row"><span>Total OUT</span><span class="red">-$${{d.out.toFixed(2)}}</span></div>
      <div class="month-row net-row"><span>NET</span><span style="color:${{nc}}">$${{net.toFixed(2)}}</span></div>
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
    return `<button onclick="toggleDonutCat('${{lbl}}')" style="display:flex;flex-direction:column;align-items:flex-start;gap:2px;background:${{colors[i]}}22;border:1px solid ${{colors[i]}}66;border-radius:10px;padding:8px 12px;cursor:pointer;text-align:left">
      <div style="display:flex;align-items:center;gap:6px"><span style="width:8px;height:8px;border-radius:50%;background:${{colors[i]}};flex-shrink:0"></span><span style="font-size:11px;font-weight:600;color:#ddd;font-family:Montserrat">${{lbl}}</span></div>
      <span style="font-size:12px;font-weight:700;color:${{colors[i]}};font-family:Montserrat;padding-left:14px">${{pct}}%</span>
      <span style="font-size:10px;color:#666;font-family:Montserrat;padding-left:14px">$${{values[i].toLocaleString('fr-CA',{{minimumFractionDigits:0}})}}</span>
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
    options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }}, tooltip: {{ callbacks: {{ label: c => ` Net: $${{c.parsed.y.toLocaleString('fr-CA',{{minimumFractionDigits:2}})}}` }} }} }}, scales: {{ x: {{ ticks: {{ color:'#666',font:{{size:10,family:'Montserrat'}},maxRotation:45,autoSkip:true,maxTicksLimit:14 }},grid:{{color:'rgba(255,255,255,0.04)'}} }}, y: {{ ticks: {{ color:'#666',font:{{size:10,family:'Montserrat'}},callback:v=>'$'+v.toLocaleString('fr-CA') }},grid:{{color:'rgba(255,255,255,0.06)'}} }} }} }}
  }});
}}

// ── Budget ────────────────────────────────────────────────────────────────────
const SKIP_CATS = new Set(['Cash/Virements','Investissements']);
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
  renderBudget(getFilteredTxns(currentDays));
}}

function deleteCat(cat) {{
  const h = getHiddenCats(); if (!h.includes(cat)) h.push(cat); saveHiddenCats(h);
  const c = getCustomCats().filter(x => x !== cat); saveCustomCats(c);
  renderBudget(getFilteredTxns(currentDays));
}}
function restoreHidden() {{ saveHiddenCats([]); renderBudget(getFilteredTxns(currentDays)); }}

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

  // Avg monthly par cat sur toutes les données
  const allMonths = {{}};
  ALL_TXNS.filter(t => t.amount > 0 && !SKIP_CATS.has(OVERRIDES[t.id] || t.category)).forEach(t => {{
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
  txns.filter(t => t.amount > 0 && !SKIP_CATS.has(OVERRIDES[t.id] || t.category)).forEach(t => {{
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

  const bars = allCats.map(cat => {{
    const s       = spent[cat]||0;
    const monthly = targets[cat] || avgMonthly[cat] || 0;
    const b       = monthly * ratio;
    const pct     = b > 0 ? Math.min((s/b)*100, 100) : (s>0?100:0);
    const over    = b > 0 && s > b;
    const overAmt = over ? s-b : 0;
    const color   = pct < 60 ? '#4fc978' : pct < 85 ? '#f7c948' : '#f76e6e';
    const icon    = CAT_ICONS[cat] || '💳';
    const proj    = currentDays < 30 && s > 0 ? (s/currentDays*30) : null;
    const hasTarget = !!targets[cat];
    const targetLabel = hasTarget
      ? `<span onclick="editTarget('${{cat}}')" style="cursor:pointer;color:#2563eb;font-size:12px;border-bottom:1px dashed #2563eb">$${{monthly.toFixed(0)}}/mois ✏️</span>`
      : `<span onclick="editTarget('${{cat}}')" style="cursor:pointer;color:#444;font-size:12px;border-bottom:1px dashed #444">moy. $${{monthly.toFixed(0)}} ✏️</span>`;
    return `
    <div class="budget-row" data-cat="${{cat}}" draggable="true"
      ondragstart="onDragStart(event)" ondragend="onDragEnd(event)"
      ondragover="onDragOver(event)" ondrop="onDrop(event)"
      style="margin-bottom:20px;cursor:grab;user-select:none">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;gap:8px">
        <div style="display:flex;align-items:center;gap:8px;min-width:0">
          <span style="color:#333;font-size:13px;cursor:grab" title="Glisser pour réordonner">⠿</span>
          <span style="font-size:15px;font-weight:700;color:#f1f1f1;white-space:nowrap">${{icon}} ${{cat}}</span>
        </div>
        <div style="display:flex;align-items:center;gap:10px;flex-shrink:0">
          <div style="text-align:right">
            <span style="font-size:16px;font-weight:700;color:${{color}}">$${{s.toFixed(0)}}</span>
            <span style="font-size:12px;color:#333"> / </span>
            ${{targetLabel}}
            ${{over ? `<span style="font-size:11px;color:#f76e6e;margin-left:8px">+$${{overAmt.toFixed(0)}} over</span>` : ''}}
            ${{proj && !over ? `<span style="font-size:11px;color:#555;margin-left:8px">→ $${{proj.toFixed(0)}}/mois</span>` : ''}}
          </div>
          <button onclick="deleteCat('${{cat}}')" style="background:none;border:none;color:#333;cursor:pointer;font-size:16px;padding:0;line-height:1;flex-shrink:0" onmouseover="this.style.color='#f76e6e'" onmouseout="this.style.color='#333'">✕</button>
        </div>
      </div>
      <div style="background:#1a1a1a;border-radius:6px;height:10px;overflow:hidden">
        <div style="width:${{pct}}%;background:${{color}};height:100%;border-radius:6px;transition:width .4s ease"></div>
      </div>
    </div>`;
  }}).join('');

  document.getElementById('budget-bars').innerHTML = bars || '<p style="color:#555">Aucune dépense dans cette période.</p>';

  // Restaurer cachées
  let restore = document.getElementById('budget-restore');
  if (!restore) {{ restore = document.createElement('div'); restore.id='budget-restore'; document.getElementById('budget-bars').parentElement.appendChild(restore); }}
  const hc = hidden.length;
  restore.innerHTML = hc > 0 ? `<button onclick="restoreHidden()" style="background:none;border:none;color:#444;font-size:12px;font-family:Montserrat;cursor:pointer;margin-top:8px">↩ Restaurer ${{hc}} catégorie${{hc>1?'s':''}} masquée${{hc>1?'s':''}}</button>` : '';

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

  const suggHTML = suggs.map(s => {{
    const icon = s.type==='warning'?'⚠️':s.type==='good'?'✅':'💡';
    const clr  = s.type==='warning'?'#f76e6e':s.type==='good'?'#4fc978':'#93c5fd';
    const bg   = s.type==='warning'?'rgba(247,110,110,0.06)':s.type==='good'?'rgba(79,201,120,0.06)':'rgba(147,197,253,0.06)';
    const bdr  = s.type==='warning'?'rgba(247,110,110,0.2)':s.type==='good'?'rgba(79,201,120,0.2)':'rgba(147,197,253,0.2)';
    return `<div style="display:flex;gap:12px;align-items:flex-start;background:${{bg}};border:1px solid ${{bdr}};border-radius:10px;padding:14px 16px">
      <span style="font-size:16px;flex-shrink:0;margin-top:1px">${{icon}}</span>
      <div><div style="font-size:13px;font-weight:600;color:${{clr}}">${{s.msg}}</div>${{s.impact?`<div style="font-size:12px;color:#555;margin-top:4px">${{s.impact}}</div>`:''}}</div>
    </div>`;
  }}).join('');

  const badgesHTML = catsWithTarget.length > 0 ? `<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:16px">${{
    Object.entries(catResults).map(([cat,res]) => {{
      const c  = res.status==='ace'?'#4fc978':res.status==='ok'?'#93c5fd':res.status==='over'?'#f59e0b':'#f76e6e';
      const ic = res.status==='ace'?'🔥':res.status==='ok'?'✓':res.status==='over'?'↑':'✕';
      return `<span style="display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:600;padding:4px 10px;border-radius:20px;background:${{c}}18;color:${{c}};border:1px solid ${{c}}30">${{ic}} ${{cat}}</span>`;
    }}).join('')
  }}</div>` : '';

  suggEl.innerHTML = `
    <div style="margin-top:28px;border-top:1px solid #1e1e1e;padding-top:24px">
      <div style="display:flex;align-items:center;gap:28px;margin-bottom:24px;flex-wrap:wrap">
        <div style="position:relative;width:160px;height:160px;flex-shrink:0">
          <svg width="160" height="160" viewBox="0 0 160 160" style="transform:rotate(-90deg)">
            <circle cx="80" cy="80" r="${{R}}" fill="none" stroke="#1e1e1e" stroke-width="14"/>
            <circle cx="80" cy="80" r="${{R}}" fill="none" stroke="${{rl}}" stroke-width="14"
              stroke-dasharray="${{CIRC.toFixed(1)}}" stroke-dashoffset="${{fillOff.toFixed(1)}}"
              stroke-linecap="round" style="transition:stroke-dashoffset .8s cubic-bezier(.4,0,.2,1)"/>
          </svg>
          <div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px">
            <div style="font-size:38px;font-weight:800;color:${{gc}};line-height:1">${{grade}}</div>
            <div style="font-size:11px;color:#555;letter-spacing:.04em">${{score!==null?score+' / 100':''}}</div>
          </div>
        </div>
        <div style="flex:1;min-width:180px">
          <div style="font-size:22px;font-weight:800;color:${{gc}};margin-bottom:2px">${{rc2}}</div>
          ${{catsWithTarget.length>0
            ? `<div style="font-size:13px;color:#555;margin-bottom:14px">${{catsOk}}/${{catsWithTarget.length}} objectifs atteints</div>`
            : '<div style="font-size:13px;color:#444;margin-bottom:14px">Fixe des cibles pour débloquer le score</div>'
          }}
          ${{badgesHTML}}
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:${{suggs.length>0?'20px':'0'}};background:#141414;border:1px solid #222;border-radius:10px;padding:12px 16px">
        <span style="font-size:12px;color:#666;text-transform:uppercase;letter-spacing:.05em;white-space:nowrap">Revenu net /mois</span>
        <input id="income-input" type="number" value="${{savedIncome||''}}" placeholder="ex: 4500"
          oninput="(function(v){{const n=parseFloat(v)||0;localStorage.setItem('finance_monthly_income',n);renderBudget(getFilteredTxns(currentDays));}})(this.value)"
          style="flex:1;max-width:120px;background:transparent;border:none;font-family:Montserrat;font-size:14px;font-weight:700;color:#f1f1f1;outline:none"/>
        <span style="font-size:12px;color:#444">CAD</span>
      </div>
      ${{suggs.length>0?`<div style="font-size:11px;font-weight:700;color:#444;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px">Suggestions</div><div style="display:flex;flex-direction:column;gap:8px">${{suggHTML}}</div>`:''}}
    </div>`;
}}

// ── Transactions tab ──────────────────────────────────────────────────────────
function initTxns() {{
  const acctSel = document.getElementById('txn-acct');
  const catSel  = document.getElementById('txn-cat');
  const accts   = [...new Set(ALL_TXNS.map(t => t.account))];
  accts.forEach(a => {{ const o=document.createElement('option'); o.value=a; o.textContent=a; acctSel.appendChild(o); }});
  const cats = [...new Set(ALL_TXNS.map(t => OVERRIDES[t.id]||t.category))].sort();
  cats.forEach(c => {{ const o=document.createElement('option'); o.value=c; o.textContent=c; catSel.appendChild(o); }});
  renderTxns('','','');
  renderRules();
}}

function renderTxns(filter='', acct='', cat='') {{
  const q    = filter.toLowerCase();
  const pool = getFilteredTxns(currentDays);   // respect period filter
  const rows = pool.filter(t => {{
    const ec = OVERRIDES[t.id]||t.category;
    const en = NAMES[t.id]||t.name;
    if (acct && t.account !== acct) return false;
    if (cat  && ec !== cat)          return false;
    if (q && !en.toLowerCase().includes(q) && !ec.toLowerCase().includes(q)) return false;
    return true;
  }});
  document.getElementById('txn-tbody').innerHTML = rows.map(t => {{
    const ec  = OVERRIDES[t.id]||t.category;
    const en  = NAMES[t.id]||t.name;
    const cls = t.amount < 0 ? 'green' : 'red';
    const amt = t.amount < 0 ? `+$${{Math.abs(t.amount).toFixed(2)}}` : `-$${{t.amount.toFixed(2)}}`;
    const ns  = NAMES[t.id] ? 'color:#93c5fd;' : '';
    return `<tr>
      <td>${{t.date}}</td>
      <td><span onclick="editName('${{t.id}}',this)" style="cursor:pointer;${{ns}}" title="Cliquer pour renommer">${{en}}</span></td>
      <td><span class="badge" onclick="editCat('${{t.id}}','${{ec}}',this)">${{ec}}</span></td>
      <td style="color:#777;font-size:0.8rem">${{t.account}}</td>
      <td class="${{cls}}" style="text-align:right">${{amt}}</td>
    </tr>`;
  }}).join('');
  document.getElementById('txn-count').textContent = `${{rows.length}} transactions`;
}}

function editName(id, el) {{
  const current = NAMES[id] || ALL_TXNS.find(t=>t.id===id)?.name || '';
  const inp = document.createElement('input');
  inp.value = current;
  inp.style.cssText = 'background:#111;border:1px solid #2563eb;border-radius:4px;padding:2px 6px;color:#f1f1f1;font-family:Montserrat;font-size:13px;width:200px';
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
  const allCats = [...new Set(ALL_TXNS.map(t => OVERRIDES[t.id]||t.category))].sort();
  const sel = document.createElement('select');
  sel.style.cssText = 'background:#111;border:1px solid #2563eb;border-radius:4px;padding:2px 6px;color:#93c5fd;font-family:Montserrat;font-size:11px;font-weight:600';
  allCats.forEach(c => {{ const o=document.createElement('option'); o.value=c; o.textContent=c; if(c===currentCat) o.selected=true; sel.appendChild(o); }});
  el.replaceWith(sel); sel.focus();
  let committed = false;
  function commit() {{
    if (committed) return; committed = true;
    const val = sel.value;
    if (val === currentCat) {{
      renderTxns(document.getElementById('txn-search')?.value||'',document.getElementById('txn-acct')?.value||'',document.getElementById('txn-cat')?.value||'');
      return;
    }}
    // Find all txns with same merchant name
    const txn     = ALL_TXNS.find(t => t.id === id);
    const merchant = txn ? (NAMES[txn.id] || txn.name) : null;
    const matches  = merchant ? ALL_TXNS.filter(t => (NAMES[t.id]||t.name) === merchant) : [];
    OVERRIDES[id] = val;
    if (matches.length > 1) {{
      showBulkToast(merchant, val, matches, currentCat, id);
    }} else {{
      localStorage.setItem('catOverrides', JSON.stringify(OVERRIDES));
      renderTxns(document.getElementById('txn-search')?.value||'',document.getElementById('txn-acct')?.value||'',document.getElementById('txn-cat')?.value||'');
      if (initializedTabs.has('budget')) renderBudget(getFilteredTxns(currentDays));
    }}
  }}
  sel.addEventListener('change', commit); sel.addEventListener('blur', commit);
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
    box-shadow:0 8px 40px rgba(0,0,0,.7); font-family:Montserrat,sans-serif;
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
      <button id="bulk-yes" style="flex:1;padding:9px;background:#1e3a8a;border:none;border-radius:8px;color:#fff;font-family:Montserrat;font-size:13px;font-weight:600;cursor:pointer">
        ✓ Appliquer aux ${{matches.length}}
      </button>
      <button id="bulk-no" style="padding:9px 16px;background:#181818;border:1px solid #333;border-radius:8px;color:#777;font-family:Montserrat;font-size:13px;cursor:pointer">
        Juste celle-ci
      </button>
      <button id="bulk-cancel" style="padding:9px 16px;background:#181818;border:1px solid #333;border-radius:8px;color:#777;font-family:Montserrat;font-size:13px;cursor:pointer">
        ✕
      </button>
    </div>
  `;

  document.body.appendChild(toast);

  function save() {{
    localStorage.setItem('catOverrides', JSON.stringify(OVERRIDES));
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
    panel.innerHTML = '<p style="color:#444;font-size:13px">Aucune règle — change la catégorie d\'une transaction pour en créer une.</p>';
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

// ── Refresh all ───────────────────────────────────────────────────────────────
function refreshAll() {{
  const txns = getFilteredTxns(currentDays);
  renderMonthly(txns); renderDonut(txns); renderWeekly(txns);
  if (initializedTabs.has('budget')) renderBudget(txns);
  if (initializedTabs.has('txns'))   renderTxns(
    document.getElementById('txn-search')?.value||'',
    document.getElementById('txn-acct')?.value||'',
    document.getElementById('txn-cat')?.value||''
  );
}}

window.addEventListener('DOMContentLoaded', () => refreshAll());

// ── Refresh button ────────────────────────────────────────────────────────────
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
        // Poll until done
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
      // No server running — open same file (no-op graceful)
      btn.disabled = false; btn.style.opacity = '1';
      icon.style.animation = '';
    }});
}}
</script>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    check_config()
    print("\n📊 Budget Dashboard — génération en cours...\n")
    print("🔗 Connexion aux APIs...")
    balances = get_plaid_balances()
    wise_bal = get_wise_balances()
    sol_bal, sol_usd = get_phantom_balance()
    print("\n📥 Transactions Plaid...")
    raw_txns = get_plaid_transactions()
    print("\n⚙️  Traitement...")
    txns = process_transactions(raw_txns)
    print(f"  {len(txns)} transactions après filtrage")
    print("\n🎨 Génération HTML...")
    html = build_html(balances, wise_bal, sol_bal, sol_usd, txns)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✅ Dashboard → {OUTPUT_PATH}\n")
    if "--no-open" not in sys.argv:
        webbrowser.open(f"file://{OUTPUT_PATH}")
