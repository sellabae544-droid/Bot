
import os, json, time, asyncio, logging, re, html
from typing import Any, Dict, Optional, List, Tuple
from urllib.parse import urlparse
import requests


# --- TON price (USD) cache ---
_TON_PRICE_CACHE = {"ts": 0.0, "price": None}

def get_ton_price_usd() -> Optional[float]:
    """Fetch TON price in USD. Cached for 60s to avoid rate limits.
    Uses CoinGecko simple price endpoint.
    """
    try:
        now = time.time()
        if _TON_PRICE_CACHE["price"] is not None and (now - _TON_PRICE_CACHE["ts"]) < 60:
            return float(_TON_PRICE_CACHE["price"])
        # CoinGecko id for TON is commonly 'the-open-network'
        url = "https://api.coingecko.com/api/v3/simple/price"
        r = requests.get(url, params={"ids": "the-open-network", "vs_currencies": "usd"}, timeout=10)
        if r.status_code == 200:
            j = r.json()
            p = (j.get("the-open-network") or {}).get("usd")
            if p:
                _TON_PRICE_CACHE["ts"] = now
                _TON_PRICE_CACHE["price"] = float(p)
                return float(p)
    except Exception:
        pass
    return None

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, CallbackQueryHandler,
    MessageHandler, ChatMemberHandler, ContextTypes, filters
)

# -------------------- LOGGING --------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("spyton_public")

# -------------------- ENV --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TONAPI_KEY = os.getenv("TONAPI_KEY", "").strip()
TONAPI_BASE = os.getenv("TONAPI_BASE", "https://tonapi.io").strip().rstrip("/")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "6.0"))
BURST_WINDOW_SEC = int(os.getenv("BURST_WINDOW_SEC", "30"))
DTRADE_REF = os.getenv("DTRADE_REF", "https://t.me/dtrade?start=11TYq7LInG").strip()
TRENDING_URL = os.getenv("TRENDING_URL", "https://t.me/SpyTonTrending").strip()
DEFAULT_TOKEN_TG = os.getenv("DEFAULT_TOKEN_TG", "https://t.me/SpyTonEco").strip()
GECKO_BASE = os.getenv("GECKO_BASE", "https://api.geckoterminal.com/api/v2").strip().rstrip("/")

DATA_FILE = os.getenv("GROUPS_FILE", "groups_public.json")
SEEN_FILE = os.getenv("SEEN_FILE", "seen_public.json")

# Dexscreener endpoints (used to resolve pool<->token)
DEX_TOKEN_URL = os.getenv("DEX_TOKEN_URL", "https://api.dexscreener.com/latest/dex/tokens").rstrip("/")
DEX_PAIR_URL = os.getenv("DEX_PAIR_URL", "https://api.dexscreener.com/latest/dex/pairs").rstrip("/")


# -------------------- DEDUST API (for pool discovery + trades) --------------------
DEDUST_API = os.getenv("DEDUST_API", "https://api.dedust.io").rstrip("/")

_DEDUST_POOLS_CACHE = {"ts": 0, "data": None}

def dedust_get_pools() -> List[Dict[str, Any]]:
    """Fetch available pools from DeDust API. Cached to avoid heavy downloads."""
    now = int(time.time())
    if _DEDUST_POOLS_CACHE["data"] is not None and now - int(_DEDUST_POOLS_CACHE["ts"] or 0) < 3600:
        return _DEDUST_POOLS_CACHE["data"] or []
    try:
        r = requests.get(f"{DEDUST_API}/v2/pools", timeout=25)
        if r.status_code != 200:
            return _DEDUST_POOLS_CACHE["data"] or []
        js = r.json()
        pools = js.get("pools") if isinstance(js, dict) else js
        if not isinstance(pools, list):
            pools = []
        _DEDUST_POOLS_CACHE["ts"] = now
        _DEDUST_POOLS_CACHE["data"] = pools
        return pools
    except Exception:
        return _DEDUST_POOLS_CACHE["data"] or []

def _dedust_is_ton_asset(asset: Any) -> bool:
    if not isinstance(asset, dict):
        return False
    t = (asset.get("type") or asset.get("kind") or "").lower()
    # common representations
    if t in ("native", "ton"):
        return True
    # sometimes TON shown as jetton with empty address
    sym = (asset.get("symbol") or "").upper()
    if sym == "TON":
        return True
    addr = (asset.get("address") or "").strip()
    # TON has no jetton master address; keep conservative
    return False

def _dedust_asset_addr(asset: Any) -> str:
    if not isinstance(asset, dict):
        return ""
    return str(asset.get("address") or asset.get("master") or asset.get("jetton") or "").strip()

def find_dedust_ton_pair_for_token(token_address: str) -> Optional[str]:
    """Find DeDust pool address for TON <-> token using DeDust API pools list."""
    ta = (token_address or "").strip()
    if not ta:
        return None
    try:
        pools = dedust_get_pools()
        best_pool = None
        best_liq = -1.0
        for p in pools:
            if not isinstance(p, dict):
                continue
            addr = str(p.get("address") or p.get("pool") or p.get("id") or "").strip()
            if not addr:
                continue
            assets = p.get("assets") or p.get("tokens") or p.get("reserves") or []
            # assets might be dict with keys a/b
            if isinstance(assets, dict):
                assets = list(assets.values())
            if not isinstance(assets, list) or len(assets) < 2:
                continue
            a0, a1 = assets[0], assets[1]
            # Determine TON side
            ton_side = None
            tok_side_addr = ""
            if _dedust_is_ton_asset(a0):
                ton_side = 0
                tok_side_addr = _dedust_asset_addr(a1)
            elif _dedust_is_ton_asset(a1):
                ton_side = 1
                tok_side_addr = _dedust_asset_addr(a0)
            else:
                continue
            if not tok_side_addr:
                continue
            if tok_side_addr != ta:
                continue
            # liquidity score if available
            liq = 0.0
            try:
                liq = float(p.get("liquidityUsd") or p.get("liquidity_usd") or p.get("tvlUsd") or 0.0)
            except Exception:
                liq = 0.0
            if liq > best_liq:
                best_liq = liq
                best_pool = addr
        return best_pool
    except Exception:
        return None

def dedust_get_trades(pool: str, limit: int = 20) -> List[Dict[str, Any]]:
    try:
        r = requests.get(f"{DEDUST_API}/v2/pools/{pool}/trades", params={"limit": limit}, timeout=25)
        if r.status_code != 200:
            return []
        js = r.json()
        trades = js.get("trades") if isinstance(js, dict) else js
        if not isinstance(trades, list):
            return []
        return trades
    except Exception:
        return []

