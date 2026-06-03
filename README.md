# 💰 Budget Dashboard

Dashboard financier personnel — Plaid, Wise, Solana. Tout tourne local ou sur Railway/Render.

## Setup en 5 minutes

### 1. Clone + install

```bash
git clone <repo-url>
cd budget-local
pip install -r requirements.txt
```

### 2. Configure tes clés

```bash
cp .env.example .env
```

Ouvre `.env` et remplis tes clés:

**Plaid (obligatoire)**
- Crée un compte sur [dashboard.plaid.com](https://dashboard.plaid.com)
- Team Settings → Keys → copie ton `Client ID` et `Secret`
- Pour ton `Access Token`, roule `python setup_plaid.py` et suis les instructions

**Wise (optionnel)**
- [wise.com/settings/account](https://wise.com/settings/account) → API tokens → crée un token Read-only
- Ton Profile ID est visible dans l'URL quand tu es connecté

**Phantom / Solana (optionnel)**
- Juste ton adresse publique Solana (read-only, aucun risque)

### 3. Lance l'app

```bash
python app.py
```

Ouvre [http://localhost:5050](http://localhost:5050) — une page de setup s'affiche pour entrer tes clés directement dans le browser (rien n'est sauvegardé côté serveur).

---

## Deploy sur Railway (gratuit)

1. Push sur GitHub
2. Nouveau projet Railway → Deploy from GitHub repo
3. Variables d'env: ajoute `SECRET_KEY` (une string random)
4. Les autres clés se remplissent via la page `/setup` dans le browser

---

## Stack

- Python + Flask
- Plaid API (comptes bancaires)
- Wise API (transfers internationaux)
- Solana RPC (balance Phantom)
- Chart.js + design #0c0c0c / #b8f566
