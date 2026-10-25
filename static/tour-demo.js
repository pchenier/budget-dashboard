/* Example data shown ONLY while the guided tour runs (tour.js swaps it into D
   and restores the user's real data afterwards). Dates are relative to today
   so this-month views (budget, savings, subscriptions) have something to show. */
window.buildFiscitDemo = function () {
  const now = new Date();
  const iso = (d) => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  const human = (d) => d.toLocaleString('en', { month: 'short' }) + ' ' + String(d.getDate()).padStart(2, '0');
  // n days ago, but never in the future and kept inside the target month
  const day = (monthsAgo, dom) => {
    const d = new Date(now.getFullYear(), now.getMonth() - monthsAgo, 1);
    const last = new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate();
    const cap = monthsAgo === 0 ? Math.max(1, now.getDate()) : last;
    d.setDate(Math.min(dom, cap));
    return d;
  };

  const ICO = {
    Groceries: 'shopping-cart', 'Food/Dining': 'coffee', Gas: 'droplet', Transport: 'map', Shopping: 'shopping-bag',
    Subscriptions: 'repeat', Housing: 'home', Utilities: 'zap', Income: 'dollar-sign', 'Phone/Internet': 'smartphone',
    Gym: 'activity', Entertainment: 'film', Health: 'heart',
  };
  const T = (name, amt, cat, acc, d) => ({
    name, merchant: name, amt, cat, acc, ico: ICO[cat] || 'folder', date: human(d), date_iso: iso(d), id: 'demo-' + name + iso(d),
  });

  // Three months of transactions so savings/subscriptions have history
  const txns = [];
  for (let m = 0; m < 3; m++) {
    txns.push(
      T('Payroll Deposit', 3400, 'Income', 'Chequing', day(m, 1)),
      T('Payroll Deposit', 3400, 'Income', 'Chequing', day(m, 15)),
      T('Rent — Plateau Apt', -1650, 'Housing', 'Chequing', day(m, 1)),
      T('Hydro-Québec', -112.4 - m * 6, 'Utilities', 'Chequing', day(m, 6)),
      T('Videotron', -85, 'Phone/Internet', 'Visa', day(m, 9)),
      T('Netflix', -22.99, 'Subscriptions', 'Visa', day(m, 4)),
      T('Spotify', -15.99, 'Subscriptions', 'Visa', day(m, 11)),
      T('IGA', -96.32 + m * 4, 'Groceries', 'Visa', day(m, 3)),
      T('Metro', -64.18, 'Groceries', 'Visa', day(m, 10)),
      T('Costco', -182.5, 'Groceries', 'Visa', day(m, 17)),
      T('Tim Hortons', -6.45, 'Food/Dining', 'Visa', day(m, 2)),
      T('Café Olimpico', -4.75, 'Food/Dining', 'Visa', day(m, 8)),
      T('Restaurant Damas', -86.2, 'Food/Dining', 'Visa', day(m, 13)),
      T('Uber Eats', -38.9, 'Food/Dining', 'Visa', day(m, 19)),
      T('STM — Opus', -97, 'Transport', 'Chequing', day(m, 1)),
      T('Shell', -58.1, 'Gas', 'Visa', day(m, 12)),
      T('Amazon.ca', -74.99 - m * 20, 'Shopping', 'Visa', day(m, 7)),
      T('Simons', -129, 'Shopping', 'Visa', day(m, 21)),
      T('Econofitness', -24.99, 'Gym', 'Visa', day(m, 5)),
      T('Cinéma Banque Scotia', -32, 'Entertainment', 'Visa', day(m, 16)),
      T('Pharmaprix', -27.4, 'Health', 'Visa', day(m, 14)),
    );
  }
  txns.push(T('Disney+', -14.99, 'Subscriptions', 'Visa', day(0, 6))); // "new" this month
  txns.sort((a, b) => (a.date_iso < b.date_iso ? 1 : -1));

  const monthLabels = [];
  for (let i = 11; i >= 0; i--) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    monthLabels.push(d.toLocaleString('en', { month: 'short' }));
  }
  const netWorthHistory = [138200, 140150, 141900, 143300, 146800, 148100, 151600, 153900, 156400, 159800, 162300, 164210];
  const investHistory = [52100, 52900, 53800, 53200, 55100, 56300, 57900, 58600, 59800, 60900, 61400, 61880];

  const cats = {};
  txns.filter((t) => t.amt < 0 && t.date_iso.slice(0, 7) === iso(now).slice(0, 7))
    .forEach((t) => (cats[t.cat] = (cats[t.cat] || 0) + -t.amt));
  const catLabels = Object.keys(cats).sort((a, b) => cats[b] - cats[a]);

  return {
    netWorth: 164210.57, totalAssets: 166850.57, totalDebt: 2640, income: 6800, spending: 3045.86, cashFlow: 3754.14,
    generated: 'Example data', netWorthHistory, monthLabels,
    accounts: [
      { id: 'demo-chq', name: 'Chequing', inst: 'Desjardins', bal: 8412.3, type: 'Chequing / Savings', delta: 1240, sync: 'just now', color: '#22c55e', init: 'C' },
      { id: 'demo-sav', name: 'High-interest savings', inst: 'EQ Bank', bal: 42150, type: 'Savings', delta: 600, sync: 'just now', color: '#6366f1', init: 'H' },
      { id: 'demo-visa', name: 'Visa Infinite', inst: 'Desjardins', bal: -2640, type: 'Credit Card', delta: -310, sync: 'just now', color: '#ef4444', init: 'V' },
      { id: 'demo-tfsa', name: 'TFSA', inst: 'Questrade', bal: 61880.12, type: 'Investment', delta: 1720, sync: '1h ago', color: '#3b82f6', init: 'T' },
      { id: 'demo-wise', name: 'Wise · USD', inst: 'Wise', bal: 4485, type: 'International', delta: 0, sync: 'just now', color: '#8b5cf6', init: 'W' },
    ],
    txns,
    budget: [
      { cat: 'Groceries', spent: 0, lim: 450, ico: ICO.Groceries },
      { cat: 'Food/Dining', spent: 0, lim: 250, ico: ICO['Food/Dining'] },
      { cat: 'Shopping', spent: 0, lim: 200, ico: ICO.Shopping },
      { cat: 'Transport', spent: 0, lim: 120, ico: ICO.Transport },
      { cat: 'Subscriptions', spent: 0, lim: 60, ico: ICO.Subscriptions },
    ],
    catLabels, catAmounts: catLabels.map((c) => Math.round(cats[c])),
    investments: [
      { name: 'Vanguard All-Equity ETF', ticker: 'VEQT', val: 38450, pct: 1.4, color: '#3b82f6', shares: 912, price: 42.16 },
      { name: 'iShares S&P/TSX 60', ticker: 'XIU', val: 14230, pct: 0.6, color: '#22c55e', shares: 402, price: 35.4 },
      { name: 'Shopify Inc.', ticker: 'SHOP', val: 9200.12, pct: -2.1, color: '#a78bfa', shares: 68, price: 135.3 },
    ],
    investHistory,
    crypto: [
      { coin: 'SOL', name: 'Solana', amt: 14.2, price: 236.5, val: 3358.3, pct: 3.2, color: '#a78bfa' },
      { coin: 'BTC', name: 'Bitcoin', amt: 0.0062, price: 137400, val: 851.88, pct: -0.8, color: '#f59e0b' },
    ],
    bills: [
      { day: 1, mo: 'next', name: 'Rent — Plateau Apt', amt: 1650 },
      { day: 4, mo: 'next', name: 'Netflix', amt: 22.99 },
      { day: 6, mo: 'next', name: 'Hydro-Québec', amt: 118 },
      { day: 9, mo: 'next', name: 'Videotron', amt: 85 },
    ].map((b) => ({ ...b, mo: new Date(now.getFullYear(), now.getMonth() + 1, 1).toLocaleString('en', { month: 'short' }).toUpperCase() })),
    habits: [
      { name: 'Workout', done: [1, 1, 0, 1, 1, 1, 0], streak: 8 },
      { name: 'Cook at home', done: [1, 0, 1, 1, 0, 1, 1], streak: 3 },
      { name: 'No-spend day', done: [0, 1, 0, 0, 1, 0, 1], streak: 1 },
    ],
    gym: [], gymDays: [], meals: [],
    userName: '', userEmail: '',
    connections: { plaid: true, wise: true, crypto: true },
    wallets: [{ chain: 'solana', address: 'Demo…Wallet', label: 'Phantom' }],
  };
};

