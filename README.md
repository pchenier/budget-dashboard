# 💰 Budget Local Dashboard

Dashboard financier local — Plaid + Wise + Phantom (Solana). Aucun serveur, aucune base de données. Tout tourne sur ta machine, les données ne sortent jamais.

---

## Ce que ça fait

- Pulls tes balances bancaires via **Plaid** (Scotia, Tangerine, TD, etc.)
- Pulls ton **Wise** (USD + CAD)
- Affiche ton **Phantom wallet** (SOL balance en temps réel)
- Génère un dashboard HTML offline dans `dashboard.html`
- 4 tabs : Vue Générale, Budget mensuel, Comptes, Transactions (searchable)

---

## Installation (5 minutes)

### 1. Python 3.9+
Vérifie que t'as Python :
```bash
python3 --version
```
Si pas installé → https://www.python.org/downloads/

### 2. Install les dépendances
```bash
cd budget-local
pip install -r requirements.txt
```

### 3. Configure tes clés API
```bash
cp .env.example .env
```
Ouvre `.env` dans un éditeur et remplis les valeurs (voir ci-dessous).

### 4. Connecte ta banque (Plaid — une seule fois)
```bash
python setup_plaid.py
```
- Ça ouvre un browser sur `localhost:8765`
- Clique "Connecter ma banque", choisis ta banque, login
- Le terminal va afficher ton `PLAID_ACCESS_TOKEN`
- Copie-le dans ton `.env` à la ligne `PLAID_ACCESS_TOKEN=`

### 5. Lance le dashboard !
```bash
python generate.py
```
Le browser s'ouvre automatiquement avec ton dashboard.

---

## Obtenir les clés API

### Plaid (banque)
1. Crée un compte sur https://dashboard.plaid.com
2. Va dans **Team Settings → Keys**
3. Copie **Client ID** et **Secret** (choisis Production si ta banque est réelle, Sandbox pour tester)
4. Dans `.env` :
   ```
   PLAID_CLIENT_ID=xxxxxxxx
   PLAID_SECRET=xxxxxxxx
   PLAID_ENV=production
   ```
5. Lance `python setup_plaid.py` pour obtenir ton `PLAID_ACCESS_TOKEN`

> ⚠️ **Plaid Production** requiert une approbation. Pour tester rapidement, utilise `PLAID_ENV=sandbox` — ça simule de fausses transactions.

### Wise
1. Va sur https://wise.com/settings/account → **API tokens**
2. Crée un token **Read-only** (c'est suffisant)
3. Ton **Profile ID** se trouve dans l'URL de ton profil Wise ou via :
   ```bash
   curl -H "Authorization: Bearer TON_TOKEN" https://api.wise.com/v1/profiles
   ```
   Note le champ `id`
4. Dans `.env` :
   ```
   WISE_TOKEN=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   WISE_PROFILE_ID=12345678
   ```

> Si t'as pas de Wise, laisse ces valeurs vides — ça skip automatiquement.

### Phantom (Solana)
1. Ouvre ton wallet Phantom
2. Copie ton **adresse publique** (commence par une lettre/chiffre, ~44 chars)
3. Dans `.env` :
   ```
   PHANTOM_WALLET=TonAdressePublique...
   ```

> C'est **read-only** — on fait juste appeler l'API publique Solana, aucun accès à ton wallet.

---

## Utilisation quotidienne

### Mode serveur (recommandé — bouton refresh + auto-refresh)
```bash
python server.py
```
- Ouvre `http://localhost:8766` dans le browser
- **Bouton Refresh** dans le header pour regénérer à la demande
- **Auto-refresh automatique** si le dashboard a plus de 7 jours
- **Banner d'avertissement** si les données sont périmées

```bash
# Port custom
python server.py --port 9000
```

### Mode one-shot (sans serveur)
```bash
# Génère + ouvre le browser
python generate.py

# Génère seulement
python generate.py --no-open
```

> `generate.py` génère un fichier HTML standalone. `server.py` ajoute le bouton Refresh et l'auto-refresh par dessus.

---

## Structure du projet

```
budget-local/
├── .env.example      ← template config
├── .env              ← tes clés (NE PAS committer sur GitHub!)
├── requirements.txt
├── setup_plaid.py    ← flow one-time pour obtenir access_token
├── generate.py       ← génération standalone (one-shot)
├── server.py         ← serveur local avec bouton refresh + auto-7j
├── dashboard.html    ← dashboard généré (créé après le premier run)
└── README.md
```

---

## 🚀 Deploy sur Railway (pour partager avec tes friends)

L'app Flask (`app.py`) permet à chaque user d'entrer **ses propres clés API** via une page setup — rien n'est sauvegardé côté serveur.

### 1. Push sur GitHub
```bash
cd budget-local
git init
git add .
git commit -m "init"
gh repo create budget-app --private --push
```

### 2. Deploy sur Railway
1. Va sur [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
2. Sélectionne ton repo `budget-app`
3. Ajoute une variable d'environnement :
   ```
   SECRET_KEY=une-longue-chaine-aleatoire-ici
   ```
4. Railway détecte le `Procfile` automatiquement → Deploy

### 3. Partage le lien
Tes friends vont sur `https://ton-app.railway.app` → page setup → entrent leurs clés Plaid/Wise/Phantom → dashboard généré en ~30 secondes.

### Notes
- Les clés sont stockées **uniquement dans la session Flask** (cookie chiffré par `SECRET_KEY`)
- Le dashboard est **caché 7 jours** en mémoire serveur par user — bouton Refresh pour forcer
- Railway Free tier = 500h/mois (suffisant pour usage perso)
- Alternative : [Render.com](https://render.com) fonctionne aussi avec le même `Procfile`

---


**Mes données sont-elles envoyées quelque part ?**
Non. Le script tourne 100% local. Il appelle seulement les APIs officielles (Plaid, Wise, Solana RPC, CoinGecko pour le prix SOL).

**Est-ce que je dois relancer `setup_plaid.py` à chaque fois ?**
Non. Le flow Plaid Link est **une seule fois**. Ton `access_token` dans `.env` est permanent (sauf si tu déconnectes l'item dans Plaid).

**Plaid dit "application not approved" ?**
En mode Production, Plaid requiert une approval. Utilise `PLAID_ENV=sandbox` pour tester avec de fausses données.

**Comment ajouter une autre banque ?**
Relance `setup_plaid.py` — mais Plaid sandbox/production basic permet généralement **une seule institution** par access_token. Pour plusieurs banques, il faut plusieurs access_tokens (un par institution).

**Comment customiser les catégories ?**
Ouvre `generate.py` et modifie la liste `CATEGORY_RULES` — c'est des keywords simples.

---

## .gitignore recommandé

Si tu mets ça sur GitHub, crée un `.gitignore` :
```
.env
dashboard.html
__pycache__/
*.pyc
```