def dedust_trade_to_buy(tr: Dict[str, Any], token_addr: str) -> Optional[Dict[str, Any]]:
    """Convert a DeDust trade item to our buy dict if it's TON -> token."""
    if not isinstance(tr, dict):
        return None
    # common fields guesses
    tx = str(tr.get("tx") or tr.get("txHash") or tr.get("hash") or tr.get("transaction") or "").strip()
    buyer = str(tr.get("sender") or tr.get("trader") or tr.get("maker") or tr.get("wallet") or "").strip()
    trade_id = str(tr.get("id") or tr.get("tradeId") or tr.get("lt") or tr.get("seqno") or tx).strip()
    # asset in/out objects
    ain = tr.get("assetIn") or tr.get("inAsset") or tr.get("fromAsset") or tr.get("in") or {}
    aout = tr.get("assetOut") or tr.get("outAsset") or tr.get("toAsset") or tr.get("out") or {}
    # amounts
    amt_in = tr.get("amountIn") or tr.get("inAmount") or tr.get("amount_in") or tr.get("amountInJettons") or tr.get("amount_in_wei") or tr.get("in") or None
    amt_out = tr.get("amountOut") or tr.get("outAmount") or tr.get("amount_out") or tr.get("amountOutJettons") or tr.get("out") or None

    # Some APIs nest amounts with decimals
    def _as_float(x):
        try:
            if isinstance(x, dict):
                x = x.get("value") or x.get("amount")
            return float(x)
        except Exception:
            return 0.0

    amt_in_f = _as_float(amt_in)
    amt_out_f = _as_float(amt_out)

    # Determine if this is TON -> token
    is_ton_in = _dedust_is_ton_asset(ain) or (isinstance(ain, dict) and (ain.get("symbol") or "").upper() == "TON")
    out_addr = _dedust_asset_addr(aout)
    if not is_ton_in:
        return None
    if out_addr != token_addr:
        return None

    # TON amount is in TON (API usually already human). If API returns nano, it will be huge; we guard:
    ton_amt = amt_in_f
    if ton_amt > 1e8:  # looks like nanoTON
        ton_amt = ton_amt / 1e9

    token_amt = amt_out_f
    return {
        "tx": tx or trade_id,
        "buyer": buyer,
        "ton": ton_amt,
        "token_amount": token_amt,
        "trade_id": trade_id,
    }

# -------------------- STATE --------------------
DEFAULT_SETTINGS = {
    "enable_ston": True,
    "enable_dedust": True,
    "min_buy_ton": 0.0,
    "anti_spam": "MED",   # LOW | MED | HIGH
    "burst_mode": True,
}