/* Example insights, only rendered while the tour is running. */
window.FISCIT_DEMO_INSIGHTS = [
  { ico: 'plus', bg: 'var(--green-bg)', title: 'Net worth increased $1,890 this month', body: 'Your net worth is up 1.2% from last month, driven by investment growth of $1,200 and positive cash flow.', type: 'positive' },
  { ico: 'coffee', bg: 'var(--red-bg)', title: 'Dining out is 6% over your budget', body: 'You spent $265 on dining this month vs your $250 limit. Cooking at home twice more this week would bring you back on track.', type: 'warning' },
  { ico: 'package', bg: 'var(--yellow-bg)', title: 'Shopping increased 34% vs last month', body: 'Shopping jumped from $235 to $315. Amazon and Simons account for most of the increase.', type: 'warning' },
  { ico: 'dollar-sign', bg: 'var(--blue-bg)', title: 'On track to save $2,562 this month', body: 'With $6,800 of income and $4,238 spent so far, you will keep about 37.7% of what you earned.', type: 'positive' },
  { ico: 'refresh-cw', bg: 'var(--bg3)', title: 'Subscriptions: $54/month across 3 services', body: 'Netflix, Spotify and a new Disney+ charge this month. Disney+ is new — keep it?', type: 'neutral' },
  { ico: 'zap', bg: 'var(--bg3)', title: 'Hydro-Québec was $118 — slightly above average', body: 'That is $8 above your 3-month average, likely heating. Still within your $130 budget.', type: 'neutral' },
];
