"""
Read-only crypto wallet balances from public endpoints (no API keys, no accounts).

Ethereum: native ETH plus the ERC-20 tokens in TOKENS, read with eth_getBalance and
balanceOf calls on public JSON-RPC nodes. Only listed tokens are read, which also
keeps airdropped scam tokens out of people's net worth.
Prices: CoinGecko's public simple/price endpoint (USD, with 24h change).
"""

import requests

ETH_RPC_URLS = (
    "https://ethereum-rpc.publicnode.com",
    "https://cloudflare-eth.com",
    "https://eth.llamarpc.com",
)

# symbol, name, CoinGecko id, decimals, chart color
NATIVE = {
    "ethereum": ("ETH", "Ethereum", "ethereum", 18, "#627eea"),
    "solana":   ("SOL", "Solana", "solana", 9, "#14f195"),
    "bitcoin":  ("BTC", "Bitcoin", "bitcoin", 8, "#f7931a"),
}

# Popular ERC-20s on Ethereum mainnet: contract, symbol, name, CoinGecko id, decimals, color.
# Decimals are checked against each contract by the test in scratch, never guessed at runtime.
TOKENS = [
    ("0xdAC17F958D2ee523a2206206994597C13D831ec7", "USDT", "Tether", "tether", 6, "#26a17b"),
    ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "USDC", "USD Coin", "usd-coin", 6, "#2775ca"),
    ("0x6B175474E89094C44Da98b954EedeAC495271d0F", "DAI", "Dai", "dai", 18, "#f5ac37"),
    ("0x6c3ea9036406852006290770BEdFcAbA0e23A0e8", "PYUSD", "PayPal USD", "paypal-usd", 6, "#0070e0"),
    ("0x4c9EDD5852cd905f086C759E8383e09bff1E68B3", "USDe", "Ethena USDe", "ethena-usde", 18, "#a1a1aa"),
    ("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "WETH", "Wrapped Ether", "weth", 18, "#8a92b2"),
    ("0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84", "stETH", "Lido Staked ETH", "staked-ether", 18, "#00a3ff"),
    ("0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", "WBTC", "Wrapped Bitcoin", "wrapped-bitcoin", 8, "#f09242"),
    ("0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", "cbBTC", "Coinbase Wrapped BTC", "coinbase-wrapped-btc", 8, "#0052ff"),
    ("0x514910771AF9Ca656af840dff83E8264EcF986CA", "LINK", "Chainlink", "chainlink", 18, "#2a5ada"),
    ("0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984", "UNI", "Uniswap", "uniswap", 18, "#ff007a"),
    ("0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9", "AAVE", "Aave", "aave", 18, "#b6509e"),
    ("0x5A98FcBEA516Cf06857215779Fd812CA3beF1B32", "LDO", "Lido DAO", "lido-dao", 18, "#f69988"),
    ("0x455e53CBB86018Ac2B8092FdCd39d8444aFFC3F6", "POL", "Polygon", "polygon-ecosystem-token", 18, "#8247e5"),
    ("0xB50721BCf8d664c30412Cfbc6cf7a15145234ad1", "ARB", "Arbitrum", "arbitrum", 18, "#28a0f0"),
    ("0xfAbA6f8e4a5E8Ab82F62fe7C39859FA577269BE3", "ONDO", "Ondo", "ondo-finance", 18, "#1a1a2e"),
    ("0xc944E90C64B2c07662A292be6244BDf05Cda44a7", "GRT", "The Graph", "the-graph", 18, "#6747ed"),
    ("0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE", "SHIB", "Shiba Inu", "shiba-inu", 18, "#ffa409"),
    ("0x6982508145454Ce325dDbE47a25d4ec3d2311933", "PEPE", "Pepe", "pepe", 18, "#4c9540"),
]