def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save_json(path: str, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

GROUPS: Dict[str, Any] = _load_json(DATA_FILE, {})  # chat_id -> config
SEEN: Dict[str, Any] = _load_json(SEEN_FILE, {})    # chat_id -> {dedupe_key: ts}

# user_id -> chat_id awaiting token paste
AWAITING: Dict[int, int] = {}
# user_id -> chat_id awaiting media upload (photo/gif)
AWAITING_MEDIA: Dict[int, int] = {}

# -------------------- HELPERS --------------------
JETTON_RE = re.compile(r"\b([EU]Q[A-Za-z0-9_-]{40,80})\b")
GECKO_POOL_RE = re.compile(r"geckoterminal\.com/ton/pools/([A-Za-z0-9_-]{20,120})", re.IGNORECASE)
DEXSCREENER_PAIR_RE = re.compile(r"dexscreener\.com/ton/([A-Za-z0-9_-]{20,120})", re.IGNORECASE)
STON_POOL_RE = re.compile(r"ston\.fi/[^\s]*?(?:pool|pools)/([A-Za-z0-9_-]{20,120})", re.IGNORECASE)
DEDUST_POOL_RE = re.compile(r"dedust\.(?:io|org)/[^\s]*?(?:pool|pools)/([A-Za-z0-9_-]{20,120})", re.IGNORECASE)

def is_private(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == "private")

async def is_admin(bot, chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False

def get_group(chat_id: int) -> Dict[str, Any]:
    key = str(chat_id)
    g = GROUPS.get(key)
    if not isinstance(g, dict):
        g = {}
        GROUPS[key] = g
    g.setdefault("settings", dict(DEFAULT_SETTINGS))
    g.setdefault("token", None)  # {address, symbol, name, ston_pool, dedust_pool}
    g.setdefault("created_at", int(time.time()))
    return g

def save_groups():
    _save_json(DATA_FILE, GROUPS)

def save_seen():
    _save_json(SEEN_FILE, SEEN)

def dedupe_ok(chat_id: int, key: str, ttl: int = 600) -> bool:
    now = int(time.time())
    bucket = SEEN.setdefault(str(chat_id), {})
    # clean a little
    if len(bucket) > 4000:
        for k, ts in list(bucket.items())[:800]:
            if now - int(ts) > ttl:
                bucket.pop(k, None)
    ts = bucket.get(key)
    if ts and now - int(ts) < ttl:
        return False
    bucket[key] = now
    return True

def anti_spam_limit(level: str) -> Tuple[int,int]:
    # returns (max_msgs_per_window, window_sec)
    lvl = (level or "MED").upper()
    if lvl == "LOW":
        return (9999, BURST_WINDOW_SEC)
    if lvl == "HIGH":
        return (4, BURST_WINDOW_SEC)
    return (8, BURST_WINDOW_SEC)

# -------------------- TONAPI --------------------
def tonapi_headers() -> Dict[str, str]:
    if not TONAPI_KEY:
        return {"Accept": "application/json"}
    return {"Authorization": f"Bearer {TONAPI_KEY}", "Accept": "application/json"}

def tonapi_get_raw(url: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    try:
        res = requests.get(url, headers=tonapi_headers(), params=params, timeout=20)
        if res.status_code in (401,403) and TONAPI_KEY:
            # sometimes user sets X-API-Key (toncenter-style)
            res = requests.get(url, headers={"X-API-Key": TONAPI_KEY, "Accept":"application/json"}, params=params, timeout=20)
        if res.status_code != 200:
            return None
        return res.json()
    except Exception:
        return None

def tonapi_get(url: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    js = tonapi_get_raw(url, params=params)
    return js if isinstance(js, dict) else None

def tonapi_jetton_info(jetton: str) -> Dict[str, str]:
    out = {"name": "", "symbol": ""}
    js = tonapi_get(f"{TONAPI_BASE}/v2/jettons/{jetton}")
    if not js:
        return out
    meta = js.get("metadata") or {}
    out["name"] = str(meta.get("name") or js.get("name") or "").strip()
    out["symbol"] = str(meta.get("symbol") or js.get("symbol") or "").strip()
    return out

def tonapi_account_transactions(address: str, limit: int = 12) -> List[Dict[str, Any]]:
    js = tonapi_get(f"{TONAPI_BASE}/v2/blockchain/accounts/{address}/transactions", params={"limit": limit})
    txs = js.get("transactions") if isinstance(js, dict) else None
    return txs if isinstance(txs, list) else []

def tonapi_account_events(address: str, limit: int = 10) -> List[Dict[str, Any]]:
    js = tonapi_get(f"{TONAPI_BASE}/v2/accounts/{address}/events", params={"limit": limit})
    ev = js.get("events") if isinstance(js, dict) else None
    return ev if isinstance(ev, list) else []

# -------------------- DEX PAIR LOOKUP --------------------

# -------------------- GECKO TERMINAL --------------------
def gecko_get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    """GeckoTerminal public API (best-effort)."""
    try:
        url = f"{GECKO_BASE}{path}"
        r = requests.get(
            url,
            params=params or {},
            headers={
                "accept": "application/json",
                "user-agent": "SpyTONBuyBot/1.0",
            },
            timeout=12,
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None

def gecko_token_info(token_addr: str) -> Optional[dict]:
    # token_addr should be a jetton master (EQ.. / UQ..)
    j = gecko_get(f"/networks/ton/tokens/{token_addr}")
    if not j or "data" not in j:
        return None
    attrs = (j.get("data") or {}).get("attributes") or {}
    return {
        "name": attrs.get("name") or "",
        "symbol": attrs.get("symbol") or "",
        "decimals": attrs.get("decimals"),
        "price_usd": attrs.get("price_usd"),
        "market_cap_usd": attrs.get("market_cap_usd") or attrs.get("fdv_usd"),
    }

def gecko_pool_info(pool_addr: str) -> Optional[dict]:
    j = gecko_get(f"/networks/ton/pools/{pool_addr}")
    if not j or "data" not in j:
        return None
    attrs = (j.get("data") or {}).get("attributes") or {}
    return {
        "price_usd": attrs.get("base_token_price_usd") or attrs.get("price_usd"),
        "liquidity_usd": attrs.get("reserve_in_usd") or attrs.get("liquidity_usd"),
        "fdv_usd": attrs.get("fdv_usd"),
        "market_cap_usd": attrs.get("market_cap_usd") or attrs.get("fdv_usd"),
        "name": attrs.get("name"),
    }

def gecko_terminal_pool_url(pool_addr: str) -> str:
    return f"https://www.geckoterminal.com/ton/pools/{pool_addr}"

def find_pair_for_token_on_dex(token_address: str, want_dex: str) -> Optional[str]:
    url = f"{DEX_TOKEN_URL}/{token_address}"
    try:
        res = requests.get(url, timeout=20)
        if res.status_code != 200:
            return None
        js = res.json()
        pairs = js.get("pairs") if isinstance(js, dict) else None
        if not isinstance(pairs, list):
            return None

        want = want_dex.lower()
        best_pair_id = None
        best_score = -1.0

        for p in pairs:
            if not isinstance(p, dict):
                continue
            dex_id = (p.get("dexId") or "").lower()
            chain_id = (p.get("chainId") or "").lower()
            if chain_id != "ton":
                continue

            if want == "stonfi" and "ston" not in dex_id:
                continue
            if want == "dedust" and "dedust" not in dex_id:
                continue

            base = p.get("baseToken") or {}
            quote = p.get("quoteToken") or {}
            base_sym = (base.get("symbol") or "").upper()
            quote_sym = (quote.get("symbol") or "").upper()
            if base_sym != "TON" and quote_sym != "TON":
                continue

            pair_id = (p.get("pairAddress") or p.get("pairId") or p.get("pair") or "").strip()
            if not pair_id:
                u = (p.get("url") or "")
                if "/ton/" in u:
                    pair_id = u.split("/ton/")[-1].split("?")[0].strip()
            if not pair_id:
                continue

            liq = 0.0
            vol = 0.0
            try:
                liq = float(((p.get("liquidity") or {}).get("usd") or 0) or 0)
            except Exception:
                liq = 0.0
            try:
                vol = float(((p.get("volume") or {}).get("h24") or 0) or 0)
            except Exception:
                vol = 0.0

            score = liq * 1_000_000 + vol
            if score > best_score:
                best_score = score
                best_pair_id = pair_id

        return best_pair_id
    except Exception:
        return None

def find_stonfi_ton_pair_for_token(token_address: str) -> Optional[str]:
    return find_pair_for_token_on_dex(token_address, "stonfi")


def dex_token_info(token_address: str) -> Dict[str, str]:
    """Fallback metadata from Dexscreener.

    DexScreener often has token name/symbol even when TonAPI metadata is missing.
    We pick the TON pair with best liquidity/volume and read the non-TON side.
    """
    out = {"name": "", "symbol": ""}
    try:
        g = gecko_token_info(token_address)
        if g:
            out["name"] = g.get("name") or out["name"]
            out["symbol"] = g.get("symbol") or out["symbol"]
            if out["name"] or out["symbol"]:
                return out
        res = requests.get(f"{DEX_TOKEN_URL}/{token_address}", timeout=20)
        if res.status_code != 200:
            return out
        js = res.json()
        pairs = js.get("pairs") if isinstance(js, dict) else None
        if not isinstance(pairs, list) or not pairs:
            return out

        best = None
        best_score = -1.0
        for p in pairs:
            if not isinstance(p, dict):
                continue
            if (p.get("chainId") or "").lower() != "ton":
                continue
            base = p.get("baseToken") or {}
            quote = p.get("quoteToken") or {}
            base_sym = (base.get("symbol") or "").upper()
            quote_sym = (quote.get("symbol") or "").upper()
            if base_sym != "TON" and quote_sym != "TON":
                continue
            liq = 0.0
            vol = 0.0
            try:
                liq = float(((p.get("liquidity") or {}).get("usd") or 0) or 0)
            except Exception:
                liq = 0.0
            try:
                vol = float(((p.get("volume") or {}).get("h24") or 0) or 0)
            except Exception:
                vol = 0.0
            score = liq * 1_000_000 + vol
            if score > best_score:
                best_score = score
                best = p

        if not best:
            best = pairs[0]

        base = best.get("baseToken") or {}
        quote = best.get("quoteToken") or {}
        base_addr = str(base.get("address") or "")
        quote_addr = str(quote.get("address") or "")
        # Choose the side that matches the token_address if possible
        tok = base if base_addr == token_address else (quote if quote_addr == token_address else None)
        if not tok:
            # Otherwise choose non-TON side
            tok = quote if (str(base.get("symbol") or "").upper() == "TON") else base
        out["name"] = str(tok.get("name") or "").strip()
        out["symbol"] = str(tok.get("symbol") or "").strip()
        return out
    except Exception:
        return out

# -------------------- BUY EXTRACTION (simplified from your working bot) --------------------
def _tx_hash(tx: Dict[str, Any]) -> str:
    return str(tx.get("hash") or tx.get("tx_hash") or tx.get("id") or "")

def _action_type(a: Dict[str, Any]) -> str:
    return str(a.get("type") or a.get("action") or a.get("name") or "")

def _short_addr(a: str) -> str:
    if not a:
        return ""
    if len(a) <= 10:
        return a
    return a[:4] + "…" + a[-4:]

def _to_float(x) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0

def stonfi_extract_buys_from_tonapi_tx(tx: Dict[str, Any], token_addr: str) -> List[Dict[str, Any]]:
    """Heuristic buy parser from TonAPI tx actions.
    BUY = TON -> token_addr.
    """
    out: List[Dict[str, Any]] = []
    tx_hash = _tx_hash(tx)

    actions = tx.get("actions")
    if not isinstance(actions, list):
        actions = []

    for a in actions:
        if not isinstance(a, dict):
            continue
        payload = a.get(a.get('type') or a.get('action') or a.get('name'))
        aa = dict(a)
        if isinstance(payload, dict):
            aa.update(payload)

        at = _action_type(aa).lower()
        if "swap" not in at and "dex" not in at:
            continue

        dex = aa.get("dex")
        dex_name = ""
        if isinstance(dex, dict):
            dex_name = str(dex.get("name") or dex.get("title") or dex.get("id") or "").lower()
        if dex_name and "ston" not in dex_name:
            continue

        # Try common fields TonAPI uses
        ton_in = _to_float(aa.get("amount_in") or aa.get("amountIn") or 0)
        jet_out = _to_float(aa.get("amount_out") or aa.get("amountOut") or 0)

        in_asset = aa.get("asset_in") or aa.get("assetIn") or aa.get("in") or {}
        out_asset = aa.get("asset_out") or aa.get("assetOut") or aa.get("out") or {}

        def asset_addr(x):
            if isinstance(x, dict):
                addr = x.get("address") or x.get("master") or x.get("jetton_master") or ""
                return str(addr)
            return ""

        in_addr = asset_addr(in_asset)
        out_addr = asset_addr(out_asset)

        # determine if TON in and token out
        is_buy = False
        # TonAPI might represent TON as "TON" or empty addr
        if out_addr == token_addr and (in_addr == "" or "ton" in str(in_asset).lower()):
            is_buy = True
        # sometimes out asset is jetton dict nested
        if not is_buy:
            # look inside swap details if present
            if str(out_addr) == token_addr and ton_in > 0:
                is_buy = True

        if not is_buy:
            continue

        buyer = (aa.get("user") or aa.get("sender") or aa.get("initiator") or aa.get("from") or "")
        if isinstance(buyer, dict):
            buyer = buyer.get("address") or ""
        buyer = str(buyer)

        out.append({
            "tx": tx_hash,
            "buyer": buyer,
            "ton": ton_in if ton_in else None,
            "token_amount": jet_out if jet_out else None,
        })

    return out

def dedust_extract_buys_from_tonapi_event(ev: Dict[str, Any], token_addr: str) -> List[Dict[str, Any]]:
    """TonAPI events endpoint sometimes provides swap action info too."""
    out: List[Dict[str, Any]] = []
    tx_hash = str(ev.get("event_id") or ev.get("id") or ev.get("hash") or "")
    actions = ev.get("actions")
    if not isinstance(actions, list):
        actions = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        at = _action_type(a).lower()
        if "swap" not in at and "dex" not in at:
            continue

        dex = a.get("dex")
        dex_name = ""
        if isinstance(dex, dict):
            dex_name = str(dex.get("name") or dex.get("title") or dex.get("id") or "").lower()
        if dex_name and "dedust" not in dex_name and "de dust" not in dex_name:
            continue

        # This varies; best-effort
        ton_in = _to_float(a.get("amount_in") or a.get("amountIn") or a.get("in_amount") or 0)
        out_asset = a.get("asset_out") or a.get("assetOut") or a.get("out") or {}
        out_addr = ""
        if isinstance(out_asset, dict):
            out_addr = str(out_asset.get("address") or out_asset.get("master") or "")
        if out_addr and out_addr != token_addr:
            continue

        buyer = (a.get("user") or a.get("sender") or a.get("initiator") or a.get("from") or "")
        if isinstance(buyer, dict):
            buyer = buyer.get("address") or ""
        buyer = str(buyer)

        if ton_in <= 0:
            continue

        out.append({"tx": tx_hash, "buyer": buyer, "ton": ton_in})
    return out

# -------------------- UI --------------------
async def build_add_to_group_url(app: Application) -> str:
    # We try to discover bot username at runtime.
    try:
        me = await app.bot.get_me()
        if me and me.username:
            return f"https://t.me/{me.username}?startgroup=true"
    except Exception:
        pass
    return "https://t.me/"  # fallback

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat:
        return

    if chat.type == "private":
        add_url = await build_add_to_group_url(context.application)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add BuyBot to Group", url=add_url)],
            [InlineKeyboardButton("⚙️ Configure Token", callback_data="CFG_PRIVATE")],
            [InlineKeyboardButton("🛠 Settings", callback_data="SET_PRIVATE")],
            [InlineKeyboardButton("🆘 Support", url="https://t.me/SpyTonEco")],
        ])
        await update.message.reply_text(
            "Welcome to *SpyTON BuyBot* (TON only).\n\n"
            "Use the buttons below — no commands needed.",
            reply_markup=kb,
            parse_mode="Markdown"
        )
    else:
        # In group, show group menu
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ Configure Token", callback_data="CFG_GROUP")],
            [InlineKeyboardButton("🛠 Settings", callback_data="SET_GROUP")],
            [InlineKeyboardButton("📊 Status", callback_data="STATUS_GROUP")],
            [InlineKeyboardButton("🗑 Remove Token", callback_data="REMOVE_GROUP")],
        ])
        await update.message.reply_text(
            "✅ *SpyTON BuyBot connected*\nTap *Configure Token* to start.",
            reply_markup=kb,
            parse_mode="Markdown"
        )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    chat = q.message.chat if q.message else update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    data = q.data or ""
    if data in ("CFG_PRIVATE","SET_PRIVATE"):
        # In private we configure a target group via last used group in AWAITING or ask user to do it in group
        await q.edit_message_text(
            "To configure a group:\n"
            "1) Add the bot to your group.\n"
            "2) In that group, tap *Configure Token*.",
            parse_mode="Markdown"
        )
        return

    if data == "CFG_GROUP":
        if not await is_admin(context.bot, chat.id, user.id):
            await q.answer("Admins only.", show_alert=True)
            return
        AWAITING[user.id] = chat.id
        await q.message.reply_text("Paste the token address (EQ… / UQ…) or a supported link.")
        return

    if data == "SET_GROUP":
        if not await is_admin(context.bot, chat.id, user.id):
            await q.answer("Admins only.", show_alert=True)
            return
        await send_settings(chat.id, context, q.message)
        return

    if data.startswith("TOG_"):
        if not await is_admin(context.bot, chat.id, user.id):
            await q.answer("Admins only.", show_alert=True)
            return
        g = get_group(chat.id)
        s = g["settings"]
        if data == "TOG_STON":
            s["enable_ston"] = not bool(s.get("enable_ston", True))
        elif data == "TOG_DEDUST":
            s["enable_dedust"] = not bool(s.get("enable_dedust", True))
        elif data == "TOG_BURST":
            s["burst_mode"] = not bool(s.get("burst_mode", True))
        save_groups()
        await send_settings(chat.id, context, q.message, edit=True)
        return

    if data.startswith("MIN_"):
        if not await is_admin(context.bot, chat.id, user.id):
            await q.answer("Admins only.", show_alert=True)
            return
        g = get_group(chat.id)
        s = g["settings"]
        val = float(data.split("_",1)[1])
        s["min_buy_ton"] = val
        save_groups()
        await send_settings(chat.id, context, q.message, edit=True)
        return

    if data.startswith("SPAM_"):
        if not await is_admin(context.bot, chat.id, user.id):
            await q.answer("Admins only.", show_alert=True)
            return
        g = get_group(chat.id)
        s = g["settings"]
        s["anti_spam"] = data.split("_",1)[1]
        save_groups()
        await send_settings(chat.id, context, q.message, edit=True)
        return


    if data.startswith("STR_"):
        if not await is_admin(context.bot, chat.id, user.id):
            await q.answer("Admins only.", show_alert=True)
            return
        g = get_group(chat.id)
        s = g["settings"]
        s["strength"] = data.split("_",1)[1]
        save_groups()
        await send_settings(chat.id, context, q.message, edit=True)
        return

    if data == "MEDIA_SET":
        if not await is_admin(context.bot, chat.id, user.id):
            await q.answer("Admins only.", show_alert=True)
            return
        AWAITING_MEDIA[user.id] = chat.id
        await q.message.reply_text("Send the *photo* or *GIF* you want to show on every buy in this group.", parse_mode="Markdown")
        return

    if data == "MEDIA_CLEAR":
        if not await is_admin(context.bot, chat.id, user.id):
            await q.answer("Admins only.", show_alert=True)
            return
        g = get_group(chat.id)
        s = g["settings"]
        s.pop("media_file_id", None)
        s.pop("media_type", None)
        save_groups()
        await q.message.reply_text("✅ Buy image cleared for this group.")
        await send_settings(chat.id, context, q.message, edit=True)
        return

    if data == "STATUS_GROUP":
        await send_status(chat.id, context, q.message)
        return

    if data == "REMOVE_GROUP":
        if not await is_admin(context.bot, chat.id, user.id):
            await q.answer("Admins only.", show_alert=True)
            return
        g = get_group(chat.id)
        if not g.get("token"):
            await q.message.reply_text("No token configured for this group.")
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Remove", callback_data="CONFIRM_REMOVE")],
            [InlineKeyboardButton("❌ Cancel", callback_data="CANCEL_REMOVE")]
        ])
        await q.message.reply_text("Remove the current token for this group?", reply_markup=kb)
        return

    if data == "CONFIRM_REMOVE":
        if not await is_admin(context.bot, chat.id, user.id):
            await q.answer("Admins only.", show_alert=True)
            return
        g = get_group(chat.id)
        g["token"] = None
        save_groups()
        await q.message.reply_text("✅ Token removed.")
        return

    if data == "CANCEL_REMOVE":
        await q.message.reply_text("Cancelled.")
        return

