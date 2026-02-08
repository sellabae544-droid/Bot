
import os, json, time, asyncio, logging, re, html
from typing import Any, Dict, Optional, List, Tuple
from urllib.parse import urlparse
import requests

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
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "2.0"))
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

# -------------------- HELPERS --------------------
JETTON_RE = re.compile(r"(?<![A-Za-z0-9_-])([EU]Q[A-Za-z0-9_-]{40,120})(?![A-Za-z0-9_-])")

# Deep-linking to DM for group configuration (Crypton-style)
# Set BOT_USERNAME env to your bot username without @ (e.g. SpyTONPublicBuyBot)
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@").strip()

def dm_cfg_url(chat_id: int) -> str:
    """Open the bot DM with /start cfg_<chat_id>"""
    if not BOT_USERNAME:
        return ""
    return f"https://t.me/{BOT_USERNAME}?start=cfg_{chat_id}"
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

def _short_addr_safe(a: str) -> str:
    """Shorten and strip non-alphanumerics for safe display."""
    if not a:
        return ""
    clean = "".join(ch for ch in a if ch.isalnum())
    if not clean:
        clean = a
    if len(clean) <= 10:
        return clean
    return clean[:4] + "…" + clean[-4:]

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
        # Deep-link DM config: /start cfg_<group_id>
        if getattr(context, "args", None) and len(context.args) >= 1:
            arg0 = (context.args[0] or "").strip()
            if arg0.startswith("cfg_"):
                try:
                    gid = int(arg0.split("_", 1)[1])
                    AWAITING[update.effective_user.id] = gid
                    await update.message.reply_text(
                        "Send the token CA (EQ…/UQ…) or a supported link (GT/DexS/STON/DeDust).\n\n"
                        "You can also add the token Telegram link after the CA.\n"
                        "Example: EQ... https://t.me/YourTokenTG"
                    )
                    return
                except Exception:
                    pass
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
        url = dm_cfg_url(chat.id)
        if not url:
            await q.message.reply_text(
                "⚠️ BOT_USERNAME is not set on the server.\n\n"
                "Set env `BOT_USERNAME` to your bot username, then try again."
            )
            return
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Configure in DM", url=url)]])
        await q.message.reply_text(
            "To configure safely (Crypton-style), use DM:\n"
            "1) Tap *Configure in DM*\n"
            "2) Send your token CA (and optional Telegram link)",
            reply_markup=kb,
            parse_mode="Markdown"
        )
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

    # Resolve either a jetton address or a supported link (GT / DexScreener / STON / DeDust)
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

    await configure_group_token(target_chat_id, addr, context, reply_to_chat=chat.id, tg_link=tg_link)

async def configure_group_token(chat_id: int, jetton: str, context: ContextTypes.DEFAULT_TYPE, reply_to_chat: int, tg_link: Optional[str] = None):
    g = get_group(chat_id)
    # 1 token per group: confirm replace if exists and different
    existing = g.get("token") or None
    if existing and existing.get("address") != jetton:
        # Ask confirmation
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Replace", callback_data=f"REPL_{chat_id}_{jetton}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="CANCEL_REPL")]
        ])
        context.user_data["pending_replace"] = {"chat_id": chat_id, "jetton": jetton, "tg_link": tg_link, "reply_to_chat": reply_to_chat}
        await context.bot.send_message(
            chat_id=reply_to_chat,
            text=f"This group already tracks *{existing.get('symbol') or existing.get('name') or 'a token'}*.\nReplace it with the new token?",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        return
    await _set_token_now(chat_id, jetton, tg_link, context, reply_to_chat)

async def on_replace_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    chat = q.message.chat if q.message else update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    pending = context.user_data.get("pending_replace")
    if not pending:
        await context.bot.send_message(chat_id=chat.id, text="No pending token to replace. Tap Configure Token again.")
        return

    chat_id = int(pending.get("chat_id"))
    jetton = str(pending.get("jetton"))
    tg_link = pending.get("tg_link")
    reply_to_chat = int(pending.get("reply_to_chat", chat.id))

    # Clear pending before applying (avoid double clicks)
    context.user_data.pop("pending_replace", None)

    await _set_token_now(chat_id, jetton, tg_link, context, reply_to_chat)

    try:
        await q.edit_message_text("✅ Token replaced. The bot will start posting buys here.")
    except Exception:
        pass