def _eth_rpc(payload):
    """POST one call (or a batch) to the first public node that answers."""
    last = None
    for url in ETH_RPC_URLS:
        try:
            r = requests.post(url, json=payload, timeout=12)
            r.raise_for_status()
            body = r.json()
            if isinstance(payload, list) and not isinstance(body, list):
                raise RuntimeError(body.get("error", {}).get("message", "batch not supported"))
            if isinstance(body, dict) and "result" not in body:
                raise RuntimeError(body.get("error", {}).get("message", "no result"))
            return body
        except Exception as e:
            last = e
    raise RuntimeError(f"every Ethereum node failed ({last})")


def eth_native(address):
    body = _eth_rpc({"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [address, "latest"]})
    return int(body["result"], 16) / 1e18


def erc20_balances(address):
    """[(token tuple, amount)] for every listed token the address holds, in one batch."""
    data = "0x70a08231" + address.lower().replace("0x", "").rjust(64, "0")  # balanceOf(address)
    batch = [
        {"jsonrpc": "2.0", "id": i, "method": "eth_call", "params": [{"to": t[0], "data": data}, "latest"]}
        for i, t in enumerate(TOKENS)
    ]
    out = []
    for item in _eth_rpc(batch):
        raw = item.get("result")
        if not raw or raw == "0x":
            continue
        units = int(raw, 16)
        if units:
            token = TOKENS[item["id"]]
            out.append((token, units / 10 ** token[4]))
    return out


def sol_native(address):
    r = requests.post("https://api.mainnet-beta.solana.com", json={
        "jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [address],
    }, timeout=10)
    r.raise_for_status()
    body = r.json()
    if "result" not in body:
        raise RuntimeError(body.get("error", {}).get("message", "no result"))
    return body["result"]["value"] / 1e9


def btc_native(address):
    r = requests.get(f"https://blockchain.info/balance?active={address}", timeout=10)
    r.raise_for_status()
    return r.json().get(address, {}).get("final_balance", 0) / 1e8


def prices_usd(coingecko_ids):
    """{id: (usd price, 24h % change)}; missing ids are left out (value shown as unknown)."""
    ids = sorted(set(coingecko_ids))
    if not ids:
        return {}
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price", params={
            "ids": ",".join(ids), "vs_currencies": "usd", "include_24hr_change": "true",
        }, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  Crypto prices: error → {e}")
        return {}
    return {k: (v.get("usd") or 0, v.get("usd_24h_change") or 0) for k, v in data.items() if "usd" in v}


def wallet_holdings(chain, address):
    """Everything a wallet holds, priced in USD:
    [{sym, name, amount, usd, change, color}], native coin first. Raises on failure."""
    if chain not in NATIVE:
        raise ValueError(f"{chain} wallets are not supported yet")
    sym, name, cg, _, color = NATIVE[chain]
    if chain == "ethereum":
        if not (address.startswith("0x") and len(address) == 42):
            raise ValueError("not an Ethereum address (expected 0x followed by 40 characters)")
        raw = [((sym, name, cg, color), eth_native(address))]
        try:
            raw += [((t[1], t[2], t[3], t[5]), amt) for t, amt in erc20_balances(address)]
        except Exception as e:
            print(f"  Crypto tokens for {address[:8]}…: error → {e}")  # ETH still counts
    elif chain == "solana":
        raw = [((sym, name, cg, color), sol_native(address))]
    else:
        raw = [((sym, name, cg, color), btc_native(address))]

    prices = prices_usd(meta[2] for meta, _ in raw)
    holdings = []
    for (s, n, c, col), amount in raw:
        price, change = prices.get(c, (None, 0))
        usd = amount * price if price is not None else 0
        # Keep the native coin even at 0 so the wallet shows; drop token dust under 1 cent
        if s != sym and price is not None and usd < 0.01:
            continue
        holdings.append({"sym": s, "name": n, "amount": amount, "price": price,
                         "usd": usd, "change": change, "color": col})
    return holdings
