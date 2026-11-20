// Dashboard in French or English.
//
// The dashboard's templates are written in English. Rather than rewrite them, every
// text node and label that reaches the page is looked up in window.FISCIT_FR
// (static/i18n-fr.js), plus a few patterns for text that contains numbers or dates.
// Only what is displayed changes: internal values (category keys, option values,
// rules) stay English, so filters and logic keep working.
//
// Language: the fiscit_lang cookie (shared with fiscit.com through Domain=.fiscit.com),
// else the browser's language.
(function () {
  var m = document.cookie.match(/(?:^|;\s*)fiscit_lang=(fr|en)/);
  var nav = ((navigator.languages && navigator.languages[0]) || navigator.language || 'en').toLowerCase();
  var lang = m ? m[1] : (nav.indexOf('fr') === 0 ? 'fr' : 'en');

  function setLang(l) {
    var c = 'fiscit_lang=' + l + '; Path=/; Max-Age=31536000; SameSite=Lax';
    if (/(^|\.)fiscit\.com$/.test(location.hostname)) c += '; Domain=.fiscit.com';
    document.cookie = c;
    location.reload();
  }

  var api = { lang: lang, locale: lang === 'fr' ? 'fr-CA' : 'en-CA', setLang: setLang, t: function (s) { return s; } };
  window.FiscitI18n = api;
  document.documentElement.lang = lang === 'fr' ? 'fr-CA' : 'en';
  if (lang !== 'fr') return;

  var DICT = window.FISCIT_FR || {};
  // Text added with the language switch itself
  DICT['Language'] = DICT['Language'] || 'Langue';
  DICT['Fiscit in English or French'] = DICT['Fiscit in English or French'] || 'Fiscit en français ou en anglais';
  DICT["That's above the 20% recommended threshold for financial freedom."] = 'C\'est au-dessus du seuil de 20 % recommandé pour bâtir ta liberté financière.';
  DICT['Now'] = DICT['Now'] || 'Maintenant';
  // Loading page (shown while a sync runs) and the dashboard's own sign-in page
  var EXTRA = {
    'Connecting to your accounts': 'Connexion à tes comptes', 'Loading data...': 'Chargement des données…',
    'Plaid: balances...': 'Plaid : soldes…', 'Plaid: transactions...': 'Plaid : transactions…',
    'Wise: balances...': 'Wise : soldes…', 'Wise: transactions...': 'Wise : transactions…',
    'Processing transactions...': 'Traitement des transactions…', 'Log in': 'Connexion',
    'Sign in with Google': 'Se connecter avec Google', 'or': 'ou', 'Your password': 'Ton mot de passe',
    'Forgot your password?': 'Mot de passe oublié?', "Don't have an account?": 'Pas de compte?', 'Sign up': 'Créer un compte',
    'Invalid email or password.': 'Courriel ou mot de passe invalide.',
    'Too many attempts. Please wait 15 minutes and try again.': 'Trop de tentatives. Attends 15 minutes et réessaie.',
    'Please confirm your email first — check your inbox for the link from Fiscit.': 'Confirme d\'abord ton courriel — vérifie ta boîte de réception pour le lien de Fiscit.',
    'Google sign-in failed. Please try again.': 'La connexion avec Google a échoué. Réessaie.',
    'Fiscit — Log In': 'Fiscit — Connexion', 'Fiscit — Loading': 'Fiscit — Chargement',
    // Money assistant
    "Assistant": "Assistant",
    "Ask about your money": "Pose tes questions sur ton argent",
    "Where did my money go this month?": "Où est passé mon argent ce mois-ci?",
    "How can I save $200 a month?": "Comment épargner 200 $ par mois?",
    "Which subscriptions could I cancel?": "Quels abonnements pourrais-je annuler?",
    "Am I on track with my budget?": "Est-ce que je respecte mon budget?",
    "Compare my spending with last month": "Compare mes dépenses avec le mois dernier",
    "How are my investments spread out?": "Comment mes placements sont-ils répartis?",
    "Looking at your overview…": "Je regarde ton portrait financier…",
    "Looking at your spending…": "Je regarde tes dépenses…",
    "Comparing your months…": "Je compare tes mois…",
    "Searching your transactions…": "Je cherche dans tes transactions…",
    "Checking your accounts…": "Je regarde tes comptes…",
    "Checking your budgets…": "Je regarde tes budgets…",
    "Finding recurring charges…": "Je cherche tes paiements récurrents…",
    "Looking at your investments…": "Je regarde tes placements…",
    "Thinking…": "Je réfléchis…",
    "You reached today’s limit of questions. It resets tomorrow.": "Tu as atteint la limite de questions pour aujourd’hui. Elle se réinitialise demain.",
    "The assistant isn’t available yet.": "L’assistant n’est pas encore disponible.",
    "I can’t help with that one. Try asking about your spending, budget or savings.": "Je ne peux pas t’aider avec ça. Essaie une question sur tes dépenses, ton budget ou ton épargne.",
    "The assistant is busy right now. Please try again in a moment.": "L’assistant est occupé en ce moment. Réessaie dans un instant.",
    "Couldn’t reach the assistant. Check your connection and try again.": "Impossible de joindre l’assistant. Vérifie ta connexion et réessaie.",
    "That question needed too many steps. Try asking something more specific.": "Cette question demandait trop d’étapes. Essaie quelque chose de plus précis.",
    "The answer got too long. Try a more specific question.": "La réponse est devenue trop longue. Essaie une question plus précise.",
    "Something went wrong. Please try again.": "Un problème est survenu. Réessaie.",
    "Type a question first.": "Écris d’abord une question.",
    "Ask about your spending, budget or savings…": "Pose une question sur tes dépenses, ton budget ou ton épargne…",
    "Send": "Envoyer",
    "New conversation": "Nouvelle conversation",
    "Answers come from your own accounts. Read-only. General information, not financial advice.": "Les réponses viennent de tes propres comptes. En lecture seule. Information générale, pas un conseil financier.",
    "I look at your real accounts and transactions to answer: where your money goes, how to save, your budget, your investments.": "Je regarde tes vrais comptes et tes transactions pour répondre : où va ton argent, comment épargner, ton budget, tes placements.",
  };
  for (var k in EXTRA) if (!Object.prototype.hasOwnProperty.call(DICT, k)) DICT[k] = EXTRA[k];
  DICT['Alert for transactions over $200'] = 'Alerte pour les transactions de plus de 200 $';
  DICT['Savings rate under 10% this month'] = DICT['Savings rate under 10% this month'] || 'Taux d\'épargne sous 10 % ce mois-ci';
  var MON = { Jan: 'janv.', Feb: 'févr.', Mar: 'mars', Apr: 'avr.', May: 'mai', Jun: 'juin', Jul: 'juil.', Aug: 'août', Sep: 'sept.', Oct: 'oct.', Nov: 'nov.', Dec: 'déc.' };
  var MONTH = { January: 'janvier', February: 'février', March: 'mars', April: 'avril', May: 'mai', June: 'juin', July: 'juillet', August: 'août', September: 'septembre', October: 'octobre', November: 'novembre', December: 'décembre' };
  var DAY = { Monday: 'lundi', Tuesday: 'mardi', Wednesday: 'mercredi', Thursday: 'jeudi', Friday: 'vendredi', Saturday: 'samedi', Sunday: 'dimanche' };
  var plural = function (n, one, many) { return Math.abs(+n) > 1 ? many : one; };
  var tr = function (s) { return translate(s); };

  // Text built from numbers, dates or names: [pattern, French]
  var PATTERNS = [
    [/^(\d+) connected accounts?$/, function (_, n) { return n + ' ' + plural(n, 'compte connecté', 'comptes connectés'); }],
    [/^(\d+) transactions?$/, function (_, n) { return n + ' ' + plural(n, 'transaction', 'transactions'); }],
    [/^Rules \((\d+)\)$/, function (_, n) { return 'Règles (' + n + ')'; }],
    [/^(.+) spent$/, function (_, a) { return a + ' dépensés'; }],
    [/^(\d+)% used$/, function (_, n) { return n + ' % utilisé'; }],
    [/^(\d+) sessions?$/, function (_, n) { return n + ' ' + plural(n, 'séance', 'séances'); }],
    [/^(\d+) days?$/, function (_, n) { return n + ' ' + plural(n, 'jour', 'jours'); }],
    [/^(\d+)d streak$/, function (_, n) { return 'série de ' + n + ' j'; }],
    [/^(\d+) over$/, function (_, n) { return n + ' en dépassement'; }],
    [/^Good morning,$/, function () { return 'Bonjour,'; }],
    [/^Good afternoon,$/, function () { return 'Bon après-midi,'; }],
    [/^Good evening,$/, function () { return 'Bonsoir,'; }],
    [/^Here's where your money stands — (\w+), (\w+) (\d+)\.$/, function (_, d, mo, n) {
      return 'Voici où en est ton argent — ' + (DAY[d] || d) + ' ' + n + ' ' + (MONTH[mo] || mo) + '.'; }],
    [/^Type (.+) to confirm:$/, function (_, e) { return 'Tape ' + e + ' pour confirmer :'; }],
    [/^Detected as ([A-Z]{3})\. Tap the right currency to confirm\.$/, function (_, c) { return 'Devise détectée : ' + c + '. Touche la bonne pour confirmer.'; }],
    [/^Currency of (.+)$/, function (_, a) { return 'Devise de ' + a; }],
    [/^Automatic \(([A-Z]{3}), from your bank\)$/, function (_, c) { return 'Automatique (' + c + ', selon ta banque)'; }],
    [/^No CAD rate for ([A-Z]{3}) yet$/, function (_, c) { return 'Pas encore de taux CAD pour ' + c; }],
    [/^Income (.+) \/ Expenses (.+)$/, function (_, a, b) { return 'Revenus ' + a + ' / Dépenses ' + b; }],
    [/^Excellent — ([\d.,]+)% savings rate this month$/, function (_, p) { return 'Excellent — taux d\'épargne de ' + p.replace('.', ',') + ' % ce mois-ci'; }],
    [/^You saved (.+) out of (.+) of income\. (.*)$/, function (_, a, b, rest) { return 'Tu as épargné ' + a + ' sur ' + b + ' de revenus. ' + tr(rest); }],
    [/^You spent (.+) out of (.+) of income\. Aim for at least 20% to build wealth — that's (.+) saved this month\.$/, function (_, a, b, c) {
      return 'Tu as dépensé ' + a + ' sur ' + b + ' de revenus. Vise au moins 20 % pour t\'enrichir — ce serait ' + c + ' épargnés ce mois-ci.'; }],
    [/^Alert for transactions over \$(\d+)$/, function (_, n) { return 'Alerte pour les transactions de plus de ' + n + ' $'; }],
    [/^(.+) YTD$/, function (_, a) { return a + ' depuis janv.'; }],
    [/^([▲▼–] .+) this month$/, function (_, a) { return a + ' ce mois-ci'; }],
    [/^Synced (.+)$/, function (_, a) { return 'Synchronisé ' + tr(a); }],
    [/^No events on (.+)$/, function (_, a) { return 'Aucun événement le ' + a; }],
    [/^Transactions on (.+)$/, function (_, a) { return 'Transactions du ' + a; }],
    [/^Delete "(.+)" and its history\? This cannot be undone\.$/, function (_, n) { return 'Supprimer « ' + n + ' » et son historique? C\'est irréversible.'; }],
    [/^Email does not match\. Type (.+) to confirm\.$/, function (_, e) { return 'Le courriel ne correspond pas. Tape ' + e + ' pour confirmer.'; }],
    [/^(\d+) kcal · (\d+)g prot$/, function (_, k, p) { return k + ' kcal · ' + p + ' g prot.'; }],
    // "Groceries · Scene+ Visa card": translate the category, never the account's own name
    [/^([^·]+) · (.+)$/, function (_, a, b) { return Object.prototype.hasOwnProperty.call(DICT, a) ? DICT[a] + ' · ' + b : null; }],
    [/^Crypto: (.+)\.\.\.$/, function (_, a) { return 'Crypto : ' + a + '…'; }],
    [/^Plaid error: (.*)$/, function (_, a) { return 'Erreur Plaid : ' + a; }],
    [/^Exchange error: (.*)$/, function (_, a) { return 'Erreur lors de l\'enregistrement : ' + a; }],
    [/^Failed to open Plaid Link: (.*)$/, function (_, a) { return 'Impossible d\'ouvrir Plaid : ' + a; }],
    // "Nov 15" (transaction dates from the server) and "Oct 26" (month labels)
    [/^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{1,2})$/, function (_, mo, n) { return n + ' ' + MON[mo]; }],
    [/^(January|February|March|April|May|June|July|August|September|October|November|December) (\d{4})$/, function (_, mo, y) { return MONTH[mo] + ' ' + y; }],
    [/^(January|February|March|April|May|June|July|August|September|October|November|December) (\d{1,2}), (\d{4})$/, function (_, mo, d, y) { return d + ' ' + MONTH[mo] + ' ' + y; }],
  ];

  function translate(s) {
    if (!s || !/[A-Za-z]/.test(s)) return s;
    var parts = s.match(/^(\s*)([\s\S]*?)(\s*)$/);
    var core = parts[2].replace(/\s+/g, ' ');
    var out = Object.prototype.hasOwnProperty.call(DICT, core) ? DICT[core] : null;
    if (out === null) {
      for (var i = 0; i < PATTERNS.length; i++) {
        var mm = core.match(PATTERNS[i][0]);
        if (mm) { var r = PATTERNS[i][1].apply(null, mm); if (r !== null) { out = r; break; } }
      }
    }
    return out === null ? s : parts[1] + out + parts[3];
  }
  api.t = translate;

  var ATTRS = ['placeholder', 'title', 'aria-label', 'alt'];
  var SKIP = /^(SCRIPT|STYLE|TEXTAREA|CODE|PRE|NOSCRIPT)$/;
  var done = new WeakMap(); // node → text we wrote, so our own edits don't loop

  function skipped(el) {
    for (var e = el; e && e !== document.body; e = e.parentElement) {
      if (SKIP.test(e.tagName) || e.isContentEditable || (e.hasAttribute && e.hasAttribute('data-i18n-skip'))) return true;
    }
    return false;
  }

  function doText(n) {
    if (done.get(n) === n.nodeValue || !n.parentElement || skipped(n.parentElement)) return;
    var t = translate(n.nodeValue);
    if (t !== n.nodeValue) n.nodeValue = t;
    done.set(n, n.nodeValue);
  }

  function doAttrs(el) {
    for (var i = 0; i < ATTRS.length; i++) {
      var v = el.getAttribute(ATTRS[i]);
      if (v) { var t = translate(v); if (t !== v) el.setAttribute(ATTRS[i], t); }
    }
  }

  function walk(root) {
    if (root.nodeType === 3) return doText(root);
    if (root.nodeType !== 1 || skipped(root)) return;
    doAttrs(root);
    var w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    var n;
    while ((n = w.nextNode())) {
      if (n.nodeType === 3) doText(n);
      // A text field's content is the person's, but its placeholder is ours
      else if (!/^(SCRIPT|STYLE)$/.test(n.tagName) && !(n.parentElement && skipped(n.parentElement))) doAttrs(n);
    }
  }

  new MutationObserver(function (list) {
    for (var i = 0; i < list.length; i++) {
      var r = list[i];
      if (r.type === 'characterData') doText(r.target);
      else if (r.type === 'attributes') { if (!skipped(r.target.parentElement || r.target)) doAttrs(r.target); }
      else for (var j = 0; j < r.addedNodes.length; j++) walk(r.addedNodes[j]);
    }
    if (document.title && translate(document.title) !== document.title) document.title = translate(document.title);
  }).observe(document.documentElement, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTRS });

  document.addEventListener('DOMContentLoaded', function () { walk(document.body); document.title = translate(document.title); });

  // Messages from alert() / confirm() / prompt()
  ['alert', 'confirm', 'prompt'].forEach(function (k) {
    var orig = window[k];
    window[k] = function (msg) { var a = [].slice.call(arguments); a[0] = translate(String(msg == null ? '' : msg)); return orig.apply(window, a); };
  });

  // Chart labels and legends live in a canvas, not the DOM
  function chartI18n() {
    if (!window.Chart || !window.Chart.register) return;
    window.Chart.register({
      id: 'fiscitI18n',
      beforeUpdate: function (chart) {
        var d = chart.data || {};
        if (Array.isArray(d.labels)) d.labels = d.labels.map(function (l) { return typeof l === 'string' ? translate(l) : l; });
        (d.datasets || []).forEach(function (ds) { if (typeof ds.label === 'string') ds.label = translate(ds.label); });
      },
    });
  }
  if (window.Chart) chartI18n(); else document.addEventListener('DOMContentLoaded', chartI18n);
})();
