#!/usr/bin/env python3
"""
Budget Local — Serveur local avec auto-refresh.

Usage:
    python server.py          # démarre sur localhost:8766
    python server.py --port 9000

Le dashboard se régénère automatiquement si :
  • C'est la première fois (pas de dashboard.html)
  • Le dashboard a plus de 7 jours
  • Tu cliques le bouton Refresh dans l'interface
"""

import os, sys, threading, time, webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from pathlib import Path

# ── Imports depuis generate.py ────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
from generate import (
    check_config, get_plaid_balances, get_plaid_transactions,
    get_wise_balances, get_phantom_balance, process_transactions,
    build_html, OUTPUT_PATH
)

REFRESH_DAYS = 7
PORT = 8766

_refresh_lock = threading.Lock()
_refresh_status = {"running": False, "last_msg": "", "last_run": None}


def get_dashboard_age():
    """Retourne l'âge du dashboard en jours, ou None si pas encore généré."""
    p = Path(OUTPUT_PATH)
    if not p.exists():
        return None
    mtime = datetime.fromtimestamp(p.stat().st_mtime)
    return (datetime.now() - mtime).total_seconds() / 86400


def run_generate(force=False):
    """Régénère le dashboard. Thread-safe."""
    with _refresh_lock:
        if _refresh_status["running"]:
            return False, "Refresh déjà en cours..."

        age = get_dashboard_age()
        if not force and age is not None and age < REFRESH_DAYS:
            return False, f"Dashboard à jour ({age:.1f}j < {REFRESH_DAYS}j)"

        _refresh_status["running"] = True
        _refresh_status["last_msg"] = "En cours..."

    try:
        print("\n🔄 Régénération du dashboard...")
        balances = get_plaid_balances()
        wise_bal = get_wise_balances()
        sol_bal, sol_usd = get_phantom_balance()
        raw_txns = get_plaid_transactions()
        txns = process_transactions(raw_txns)
        html = build_html(balances, wise_bal, sol_bal, sol_usd, txns)

        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            f.write(html)

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        msg = f"Régénéré le {now}"
        _refresh_status["last_msg"] = msg
        _refresh_status["last_run"] = datetime.now()
        print(f"✅ Dashboard régénéré → {OUTPUT_PATH}\n")
        return True, msg
    except Exception as e:
        msg = f"Erreur: {e}"
        _refresh_status["last_msg"] = msg
        print(f"❌ {msg}")
        return False, msg
    finally:
        _refresh_status["running"] = False




# ── HTTP Handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Silence les logs sauf erreurs
        if int(args[1]) >= 400:
            print(f"  [{args[1]}] {args[0]}")

    def do_GET(self):
        if self.path == "/" or self.path == "/dashboard.html":
            # Auto-refresh si stale
            age = get_dashboard_age()
            if age is None or age >= REFRESH_DAYS:
                print(f"  Auto-refresh ({age:.1f}j >= {REFRESH_DAYS}j)" if age else "  Premier run")
                run_generate(force=True)

            try:
                with open(OUTPUT_PATH, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.end_headers()
                self.wfile.write(content)
            except FileNotFoundError:
                self._text(500, "Dashboard pas encore généré")

        elif self.path == "/refresh":
            # Refresh forcé via bouton
            ok, msg = run_generate(force=True)
            import json
            body = json.dumps({"ok": ok, "msg": msg}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/status":
            import json
            age = get_dashboard_age()
            body = json.dumps({
                "running":   _refresh_status["running"],
                "last_msg":  _refresh_status["last_msg"],
                "age_days":  round(age, 2) if age is not None else None,
                "stale":     age is None or age >= REFRESH_DAYS,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        else:
            self._text(404, "Not found")

    def _text(self, code, msg):
        body = msg.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    check_config()

    # Parse port
    for arg in sys.argv[1:]:
        if arg.startswith("--port="):
            PORT = int(arg.split("=")[1])
        elif arg == "--port" and sys.argv.index(arg) + 1 < len(sys.argv):
            PORT = int(sys.argv[sys.argv.index(arg) + 1])

    url = f"http://localhost:{PORT}"
    print(f"\n💰 Budget Local Server")
    print(f"   URL       : {url}")
    print(f"   Refresh   : automatique après {REFRESH_DAYS} jours")
    print(f"   Dashboard : {OUTPUT_PATH}")

    age = get_dashboard_age()
    if age is None:
        print(f"\n  Premier run — génération du dashboard...")
    elif age >= REFRESH_DAYS:
        print(f"\n  Dashboard a {age:.1f} jours — refresh automatique au chargement")
    else:
        print(f"\n  Dashboard à jour ({age:.1f}j) — chargement direct")

    print(f"\n  Ctrl+C pour arrêter\n")

    server = HTTPServer(("localhost", PORT), Handler)
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\n  Serveur arrêté.")
