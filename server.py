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

import os, sys, subprocess, threading, webbrowser, json
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from pathlib import Path

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH  = os.path.join(SCRIPT_DIR, "dashboard.html")
PYTHON       = sys.executable   # même interpréteur que le serveur
GENERATE     = os.path.join(SCRIPT_DIR, "generate.py")

REFRESH_DAYS = 7
PORT         = 8766

_refresh_lock   = threading.Lock()
_refresh_status = {"running": False, "last_msg": "", "last_run": None}


def get_dashboard_age():
    p = Path(OUTPUT_PATH)
    if not p.exists():
        return None
    return (datetime.now() - datetime.fromtimestamp(p.stat().st_mtime)).total_seconds() / 86400


def run_generate(force=False):
    """Lance generate.py en subprocess — toujours le code le plus récent."""
    with _refresh_lock:
        if _refresh_status["running"]:
            return False, "Refresh déjà en cours..."
        age = get_dashboard_age()
        if not force and age is not None and age < REFRESH_DAYS:
            return False, f"Dashboard à jour ({age:.1f}j)"
        _refresh_status["running"] = True
        _refresh_status["last_msg"] = "En cours..."

    def _do():
        try:
            print("\n🔄 Régénération du dashboard...")
            result = subprocess.run(
                [PYTHON, GENERATE, "--no-open"],
                cwd=SCRIPT_DIR,
                capture_output=False,
                timeout=300,
            )
            if result.returncode == 0:
                now = datetime.now().strftime("%Y-%m-%d %H:%M")
                msg = f"Régénéré le {now}"
                _refresh_status["last_run"] = datetime.now()
                print(f"✅ Dashboard régénéré\n")
            else:
                msg = f"Erreur generate.py (code {result.returncode})"
                print(f"❌ {msg}")
            _refresh_status["last_msg"] = msg
        except subprocess.TimeoutExpired:
            msg = "Timeout — génération trop longue"
            _refresh_status["last_msg"] = msg
            print(f"❌ {msg}")
        except Exception as e:
            msg = f"Erreur: {e}"
            _refresh_status["last_msg"] = msg
            print(f"❌ {msg}")
        finally:
            _refresh_status["running"] = False

    threading.Thread(target=_do, daemon=True).start()
    return True, "Refresh lancé en arrière-plan..."


# ── HTTP Handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        try:
            if int(args[1]) >= 400:
                print(f"  [{args[1]}] {args[0]}")
        except (IndexError, ValueError):
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_POST(self):
        if self.path == "/refresh":
            ok, msg = run_generate(force=True)
            body = json.dumps({"ok": ok, "msg": msg}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        else:
            self._text(404, "Not found")

    def do_GET(self):
        if self.path in ("/", "/dashboard.html"):
            age = get_dashboard_age()
            if age is None or age >= REFRESH_DAYS:
                print(f"  Auto-refresh ({age:.1f}j)" if age else "  Premier run")
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

        elif self.path == "/status":
            age = get_dashboard_age()
            body = json.dumps({
                "running":  _refresh_status["running"],
                "last_msg": _refresh_status["last_msg"],
                "age_days": round(age, 2) if age is not None else None,
                "stale":    age is None or age >= REFRESH_DAYS,
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
        run_generate(force=True)
    elif age >= REFRESH_DAYS:
        print(f"\n  Dashboard a {age:.1f} jours — refresh automatique")
        run_generate(force=True)
    else:
        print(f"\n  Dashboard à jour ({age:.1f}j) — chargement direct")

    print(f"\n  Ctrl+C pour arrêter\n")

    server = HTTPServer(("localhost", PORT), Handler)
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Arrêt du serveur.")