async def send_settings(chat_id: int, context: ContextTypes.DEFAULT_TYPE, msg, edit: bool=False):
    g = get_group(chat_id)
    s = g["settings"]
    ston = "ON ✅" if s.get("enable_ston", True) else "OFF ❌"
    dedust = "ON ✅" if s.get("enable_dedust", True) else "OFF ❌"
    burst = "ON ✅" if s.get("burst_mode", True) else "OFF ❌"
    anti = (s.get("anti_spam") or "MED").upper()
    min_buy = s.get("min_buy_ton", 0.0)

    text = (
        "*SpyTON BuyBot Settings*\n"
        f"• STON.fi: *{ston}*\n"
        f"• DeDust: *{dedust}*\n"
        f"• Burst mode: *{burst}*\n"
        f"• Anti-spam: *{anti}*\n"
        f"• Min buy (TON): *{min_buy}*\n"
        f"• Buy strength: *{(s.get('strength') or 'MED').upper()}*\n"
        f"• Media: *{'SET ✅' if s.get('media_file_id') else 'NONE'}*\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"STON.fi: {ston}", callback_data="TOG_STON"),
         InlineKeyboardButton(f"DeDust: {dedust}", callback_data="TOG_DEDUST")],
        [InlineKeyboardButton(f"Burst: {burst}", callback_data="TOG_BURST")],
        [InlineKeyboardButton("Min 0", callback_data="MIN_0"),
         InlineKeyboardButton("0.1", callback_data="MIN_0.1"),
         InlineKeyboardButton("0.5", callback_data="MIN_0.5"),
         InlineKeyboardButton("1", callback_data="MIN_1"),
         InlineKeyboardButton("5", callback_data="MIN_5")],
        [InlineKeyboardButton("Anti: LOW", callback_data="SPAM_LOW"),
         InlineKeyboardButton("MED", callback_data="SPAM_MED"),
         InlineKeyboardButton("HIGH", callback_data="SPAM_HIGH")],
        [InlineKeyboardButton("Strength: LOW", callback_data="STR_LOW"), InlineKeyboardButton("MED", callback_data="STR_MED"), InlineKeyboardButton("HIGH", callback_data="STR_HIGH")],
        [InlineKeyboardButton("🖼 Set Buy Image", callback_data="MEDIA_SET"), InlineKeyboardButton("🧹 Clear Image", callback_data="MEDIA_CLEAR")],
    ])
    if edit:
        try:
            await msg.edit_text(text, reply_markup=kb, parse_mode="Markdown")
            return
        except Exception:
            pass
    await msg.reply_text(text, reply_markup=kb, parse_mode="Markdown")

