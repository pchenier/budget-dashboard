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

        # Inject le bouton refresh + banner dans le HTML
        html = inject_server_ui(html)

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


def inject_server_ui(html: str) -> str:
    """Injecte le bouton Refresh + badge dans le header HTML."""
    next_refresh = datetime.now() + timedelta(days=REFRESH_DAYS)
    next_str = next_refresh.strftime("%d %b %Y")
    generated_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    refresh_ui = f"""
  <style>
    .refresh-btn {{
      display: inline-flex; align-items: center; gap: 6px;
      padding: 8px 16px; background: rgba(79,134,247,.15);
      border: 1px solid rgba(79,134,247,.4); border-radius: 8px;
      color: #4f86f7; font-size: 13px; font-weight: 600;
      cursor: pointer; text-decoration: none; transition: all .2s;
    }}
    .refresh-btn:hover {{ background: rgba(79,134,247,.3); }}
    .refresh-btn.loading {{ opacity: .6; pointer-events: none; }}
    .next-refresh {{ font-size: 11px; color: #666; margin-top: 4px; text-align: right; }}
    .stale-banner {{
      background: rgba(247,201,72,.1); border-bottom: 1px solid rgba(247,201,72,.3);
      padding: 10px 32px; font-size: 13px; color: #f7c948;
      display: flex; justify-content: space-between; align-items: center;
    }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    .spinning {{ display: inline-block; animation: spin 1s linear infinite; }}
  </style>"""

    # Injecte styles dans <head>
    html = html.replace("</head>", refresh_ui + "\n</head>", 1)

    # Remplace le header existant pour ajouter le bouton
    old_header_end = """  <span class="gen">Généré le """ + generated_str + """</span>
</header>"""
    new_header_end = f"""  <div style="text-align:right">
    <a class="refresh-btn" id="refresh-btn" onclick="doRefresh(event)">
      <span id="refresh-icon">🔄</span> Refresh
    </a>
    <div class="next-refresh">Auto-refresh: {next_str}</div>
  </div>
</header>"""

    html = html.replace(old_header_end, new_header_end, 1)

    # Ajoute le script refresh
    refresh_script = f"""
<script>
// ── Auto-stale check ──────────────────────────────────────────────────────────
(function() {{
  const REFRESH_MS = {REFRESH_DAYS} * 24 * 60 * 60 * 1000;
  const generated = new Date("{generated_str.replace(' ', 'T')}");
  const age = Date.now() - generated.getTime();
  if (age > REFRESH_MS) {{
    const banner = document.createElement('div');
    banner.className = 'stale-banner';
    banner.innerHTML = `⚠️ Dashboard généré il y a ${{Math.floor(age/86400000)}} jours — données possiblement périmées.
      <a class="refresh-btn" onclick="doRefresh(event)" style="padding:6px 12px; font-size:12px">🔄 Refresh maintenant</a>`;
    document.querySelector('.tabs').before(banner);
  }}
}})();

// ── Refresh function ──────────────────────────────────────────────────────────
function doRefresh(e) {{
  e.preventDefault();
  const btn = document.getElementById('refresh-btn');
  if (btn) {{ btn.classList.add('loading'); document.getElementById('refresh-icon').className = 'spinning'; document.getElementById('refresh-icon').textContent = '⟳'; }}
  fetch('/refresh')
    .then(r => r.json())
    .then(d => {{
      if (d.ok) {{ setTimeout(() => location.reload(), 500); }}
      else {{ alert('Erreur: ' + d.msg); if(btn) btn.classList.remove('loading'); }}
    }})
    .catch(() => {{ alert('Serveur non disponible — lance python server.py'); if(btn) btn.classList.remove('loading'); }});
}}
</script>"""

    html = html.replace("</body>", refresh_script + "\n</body>", 1)
    return html


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
                self.send_header("Content-Length", len(content))
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
