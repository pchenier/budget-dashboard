#!/usr/bin/env python3
"""
Plaid Link Flow — obtenir ton access_token une seule fois.
Lance ce script, ça ouvre un mini serveur web local + browser.
Complète le flow Plaid Link, copie l'access_token dans ton .env.

Usage:
    python setup_plaid.py
"""

import os, json, threading, webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import requests
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("PLAID_CLIENT_ID")
SECRET    = os.getenv("PLAID_SECRET")
ENV       = os.getenv("PLAID_ENV", "production")

BASE = "https://production.plaid.com" if ENV == "production" else "https://sandbox.plaid.com"

# 1. Créer un link_token
def create_link_token():
    r = requests.post(f"{BASE}/link/token/create", json={
        "client_id": CLIENT_ID,
        "secret":    SECRET,
        "client_name": "Budget Local",
        "country_codes": ["CA", "US"],
        "language": "fr",
        "user": {"client_user_id": "local-user"},
        "products": ["transactions"],
    })
    r.raise_for_status()
    return r.json()["link_token"]

# 2. Échanger public_token → access_token
def exchange_token(public_token):
    r = requests.post(f"{BASE}/item/public_token/exchange", json={
        "client_id": CLIENT_ID,
        "secret":    SECRET,
        "public_token": public_token,
    })
    r.raise_for_status()
    return r.json()["access_token"]

ACCESS_TOKEN_RESULT = []

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass  # silence logs

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            link_token = create_link_token()
            html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Budget Local — Connexion Plaid</title>
  <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
  <style>
    body {{ font-family: sans-serif; display:flex; flex-direction:column;
           align-items:center; justify-content:center; height:100vh; margin:0;
           background:#111; color:#fff; }}
    button {{ padding:16px 32px; font-size:18px; background:#4f86f7;
              color:#fff; border:none; border-radius:8px; cursor:pointer; }}
    button:hover {{ background:#2a5fd6; }}
    #status {{ margin-top:20px; font-size:14px; color:#aaa; }}
  </style>
</head>
<body>
  <h2>Budget Local</h2>
  <p>Connecte ta banque via Plaid pour générer ton dashboard.</p>
  <button onclick="openPlaid()">Connecter ma banque</button>
  <div id="status"></div>
  <script>
    function openPlaid() {{
      var handler = Plaid.create({{
        token: '{link_token}',
        onSuccess: function(public_token, metadata) {{
          document.getElementById('status').innerText = 'Connexion réussie! Ferme pas cette page...';
          fetch('/callback?public_token=' + public_token)
            .then(r => r.text())
            .then(msg => {{
              document.getElementById('status').innerHTML = msg;
            }});
        }},
        onExit: function(err) {{
          if (err) document.getElementById('status').innerText = 'Erreur: ' + err.error_message;
        }},
      }});
      handler.open();
    }}
  </script>
</body>
</html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())

        elif parsed.path == "/callback":
            qs = parse_qs(parsed.query)
            pub = qs.get("public_token", [None])[0]
            if pub:
                try:
                    access_token = exchange_token(pub)
                    ACCESS_TOKEN_RESULT.append(access_token)
                    msg = f"""
<div style='font-family:monospace; text-align:center'>
  <p style='color:#4fc978; font-size:18px'>✅ Succès!</p>
  <p>Copie ce token dans ton fichier <code>.env</code> :</p>
  <p style='background:#222; padding:12px; border-radius:6px; word-break:break-all'>
    PLAID_ACCESS_TOKEN={access_token}
  </p>
  <p style='color:#aaa; font-size:13px'>Le terminal a aussi imprimé le token. Tu peux fermer cette page.</p>
</div>"""
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(msg.encode())
                    print(f"\n✅ PLAID_ACCESS_TOKEN={access_token}\n")
                    print("Ajoute ça dans ton .env puis relance generate.py")
                    # Shutdown server after 3s
                    threading.Timer(3, server.shutdown).start()
                except Exception as e:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(f"Erreur: {e}".encode())
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Pas de public_token")
        else:
            self.send_response(404)
            self.end_headers()

if __name__ == "__main__":
    if not CLIENT_ID or CLIENT_ID == "xxxxxxxxxxxxxxxxxxxxxxxx":
        print("❌ Configure ton .env d'abord! (PLAID_CLIENT_ID + PLAID_SECRET)")
        exit(1)

    server = HTTPServer(("localhost", 8765), Handler)
    url = "http://localhost:8765"
    print(f"\n🚀 Serveur démarré → {url}")
    print("Ouverture du browser dans 1 seconde...\n")
    threading.Timer(1, lambda: webbrowser.open(url)).start()
    server.serve_forever()