async def send_status(chat_id: int, context: ContextTypes.DEFAULT_TYPE, msg):
    g = get_group(chat_id)
    token = g.get("token")
    if not token:
        await msg.reply_text("No token configured. Tap *Configure Token*.", parse_mode="Markdown")
        return
    await msg.reply_text(
        "📊 *Status*\n"
        f"Token: *{token.get('symbol') or token.get('name') or 'UNKNOWN'}*\n"
        f"Address: `{token.get('address')}`\n"
        f"STON pool: `{token.get('ston_pool') or 'NONE'}`\n"
        f"DeDust pool: `{token.get('dedust_pool') or 'NONE'}`\n",
        parse_mode="Markdown"
    )

# -------------------- TOKEN AUTO-DETECT --------------------
def detect_token_address(text: str) -> Optional[str]:
    m = JETTON_RE.search(text or "")
    if m:
        return m.group(1)
    return None

def _dex_pair_lookup(pair_id: str) -> Optional[Dict[str, Any]]:
    """Return Dexscreener pair payload (TON) for a given pair/pool id."""
    pair_id = (pair_id or "").strip()
    if not pair_id:
        return None
    url = f"{DEX_PAIR_URL}/ton/{pair_id}"
    try:
        res = requests.get(url, timeout=20)
        if res.status_code != 200:
            return None
        js = res.json()
        pairs = js.get("pair") or js.get("pairs")
        if isinstance(pairs, list) and pairs:
            return pairs[0] if isinstance(pairs[0], dict) else None
        if isinstance(pairs, dict):
            return pairs
        # Some responses use "pairs" list
        if isinstance(js.get("pairs"), list) and js.get("pairs"):
            p0 = js.get("pairs")[0]
            return p0 if isinstance(p0, dict) else None
        return None
    except Exception:
        return None

def resolve_jetton_from_text_sync(text: str) -> Optional[str]:
    """Resolve a jetton master address from either a jetton address or supported pool/link."""
    t = (text or "").strip()
    if not t:
        return None

    # 1) Direct jetton address
    direct = detect_token_address(t)
    if direct:
        # If it *looks* like a pool link context, try pair lookup first
        if "pool" in t.lower() or "pools" in t.lower() or "geckoterminal" in t.lower() or "dexscreener" in t.lower():
            p = _dex_pair_lookup(direct)
            if p:
                base = p.get("baseToken") or {}
                quote = p.get("quoteToken") or {}
                base_sym = str(base.get("symbol") or "").upper()
                quote_sym = str(quote.get("symbol") or "").upper()
                base_addr = str(base.get("address") or "")
                quote_addr = str(quote.get("address") or "")
                if base_sym == "TON" and quote_addr:
                    return quote_addr
                if quote_sym == "TON" and base_addr:
                    return base_addr
        return direct

    # 2) GeckoTerminal / Dexscreener / ston.fi / dedust.io pool links
    pair_id = None
    for rx in (GECKO_POOL_RE, DEXSCREENER_PAIR_RE, STON_POOL_RE, DEDUST_POOL_RE):
        m = rx.search(t)
        if m:
            pair_id = m.group(1)
            break

    # 3) Fallback: if the message contains a single EQ/UQ-like id, attempt using it as pair id
    if not pair_id:
        m = JETTON_RE.search(t)
        if m:
            pair_id = m.group(1)

    if not pair_id:
        return None

    p = _dex_pair_lookup(pair_id)
    if not p:
        return None
    base = p.get("baseToken") or {}
    quote = p.get("quoteToken") or {}
    base_sym = str(base.get("symbol") or "").upper()
    quote_sym = str(quote.get("symbol") or "").upper()
    base_addr = str(base.get("address") or "")
    quote_addr = str(quote.get("address") or "")
    # choose the non-TON side
    if base_sym == "TON" and quote_addr:
        return quote_addr
    if quote_sym == "TON" and base_addr:
        return base_addr
    # if neither side says TON, still return base (best-effort)
    return base_addr or quote_addr or None

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat or not update.effective_user:
        return
    chat = update.effective_chat
    user = update.effective_user
    text = (update.message.text or "").strip()

    # If admin is setting media for this group
    awaiting_chat = AWAITING_MEDIA.get(user.id)
    if awaiting_chat and chat.id == awaiting_chat:
        # Accept photo or GIF (animation)
        media_file_id = None
        media_type = None
        if update.message.photo:
            media_file_id = update.message.photo[-1].file_id
            media_type = "photo"
        elif update.message.animation:
            media_file_id = update.message.animation.file_id
            media_type = "animation"
        elif update.message.document and (update.message.document.mime_type or "").startswith("image/"):
            media_file_id = update.message.document.file_id
            media_type = "photo"
        if media_file_id:
            g = get_group(chat.id)
            s = g["settings"]
            s["media_file_id"] = media_file_id
            s["media_type"] = media_type
            save_groups()
            AWAITING_MEDIA.pop(user.id, None)
            await update.message.reply_text("✅ Buy image saved for this group.")
            await send_settings(chat.id, context, update.message)
            return
        # If they sent something else, ignore and keep waiting
        return

    # Resolve either a jetton address or a supported link (GT / DexScreener / STON / DeDust)
    # optional token Telegram link in same message
    tg_in = None
    m = re.search(r'(https?://t\.me/[^\s]+|t\.me/[^\s]+)', text)
    if m:
        tg_in = m.group(1)
        if tg_in.startswith('t.me/'):
            tg_in = 'https://' + tg_in
    addr = await _to_thread(resolve_jetton_from_text_sync, text)
    if not addr:
        return

    # decide which chat to configure
    target_chat_id = None
    if chat.type == "private":
        target_chat_id = AWAITING.get(user.id)
        if not target_chat_id:
            await update.message.reply_text("Add the bot to your group, then tap *Configure Token* in that group.", parse_mode="Markdown")
            return
    else:
        # in group: only admins can configure
        if not await is_admin(context.bot, chat.id, user.id):
            return
        # If user pressed configure, it's this chat anyway
        target_chat_id = chat.id

    await configure_group_token(target_chat_id, addr, context, reply_to_chat=chat.id, token_tg=tg_in)

async def configure_group_token(chat_id: int, jetton: str, context: ContextTypes.DEFAULT_TYPE, reply_to_chat: int, token_tg: Optional[str] = None):
    g = get_group(chat_id)
    # 1 token per group: confirm replace if exists and different
    existing = g.get("token") or None
    if existing and existing.get("address") != jetton:
        # Ask confirmation
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Replace", callback_data=f"REPL_{chat_id}_{jetton}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="CANCEL_REPL")]
        ])
        await context.bot.send_message(
            chat_id=reply_to_chat,
            text=f"This group already tracks *{existing.get('symbol') or existing.get('name') or 'a token'}*.\nReplace it with the new token?",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        return
    await _set_token_now(chat_id, jetton, context, reply_to_chat)

async def on_replace_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    chat = q.message.chat if q.message else update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    data = q.data or ""
    if data.startswith("REPL_"):
        # REPL_chatid_jetton
        parts = data.split("_", 2)
        if len(parts) != 3:
            return
        target_chat_id = int(parts[1])
        jetton = parts[2]
        # ensure pressing inside that group and admin
        if chat.id != target_chat_id:
            await q.answer("Open this in the target group.", show_alert=True)
            return
        if not await is_admin(context.bot, chat.id, user.id):
            await q.answer("Admins only.", show_alert=True)
            return
        await _set_token_now(target_chat_id, jetton, context, chat.id)
        try:
            await q.message.delete()
        except Exception:
            pass
        return

    if data == "CANCEL_REPL":
        await q.message.reply_text("Cancelled.")
        return

async def _set_token_now(chat_id: int, jetton: str, context: ContextTypes.DEFAULT_TYPE, reply_chat_id: int):
    # Token metadata (GeckoTerminal first, then TonAPI, then DexScreener)
    gk = gecko_token_info(jetton)
    name = (gk.get("name") or "").strip() if gk else ""
    sym = (gk.get("symbol") or "").strip() if gk else ""
    if not name and not sym:
        info = tonapi_jetton_info(jetton)
        name = (info.get("name") or "").strip()
        sym = (info.get("symbol") or "").strip()
    if not name and not sym:
        dx = dex_token_info(jetton)
        name = (dx.get("name") or "").strip()
        sym = (dx.get("symbol") or "").strip()
    ston_pool = find_stonfi_ton_pair_for_token(jetton)
    dedust_pool = find_dedust_ton_pair_for_token(jetton)

    g = get_group(chat_id)
    g["token"] = {
        "address": jetton,
        "name": name,
        "symbol": sym,
        "telegram": token_tg or "",
        "ston_pool": ston_pool,
        "dedust_pool": dedust_pool,
        "set_at": int(time.time()),
        "last_ston_tx": None,
        "last_dedust_trade": None,
        "burst": {"window_start": int(time.time()), "count": 0},
    }
    save_groups()

    disp = sym or name or "TOKEN"
    await context.bot.send_message(
        chat_id=reply_chat_id,
        text=(
            f"✅ *Token Added*\n"
            f"• Token: *{html.escape(disp)}*\n"
            f"• Address: `{jetton}`\n"
            f"• STON.fi pool: `{ston_pool or 'NONE'}`\n"
            f"• DeDust pool: `{dedust_pool or 'NONE'}`\n\n"
            f"Now posting buys automatically."
        ),
        parse_mode="Markdown"
    )

# -------------------- TRACKERS --------------------
async def _to_thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)

async def poll_once(app: Application):
    # Collect all groups with configured token
    items: List[Tuple[int, Dict[str, Any]]] = []
    for k, g in GROUPS.items():
        if not isinstance(g, dict):
            continue
        token = g.get("token")
        if not isinstance(token, dict):
            continue
        items.append((int(k), g))

    # For each group, poll its pools
    for chat_id, g in items:
        token = g["token"]
        settings = g.get("settings") or DEFAULT_SETTINGS
        min_buy = float(settings.get("min_buy_ton") or 0.0)
        anti = (settings.get("anti_spam") or "MED").upper()
        max_msgs, window = anti_spam_limit(anti)

        burst = token.setdefault("burst", {"window_start": int(time.time()), "count": 0})
        now = int(time.time())
        if now - int(burst.get("window_start", now)) > window:
            burst["window_start"] = now
            burst["count"] = 0

        # STON (STON exported events by blocks)
        if settings.get("enable_ston", True) and token.get("ston_pool"):
            pool = token["ston_pool"]
            try:
                global STON_LAST_BLOCK
                latest = await _to_thread(ston_latest_block)
                if latest is None:
                    raise RuntimeError("no latest block")
                if STON_LAST_BLOCK is None:
                    # initialize slightly behind to avoid missing
                    STON_LAST_BLOCK = max(0, int(latest) - 5)
                from_b = int(STON_LAST_BLOCK) + 1
                to_b = int(latest)
                # cap range to avoid huge pulls
                if to_b - from_b > 60:
                    from_b = to_b - 60
                evs = await _to_thread(ston_events, from_b, to_b)
                # advance cursor even if no events
                STON_LAST_BLOCK = to_b
                # filter swaps for this pool (STON export feed)
                ton_leg = ensure_ton_leg_for_pool(token)
                posted_any = False
                for ev in evs:
                    if (str(ev.get("eventType") or "").lower() != "swap"):
                        continue
                    pair_id = str(ev.get("pairId") or "").strip()
                    if pair_id != pool:
                        continue
                    tx = str(ev.get("txnId") or "").strip()
                    if not tx:
                        continue
                    maker = str(ev.get("maker") or "").strip()
                    a0_in = _to_float(ev.get("amount0In"))
                    a0_out = _to_float(ev.get("amount0Out"))
                    a1_in = _to_float(ev.get("amount1In"))
                    a1_out = _to_float(ev.get("amount1Out"))
                    ton_spent = 0.0
                    token_received = 0.0
                    if ton_leg == 0:
                        if a0_in > 0 and a1_out > 0:
                            ton_spent = a0_in
                            token_received = a1_out
                        else:
                            continue
                    elif ton_leg == 1:
                        if a1_in > 0 and a0_out > 0:
                            ton_spent = a1_in
                            token_received = a0_out
                        else:
                            continue
                    else:
                        continue
                    if ton_spent < min_buy:
                        continue
                    dedupe_key = f"ston:{pool}:{tx}:{maker}"
                    if not dedupe_ok(chat_id, dedupe_key):
                        continue
                    if settings.get("burst_mode", True) and burst["count"] >= max_msgs:
                        continue
                    burst["count"] += 1
                    await post_buy(app, chat_id, token, {"tx": tx, "buyer": maker, "ton": ton_spent, "token_amount": token_received}, source="STON.fi")
                    posted_any = True

                # Fallback for STON.fi v2 swaps (TonAPI tx actions).
                # Some v2 pools don't appear in the export feed with matching pairId/fields,
                # but TonAPI actions still include "Swap tokens" / "Stonfi Swap V2".
                if not posted_any:
                    try:
                        txs = await _to_thread(tonapi_account_transactions, pool, 15)
                        # process oldest -> newest
                        txs = list(reversed(txs))
                        for txo in txs:
                            buys = stonfi_extract_buys_from_tonapi_tx(txo, token["address"])
                            for b in buys:
                                ton_spent = float(b.get("ton") or 0.0)
                                if ton_spent < min_buy:
                                    continue
                                txh = str(b.get("tx") or "").strip() or _tx_hash(txo)
                                buyer = str(b.get("buyer") or "").strip()
                                dedupe_key = f"stonv2:{pool}:{txh}:{buyer}"
                                if not dedupe_ok(chat_id, dedupe_key):
                                    continue
                                if settings.get("burst_mode", True) and burst["count"] >= max_msgs:
                                    continue
                                burst["count"] += 1
                                await post_buy(app, chat_id, token, {"tx": txh, "buyer": buyer, "ton": ton_spent, "token_amount": float(b.get("token_amount") or 0.0)}, source="STON.fi")
                        save_groups()
                    except Exception as _e:
                        log.debug("STON v2 fallback err chat=%s %s", chat_id, _e)
                save_groups()
            except Exception as e:
                log.debug("STON poll err chat=%s %s", chat_id, e)

        # DeDust (DeDust API trades)
        if settings.get("enable_dedust", True) and token.get("dedust_pool"):
            pool = token["dedust_pool"]
            try:
                trades = await _to_thread(dedust_get_trades, pool, 25)
                # process oldest -> newest
                trades = list(reversed(trades))
                last_id = token.get("last_dedust_trade")
                for tr in trades:
                    b = dedust_trade_to_buy(tr, token["address"])
                    if not b:
                        continue
                    trade_id = str(b.get("trade_id") or b.get("tx") or "")
                    if last_id and trade_id <= str(last_id):
                        continue
                    ton_amt = float(b.get("ton") or 0.0)
                    if ton_amt < min_buy:
                        continue
                    dedupe_key = f"dedust:{pool}:{b.get('tx')}:{b.get('buyer')}"
                    if not dedupe_ok(chat_id, dedupe_key):
                        continue
                    if settings.get("burst_mode", True) and burst["count"] >= max_msgs:
                        continue
                    burst["count"] += 1
                    await post_buy(app, chat_id, token, {
                        "tx": b.get("tx"),
                        "buyer": b.get("buyer"),
                        "ton": ton_amt,
                        "token_amount": float(b.get("token_amount") or 0.0),
                    }, source="DeDust")
                    token["last_dedust_trade"] = trade_id
                save_groups()
            except Exception as e:
                log.debug("DeDust poll err chat=%s %s", chat_id, e)


    # save seen occasionally
    save_seen()

async def post_buy(app: Application, chat_id: int, token: Dict[str, Any], b: Dict[str, Any], source: str):
    sym = (token.get("symbol") or "").strip()
    name = (token.get("name") or "").strip()
    title = sym or name or "TOKEN"

    ton_amt = float(b.get("ton") or 0.0)
    tok_amt = b.get("token_amt") if b.get("token_amt") is not None else b.get("token_amount")
    tok_symbol = b.get("token_symbol") or token.get("symbol") or sym or ""

    buyer_full = str(b.get("buyer") or "")
    buyer_short = _short_addr(buyer_full)
    tx = str(b.get("tx") or "")

    ston_pool = token.get("ston_pool") or ""
    dedust_pool = token.get("dedust_pool") or ""
    pool_for_market = ston_pool or dedust_pool

    # Market data (prefer GeckoTerminal)
    price_usd = liq_usd = mc_usd = None
    if pool_for_market:
        pinfo = gecko_pool_info(pool_for_market)
        if pinfo:
            try:
                price_usd = float(pinfo.get("price_usd")) if pinfo.get("price_usd") is not None else None
            except Exception:
                price_usd = None
            try:
                liq_usd = float(pinfo.get("liquidity_usd")) if pinfo.get("liquidity_usd") is not None else None
            except Exception:
                liq_usd = None
            try:
                mc_usd = float(pinfo.get("market_cap_usd")) if pinfo.get("market_cap_usd") is not None else None
            except Exception:
                mc_usd = None

    if (price_usd is None or mc_usd is None) and token.get("address"):
        tinfo = gecko_token_info(token["address"])
        if tinfo:
            if price_usd is None:
                try:
                    price_usd = float(tinfo.get("price_usd")) if tinfo.get("price_usd") is not None else None
                except Exception:
                    pass
            if mc_usd is None:
                try:
                    mc_usd = float(tinfo.get("market_cap_usd")) if tinfo.get("market_cap_usd") is not None else None
                except Exception:
                    pass

    # Holders (best-effort via TonAPI)
    holders = None
    try:
        info = tonapi_jetton_info(token.get("address") or "")
        h = info.get("holders_count") if isinstance(info, dict) else None
        if h is not None:
            holders = int(h)
    except Exception:
        holders = None

    # Links row
    pair_for_links = pool_for_market or ""
    tx_url = f"https://tonviewer.com/transaction/{tx}" if tx else None
    gt_url = gecko_terminal_pool_url(pair_for_links) if pair_for_links else None
    dex_url = f"https://dexscreener.com/ton/{pair_for_links}" if pair_for_links else None
    tg_link = token.get("telegram") or ""
    trending = TRENDING_URL


    # ----- Premium message style (HTML) -----
    # Buy strength diamonds (per-group setting)
    gcfg = get_group(chat_id)
    s = (gcfg.get("settings") or {})
    strength = (s.get("strength") or "MED").upper()
    if strength == "LOW":
        thr = [0.2, 0.5, 1, 2, 5]
    elif strength == "HIGH":
        thr = [1, 2, 5, 10, 20]
    else:
        thr = [0.5, 1, 2, 5, 10]
    diamonds = "💎" * sum(1 for t in thr if ton_amt >= t)

    # Buyer clickable
    buyer_line = ""
    if buyer_full:
        buyer_line = f'<a href="https://tonviewer.com/address/{html.escape(buyer_full)}">{html.escape(buyer_short)}</a>'

    # Token amounts (dynamic decimals, never swap with TON)
    tok_line = ""
    if tok_amt and tok_symbol:
        def _fmt_amount(v: float) -> str:
            av = abs(v)
            if av >= 1_000_000:
                d = 0
            elif av >= 1_000:
                d = 2
            elif av >= 1:
                d = 2
            elif av >= 0.01:
                d = 4
            else:
                d = 6
            s = f"{v:,.{d}f}"
            # trim trailing zeros
            if "." in s:
                s = s.rstrip("0").rstrip(".")
            return s

        try:
            _ta = float(tok_amt)
            tok_line = f"🪙 <b>{_fmt_amount(_ta)} {html.escape(str(tok_symbol))}</b>"
        except Exception:
            tok_line = f"🪙 <b>{html.escape(str(tok_amt))} {html.escape(str(tok_symbol))}</b>"

    # Build links row (keep only TX | GT | DexS | Telegram | Trending)
    link_parts: List[str] = []
    if tx_url:
        link_parts.append(f'<a href="{html.escape(tx_url)}">TX</a>')
    if gt_url:
        link_parts.append(f'<a href="{html.escape(gt_url)}">GT</a>')
    if dex_url:
        link_parts.append(f'<a href="{html.escape(dex_url)}">DexS</a>')
    if tg_link:
        link_parts.append(f'<a href="{html.escape(tg_link)}">Telegram</a>')
    if trending:
        link_parts.append(f'<a href="{html.escape(trending)}">Trending</a>')
    links_row = " | ".join(link_parts)

    parts: List[str] = []
    parts.append(f"<b>{html.escape(title)} Buy!</b>")
    if diamonds:
        parts.append(diamonds)
    ton_price = get_ton_price_usd()
    if ton_price:
        parts.append(f"💎 <b>{ton_amt:,.2f} TON</b> (${ton_amt*ton_price:,.2f})")
    else:
        parts.append(f"💎 <b>{ton_amt:,.2f} TON</b>")
    if tok_line:
        parts.append(tok_line)
    if buyer_line:
        parts.append(buyer_line)

    # Stats
    if price_usd is not None:
        parts.append(f"Price: ${price_usd:,.6f}")
    if liq_usd is not None:
        parts.append(f"Liquidity: ${liq_usd:,.0f}")
    if mc_usd is not None:
        parts.append(f"MCap: ${mc_usd:,.0f}")
    if holders is not None:
        parts.append(f"Holders: {holders}")

    if links_row:
        parts.append(links_row)

    msg = "\n".join(parts)


    # Single buy button (dTrade referral + CA)
    ref = (DTRADE_REF or "https://t.me/dtrade?start=11TYq7LInG").rstrip("_")
    ca = token.get("address") or ""
    buy_url = f"{ref}_{ca}" if ca else ref
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Buy {sym or 'Token'} with dTrade", url=buy_url)]])

    try:
        # Optional per-group media (photo/GIF) attached to every buy
        gcfg = get_group(chat_id)
        s = (gcfg.get("settings") or {})
        media_id = s.get("media_file_id")
        media_type = s.get("media_type")
        if media_id:
            if media_type == "animation":
                await app.bot.send_animation(chat_id=chat_id, animation=media_id, caption=msg, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
            else:
                await app.bot.send_photo(chat_id=chat_id, photo=media_id, caption=msg, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        else:
            await app.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        # fallback without keyboard/markdown
        try:
            await app.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            log.debug("send fail %s", e)

async def tracker_loop(app: Application):
    while True:
        try:
            await poll_once(app)
        except Exception as e:
            log.exception("tracker loop error: %s", e)
        await asyncio.sleep(POLL_INTERVAL)

# -------------------- Chat member welcome --------------------
async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # when bot added to group, post premium intro
    try:
        my_chat_member = update.my_chat_member
        if not my_chat_member:
            return
        chat = my_chat_member.chat
        new = my_chat_member.new_chat_member
        if chat.type not in ("group","supergroup"):
            return
        if new and new.status in ("member","administrator"):
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⚙️ Configure Token", callback_data="CFG_GROUP")],
                [InlineKeyboardButton("🛠 Settings", callback_data="SET_GROUP")],
                [InlineKeyboardButton("📊 Status", callback_data="STATUS_GROUP")],
            ])
            await context.bot.send_message(
                chat_id=chat.id,
                text="✅ *SpyTON BuyBot connected*\nTap *Configure Token* to start posting buys.",
                reply_markup=kb,
                parse_mode="Markdown"
            )
    except Exception:
        return

# -------------------- HEALTH SERVER --------------------
app_flask = Flask(__name__)

@app_flask.get("/")
def health():
    return "ok", 200

def run_flask():
    port = int(os.getenv("PORT", "8080"))
    app_flask.run(host="0.0.0.0", port=port)

# -------------------- MAIN --------------------
async def post_init(app: Application):
    # start tracker
    app.create_task(tracker_loop(app))
    log.info("Tracker started.")

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing.")
    application = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CallbackQueryHandler(on_replace_button, pattern=r"^(REPL_|CANCEL_REPL$)"))
    application.add_handler(CallbackQueryHandler(on_button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # flask in thread for Railway health
    import threading
    threading.Thread(target=run_flask, daemon=True).start()

    log.info("SpyTON Public BuyBot starting...")
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
# -------------------- STON API (exported events) --------------------
STON_BASE = os.getenv("STON_BASE", "https://api.ston.fi").rstrip("/")
STON_LATEST_BLOCK_URL = f"{STON_BASE}/export/dexscreener/v1/latest-block"
STON_EVENTS_URL = f"{STON_BASE}/export/dexscreener/v1/events"
STON_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
STON_LAST_BLOCK: Optional[int] = None

def ston_latest_block() -> Optional[int]:
    try:
        r = requests.get(STON_LATEST_BLOCK_URL, headers=STON_HEADERS, timeout=20)
        if r.status_code != 200:
            return None
        js = r.json()
        if isinstance(js, dict):
            v = js.get("block") or js.get("latestBlock") or js.get("latest_block")
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)
        if isinstance(js, int):
            return js
        if isinstance(js, str) and js.isdigit():
            return int(js)
        return None
    except Exception:
        return None

def ston_events(from_block: int, to_block: int) -> List[Dict[str, Any]]:
    params = {"fromBlock": int(from_block), "toBlock": int(to_block)}
    try:
        r = requests.get(STON_EVENTS_URL, params=params, headers=STON_HEADERS, timeout=20)
        if r.status_code != 200:
            return []
        js = r.json()
        if isinstance(js, list):
            return [x for x in js if isinstance(x, dict)]
        if isinstance(js, dict) and isinstance(js.get("events"), list):
            return [x for x in js["events"] if isinstance(x, dict)]
        return []
    except Exception:
        return []

def ensure_ton_leg_for_pool(token: Dict[str, Any]) -> Optional[int]:
    # cache 0/1 where TON is leg0(amount0*) or leg1(amount1*)
    tl = token.get("ton_leg")
    if tl in (0,1):
        return int(tl)
    pool = token.get("ston_pool")
    if not pool:
        return None
    meta = _dex_pair_lookup(pool)
    if not isinstance(meta, dict):
        return None
    base = (meta.get("baseToken") or {})
    quote = (meta.get("quoteToken") or {})
    base_sym = str(base.get("symbol") or "").upper()
    quote_sym = str(quote.get("symbol") or "").upper()
    if base_sym == "TON":
        token["ton_leg"] = 0
        return 0
    if quote_sym == "TON":
        token["ton_leg"] = 1
        return 1
    return None


