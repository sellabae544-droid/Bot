
import os, json, time, asyncio, logging, re, html, base64
from typing import Any, Dict, Optional, List, Tuple
from urllib.parse import urlparse, quote
import requests

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
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

    # Crypton-style options
    "strength_on": True,
    "strength_emoji": "🟢",
    "strength_step_ton": 5.0,   # 1 strength unit per X TON
    "strength_max": 30,         # max emojis

    # Optional buy alert image
    # If enabled and a file_id is set, the bot will send a Telegram photo (not a link).
    "buy_image_on": False,
    "buy_image_file_id": "",

    # Min buy can be TON or USD
    "min_buy_unit": "TON",   # TON | USD
    "min_buy_usd": 0.0,

    # Layout toggles
    "show_price": True,
    "show_liquidity": True,
    "show_mcap": True,
    "show_holders": True,
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
AWAITING: Dict[int, Dict[str, Any]] = {}  # user_id -> {'group_id': int, 'stage': str, 'dex': str}

# user_id -> chat_id awaiting social link input
AWAITING_SOCIAL: Dict[int, Dict[str, Any]] = {}  # {'chat_id': int, 'field': 'telegram'|'website'|'twitter'}

# user_id -> chat_id awaiting buy image photo
AWAITING_IMAGE: Dict[int, int] = {}

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




# -------------------- CACHES --------------------
TX_LT_CACHE: Dict[str, Tuple[int, str]] = {}  # key=f"{account}:{lt}" -> (ts, hash)
MARKET_CACHE: Dict[str, Dict[str, Any]] = {}  # key=pool or token -> {ts, price_usd, liq_usd, mc_usd, holders}

# TON/USD price cache (for USD min-buy)
TON_PRICE_CACHE: Dict[str, Any] = {"ts": 0, "usd": None}

BOT_USERNAME_CACHE = None

async def get_bot_username(bot):
    global BOT_USERNAME_CACHE
    if BOT_USERNAME_CACHE:
        return BOT_USERNAME_CACHE
    me = await bot.get_me()
    BOT_USERNAME_CACHE = me.username
    return BOT_USERNAME_CACHE


async def stonfi_latest_swaps(pool: str, limit: int = 25) -> List[Dict[str, Any]]:
    """Best-effort: fetch latest pool transactions from TonAPI and treat them as swaps for warmup.
    This is used only to avoid posting old buys right after configuration."""
    try:
        txs = await _to_thread(tonapi_account_transactions, pool, int(limit))
        out = []
        for txo in txs or []:
            h = _tx_hash(txo)
            if h:
                out.append({"hash": h})
        return out
    except Exception:
        return []

async def dedust_latest_trades(pool: str, limit: int = 25) -> List[Dict[str, Any]]:
    """Fetch latest trades from DeDust API in a thread."""
    try:
        return await _to_thread(dedust_get_trades, pool, int(limit))
    except Exception:
        return []

async def warmup_seen_for_chat(chat_id: int, ston_pool: str|None, dedust_pool: str|None):
    """Mark latest swaps as seen so the bot does not spam old buys right after configuration.
    Also sets baseline last_* ids so we skip anything older than the moment the token was configured."""
    try:
        bucket = SEEN.setdefault(str(chat_id), {})
        newest_ston = None
        newest_dedust = None

        # STON.fi (warmup by pool tx hashes from TonAPI)
        if ston_pool:
            swaps = await stonfi_latest_swaps(ston_pool, limit=40)
            for s in swaps:
                txhash = (s.get('tx_hash') or s.get('txHash') or s.get('hash') or '').strip()
                if txhash:
                    bucket[f"ston:{ston_pool}:{txhash}"] = int(time.time())
                    if newest_ston is None:
                        newest_ston = txhash  # first item is newest

        # DeDust (warmup by latest trade ids and tx hashes where available)
        if dedust_pool:
            trades = await dedust_latest_trades(dedust_pool, limit=60)

            # Some DeDust endpoints may return trades in oldest->newest order.
            # To prevent "old buys" spam, we always baseline to the MAX lt/trade_id we can see.
            max_lt_i = None
            max_ts_i = None
            for t in trades:
                lt_raw = (t.get('lt') or t.get('trade_id') or t.get('id') or '')
                lt_s = str(lt_raw).strip()
                if lt_s:
                    try:
                        lt_i = int(lt_s)
                        if (max_lt_i is None) or (lt_i > max_lt_i):
                            max_lt_i = lt_i
                    except Exception:
                        pass

                # timestamp baseline (ms or sec)
                ts_raw = (t.get('timestamp') or t.get('time') or t.get('ts') or 0)
                try:
                    ts_i = int(float(ts_raw or 0))
                    if ts_i > 10_000_000_000:  # ms
                        ts_i = ts_i // 1000
                    if ts_i > 0 and ((max_ts_i is None) or (ts_i > max_ts_i)):
                        max_ts_i = ts_i
                except Exception:
                    pass

                txhash = (t.get('tx_hash') or t.get('txHash') or t.get('hash') or '').strip()
                if txhash:
                    bucket[f"dedust:{dedust_pool}:{txhash}"] = int(time.time())

            if max_lt_i is not None:
                newest_dedust = str(max_lt_i)
            newest_dedust_ts = max_ts_i

        # save baselines into group token so polling skips older history
        g = GROUPS.get(str(chat_id)) or {}
        tok = g.get("token") if isinstance(g, dict) else None
        if isinstance(tok, dict):
            if newest_ston:
                tok["last_ston_tx"] = newest_ston
            if newest_dedust:
                tok["last_dedust_trade"] = newest_dedust
            if newest_dedust_ts:
                tok["last_dedust_ts"] = int(newest_dedust_ts)

            # baseline: ignore anything before now
            tok["ignore_before_ts"] = int(time.time())
            # baseline for STON export cursor: start from current latest block
            try:
                latest = await _to_thread(ston_latest_block)
                if latest is not None:
                    tok["ston_last_block"] = int(latest)
            except Exception:
                pass
            save_groups()

        save_seen()
    except Exception:
        return

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

def tonapi_jetton_info(jetton: str) -> Dict[str, Any]:
    """Fetch basic jetton metadata from TonAPI.

    We keep this as a small dict used across the bot. TonAPI responses often
    include holders_count at the top-level.
    """
    out: Dict[str, Any] = {"name": "", "symbol": "", "holders_count": None}
    js = tonapi_get(f"{TONAPI_BASE}/v2/jettons/{jetton}")
    if not js:
        return out
    meta = js.get("metadata") or {}
    out["name"] = str(meta.get("name") or js.get("name") or "").strip()
    out["symbol"] = str(meta.get("symbol") or js.get("symbol") or "").strip()
    # TonAPI commonly exposes holders_count at top-level
    try:
        hc = js.get("holders_count")
        if hc is not None:
            out["holders_count"] = int(hc)
    except Exception:
        pass
    return out

def tonapi_jetton_holders_count(jetton: str) -> Optional[int]:
    """Best-effort holders count. Some TonAPI responses don't include holders_count on the main jetton endpoint."""
    try:
        data = tonapi_get(f"{TONAPI_BASE}/v2/jettons/{jetton}/holders", params={"limit": 1, "offset": 0})
        if not isinstance(data, dict):
            return None
        # TonAPI may return total/total_count in root
        for k in ("total", "total_count", "count", "holders", "holders_count"):
            v = data.get(k)
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)
        # or nested
        meta = data.get("metadata") or {}
        if isinstance(meta, dict):
            v = meta.get("total") or meta.get("total_count")
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)
        return None
    except Exception:
        return None


def tonapi_account_transactions(address: str, limit: int = 12) -> List[Dict[str, Any]]:
    js = tonapi_get(f"{TONAPI_BASE}/v2/blockchain/accounts/{address}/transactions", params={"limit": limit})
    txs = js.get("transactions") if isinstance(js, dict) else None
    return txs if isinstance(txs, list) else []

def tonapi_account_events(address: str, limit: int = 10) -> List[Dict[str, Any]]:
    js = tonapi_get(f"{TONAPI_BASE}/v2/accounts/{address}/events", params={"limit": limit})
    ev = js.get("events") if isinstance(js, dict) else None
    return ev if isinstance(ev, list) else []

def tonapi_find_tx_hash_by_lt(account: str, lt: str, limit: int = 40) -> str:
    """Find a real transaction hash for an account by LT (with cache + adaptive scan).

    Some DEX trade APIs expose only LT; Tonviewer needs the real tx hash.
    We scan recent account transactions from TonAPI and match by LT.
    """
    account = str(account or "").strip()
    if not account:
        return ""
    try:
        lt_s = str(int(str(lt).strip()))
    except Exception:
        return ""

    cache_key = f"{account}:{lt_s}"
    now = int(time.time())
    # 24h cache
    cached = TX_LT_CACHE.get(cache_key)
    if cached and now - int(cached[0]) < 86400:
        return str(cached[1] or "").strip()

    # Adaptive scan sizes (fast -> deeper)
    scan_limits = [max(40, int(limit or 40)), 120, 300, 600]
    for lim in scan_limits:
        try:
            txs = tonapi_account_transactions(account, limit=lim)
            for tx in txs:
                if not isinstance(tx, dict):
                    continue
                tid = tx.get("transaction_id") or {}
                tx_lt = str(tid.get("lt") or tx.get("lt") or "").strip()
                if not tx_lt:
                    continue
                try:
                    if str(int(tx_lt)) != lt_s:
                        continue
                except Exception:
                    continue
                h = tid.get("hash") or tx.get("hash") or tx.get("tx_hash") or tx.get("id")
                h = str(h or "").strip()
                if h:
                    TX_LT_CACHE[cache_key] = (now, h)
                    return h
        except Exception:
            # brief retry on transient errors
            try:
                time.sleep(0.35)
            except Exception:
                pass
            continue

    return ""

def ton_usd_price() -> Optional[float]:
    """Fetch TON/USD price (cached). Used only when min_buy_unit == USD."""
    now = int(time.time())
    try:
        if TON_PRICE_CACHE.get("usd") is not None and now - int(TON_PRICE_CACHE.get("ts") or 0) < 120:
            return float(TON_PRICE_CACHE.get("usd"))
    except Exception:
        pass
    # Best-effort CoinGecko simple price
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "the-open-network", "vs_currencies": "usd"},
            timeout=10,
            headers={"accept": "application/json", "user-agent": "SpyTONBuyBot/1.0"},
        )
        if r.status_code == 200:
            js = r.json()
            usd = js.get("the-open-network", {}).get("usd")
            if usd is not None:
                TON_PRICE_CACHE["usd"] = float(usd)
                TON_PRICE_CACHE["ts"] = now
                return float(usd)
    except Exception:
        pass
    return None

def min_buy_ton_threshold(settings: Dict[str, Any]) -> float:
    """Return the TON amount threshold implied by settings (TON or USD)."""
    unit = str(settings.get("min_buy_unit") or "TON").upper()
    if unit != "USD":
        try:
            return float(settings.get("min_buy_ton") or 0.0)
        except Exception:
            return 0.0
    try:
        usd_thr = float(settings.get("min_buy_usd") or 0.0)
    except Exception:
        usd_thr = 0.0
    if usd_thr <= 0:
        return 0.0
    p = ton_usd_price()
    if not p or p <= 0:
        # If we can't fetch TON price, don't block buys.
        return 0.0
    return usd_thr / p

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
    """Extract a tx hash from various TonAPI / DEX payload shapes."""
    if not isinstance(tx, dict):
        return ""
    # Common flat keys
    h = tx.get("hash") or tx.get("tx_hash") or tx.get("transaction_hash") or tx.get("id")
    if isinstance(h, str) and h.strip():
        return h.strip()
    # TonAPI account tx shape: {"transaction_id": {"hash": "...", "lt": "..."}, ...}
    tid = tx.get("transaction_id")
    if isinstance(tid, dict):
        h2 = tid.get("hash") or tid.get("tx_hash")
        if isinstance(h2, str) and h2.strip():
            return h2.strip()
    # Some payloads wrap in {"event": {"tx_hash": ...}}
    ev = tx.get("event")
    if isinstance(ev, dict):
        h3 = ev.get("tx_hash") or ev.get("hash")
        if isinstance(h3, str) and h3.strip():
            return h3.strip()
    return ""

def _normalize_tx_hash_to_hex(h: Any) -> str:
    """Return a 64-char lowercase hex tx hash when possible.

    Tonviewer transaction link format: https://tonviewer.com/transaction/<hash as hex>.
    Some APIs return base64url-encoded 32-byte hashes; we convert those to hex.
    """
    if h is None:
        return ""
    s = str(h).strip()
    if not s:
        return ""
    # Already hex?
    if re.fullmatch(r"[0-9a-fA-F]{64}", s):
        return s.lower()
    # If looks like base64url, try decode -> 32 bytes
    try:
        pad = "=" * ((4 - (len(s) % 4)) % 4)
        b = base64.urlsafe_b64decode(s + pad)
        if isinstance(b, (bytes, bytearray)) and len(b) == 32:
            return bytes(b).hex()
    except Exception:
        pass
    return ""

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
    # Prefer real transaction hash when present (hex or base64url). Fall back to event id.
    tx_hash = str(ev.get("hash") or ev.get("tx_hash") or ev.get("transaction_hash") or ev.get("id") or ev.get("event_id") or "")
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
        # Deep-link from group "Click Here!" button: /start cfg_<group_id>
        if context.args:
            arg = str(context.args[0])
            if arg.startswith("cfg_"):
                try:
                    group_id = int(arg.split("_", 1)[1])
                except Exception:
                    group_id = None
                if group_id:
                    # Auto-detect mode: user sends CA, we resolve STON.fi + DeDust pools automatically.
                    AWAITING[update.effective_user.id] = {"group_id": group_id, "stage": "CA", "dex": "both"}
                    await update.message.reply_text(
                        "✅ *SpyTON BuyBot connected*\n\n"
                        "Now send the token CA here in DM.\n"
                        "I will auto-detect *STON.fi* / *DeDust* pools and start posting buys in your group.\n\n"
                        "Tip: you can also include the token Telegram link in the same message.\n"
                        "Example:\n`<CA> https://t.me/YourToken`",
                        parse_mode="Markdown"
                    )
                    return
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
            [InlineKeyboardButton("⚙️ Token Settings", callback_data="TOKENSET_GROUP")],
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
        # Crypton-style: group button opens DM config (deep-link) so you don't have to reply in group.
        if not await is_admin(context.bot, chat.id, user.id):
            await q.answer("Admins only.", show_alert=True)
            return
        bot_username = await get_bot_username(context.bot)
        deep = f"https://t.me/{bot_username}?start=cfg_{chat.id}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Click Here!", url=deep)]])
        await q.message.reply_text(
            "To continue, click *Click Here!* and send your token CA in DM.",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        await q.answer()
        return


    # DEX selection in private DM config
    if data.startswith("DEX_STON_") or data.startswith("DEX_DEDUST_"):
        try:
            group_id = int(data.split("_", 2)[2])
        except Exception:
            group_id = None
        if not group_id:
            return
        dex = "ston" if data.startswith("DEX_STON_") else "dedust"
        AWAITING[user.id] = {"group_id": group_id, "stage": "CA", "dex": dex}
        await q.edit_message_text(
            "Send the token CA now (EQ… / UQ…) or a supported link (GT/DexS/STON/DeDust).\n\n"
            "Optional: add the token Telegram link after the CA.\n"
            "Example: EQ... https://t.me/YourTokenTG"
        )
        return

    if data == "TOKENSET_GROUP":
        if not await is_admin(context.bot, chat.id, user.id):
            await q.answer("Admins only.", show_alert=True)
            return
        await send_token_settings(chat.id, context, q.message)
        return

    if data.startswith("TS_"):
        if not await is_admin(context.bot, chat.id, user.id):
            await q.answer("Admins only.", show_alert=True)
            return
        await handle_token_settings_button(chat.id, data, update, context)
        return

    if data == "SET_GROUP":
        # Settings should open the Crypton-style module menu (Token Settings),
        # not the legacy quick-toggles panel.
        if not await is_admin(context.bot, chat.id, user.id):
            await q.answer("Admins only.", show_alert=True)
            return
        await send_token_settings(chat.id, context, q.message)
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
            prev = bool(s.get("enable_dedust", True))
            s["enable_dedust"] = not prev
            # If turning ON, baseline DeDust so it never dumps old buys.
            if (not prev) and bool(s["enable_dedust"]):
                tok = g.get("token") if isinstance(g, dict) else None
                if isinstance(tok, dict) and tok.get("dedust_pool"):
                    try:
                        await warmup_seen_for_chat(chat.id, None, tok.get("dedust_pool"))
                    except Exception:
                        pass
                    tok["init_done"] = False
                    save_groups()
        elif data == "TOG_BURST":
            s["burst_mode"] = not bool(s.get("burst_mode", True))
        elif data == "TOG_STRENGTH":
            s["strength_on"] = not bool(s.get("strength_on", True))
        elif data == "TOG_IMAGE":
            s["buy_image_on"] = not bool(s.get("buy_image_on", False))
        save_groups()
        await send_settings(chat.id, context, q.message, edit=True)
        return

    if data == "IMG_SET":
        if not await is_admin(context.bot, chat.id, user.id):
            await q.answer("Admins only.", show_alert=True)
            return
        # Next photo from this admin will be saved as the buy image for this group.
        AWAITING_IMAGE[user.id] = chat.id
        await q.message.reply_text("Send the *buy image* now as a Telegram photo (not a file).", parse_mode="Markdown")
        return

    if data == "IMG_CLEAR":
        if not await is_admin(context.bot, chat.id, user.id):
            await q.answer("Admins only.", show_alert=True)
            return
        g = get_group(chat.id)
        g["settings"]["buy_image_file_id"] = ""
        g["settings"]["buy_image_on"] = False
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

    if data.startswith("STEP_"):
        if not await is_admin(context.bot, chat.id, user.id):
            await q.answer("Admins only.", show_alert=True)
            return
        g = get_group(chat.id)
        s = g["settings"]
        step = float(data.split("_", 1)[1])
        s["strength_step_ton"] = step
        save_groups()
        await send_settings(chat.id, context, q.message, edit=True)
        return

    if data.startswith("MAX_"):
        if not await is_admin(context.bot, chat.id, user.id):
            await q.answer("Admins only.", show_alert=True)
            return
        g = get_group(chat.id)
        s = g["settings"]
        mx = int(data.split("_", 1)[1])
        s["strength_max"] = mx
        save_groups()
        await send_settings(chat.id, context, q.message, edit=True)
        return

    if data.startswith("EMO_"):
        if not await is_admin(context.bot, chat.id, user.id):
            await q.answer("Admins only.", show_alert=True)
            return
        g = get_group(chat.id)
        s = g["settings"]
        if data == "EMO_GREEN":
            s["strength_emoji"] = "🟢"
        elif data == "EMO_PLANE":
            s["strength_emoji"] = "✈️"
        elif data == "EMO_DIAMOND":
            s["strength_emoji"] = "💎"
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

    strength = "ON ✅" if s.get("strength_on", True) else "OFF ❌"
    strength_step = float(s.get("strength_step_ton") or 5.0)
    strength_max = int(s.get("strength_max") or 30)
    strength_emoji = str(s.get("strength_emoji") or "🟢")

    img_on = bool(s.get("buy_image_on", False))
    img_set = bool((s.get("buy_image_file_id") or "").strip())
    img = "ON ✅" if img_on else "OFF ❌"
    img_note = "set" if img_set else "not set"

    text = (
        "*SpyTON BuyBot Settings*\n"
        f"• STON.fi: *{ston}*\n"
        f"• DeDust: *{dedust}*\n"
        f"• Burst mode: *{burst}*\n"
        f"• Anti-spam: *{anti}*\n"
        f"• Min buy (TON): *{min_buy}*\n"
        f"• Buy strength: *{strength}* ({strength_emoji}, step {strength_step} TON, max {strength_max})\n"
        f"• Buy image: *{img}* ({img_note})\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"STON.fi: {ston}", callback_data="TOG_STON"),
         InlineKeyboardButton(f"DeDust: {dedust}", callback_data="TOG_DEDUST")],
        [InlineKeyboardButton(f"Burst: {burst}", callback_data="TOG_BURST")],
        [InlineKeyboardButton(f"Strength: {strength}", callback_data="TOG_STRENGTH"),
         InlineKeyboardButton(f"Image: {img}", callback_data="TOG_IMAGE")],
        [InlineKeyboardButton("🖼 Set Buy Image", callback_data="IMG_SET"),
         InlineKeyboardButton("🗑 Clear Image", callback_data="IMG_CLEAR")],
        [InlineKeyboardButton("Min 0", callback_data="MIN_0"),
         InlineKeyboardButton("0.1", callback_data="MIN_0.1"),
         InlineKeyboardButton("0.5", callback_data="MIN_0.5"),
         InlineKeyboardButton("1", callback_data="MIN_1"),
         InlineKeyboardButton("5", callback_data="MIN_5")],
        [InlineKeyboardButton("Step 1", callback_data="STEP_1"),
         InlineKeyboardButton("5", callback_data="STEP_5"),
         InlineKeyboardButton("10", callback_data="STEP_10"),
         InlineKeyboardButton("20", callback_data="STEP_20")],
        [InlineKeyboardButton("Max 10", callback_data="MAX_10"),
         InlineKeyboardButton("15", callback_data="MAX_15"),
         InlineKeyboardButton("30", callback_data="MAX_30")],
        [InlineKeyboardButton("🟢", callback_data="EMO_GREEN"),
         InlineKeyboardButton("✈️", callback_data="EMO_PLANE"),
         InlineKeyboardButton("💎", callback_data="EMO_DIAMOND")],
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


# -------------------- Crypton-style Token Settings (modules) --------------------
async def send_token_settings(chat_id: int, context: ContextTypes.DEFAULT_TYPE, msg, edit: bool=False):
    g = get_group(chat_id)
    tok = g.get("token") if isinstance(g, dict) else None
    s = g.get("settings") or DEFAULT_SETTINGS

    token_name = "None"
    if isinstance(tok, dict):
        token_name = (tok.get("symbol") or tok.get("name") or "TOKEN").strip()
    paused = bool(tok.get("paused", False)) if isinstance(tok, dict) else False

    unit = str(s.get("min_buy_unit") or "TON").upper()
    min_buy_disp = f"{float(s.get('min_buy_ton') or 0.0)} TON" if unit != "USD" else f"${float(s.get('min_buy_usd') or 0.0)}"

    text = (
        "*Token Settings*\n"
        f"• Token: *{html.escape(token_name)}*\n"
        f"• Min Buy: *{min_buy_disp}*\n"
        f"• Status: *{'PAUSED ⏸️' if paused else 'RUNNING ✅'}*\n\n"
        "Choose a module:"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Min Buy", callback_data="TS_MIN"),
         InlineKeyboardButton("Emoji", callback_data="TS_EMO")],
        [InlineKeyboardButton("Manage Media", callback_data="TS_MEDIA"),
         InlineKeyboardButton("Social Links", callback_data="TS_SOC")],
        [InlineKeyboardButton("Layout", callback_data="TS_LAYOUT"),
         InlineKeyboardButton("Bot Preview", callback_data="TS_PREVIEW")],
        [InlineKeyboardButton("Pause / Resume", callback_data="TS_PAUSE"),
         InlineKeyboardButton("Remove Token", callback_data="TS_REMOVE")],
        [InlineKeyboardButton("⬅️ Back", callback_data="TS_BACK")],
    ])

    if edit:
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=kb, disable_web_page_preview=True)
    else:
        await msg.reply_text(text, parse_mode="Markdown", reply_markup=kb, disable_web_page_preview=True)

async def handle_token_settings_button(chat_id: int, data: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    msg = q.message if q else None
    g = get_group(chat_id)
    tok = g.get("token") if isinstance(g, dict) else None
    s = g.get("settings") or DEFAULT_SETTINGS

    if not msg:
        return

    # Back to group menu
    if data == "TS_BACK":
        await send_token_settings(chat_id, context, msg, edit=True)
        return

    # ----- Min Buy -----
    if data == "TS_MIN":
        unit = str(s.get("min_buy_unit") or "TON").upper()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Unit: TON {'✅' if unit!='USD' else ''}", callback_data="TS_MIN_UNIT_TON"),
             InlineKeyboardButton(f"Unit: USD {'✅' if unit=='USD' else ''}", callback_data="TS_MIN_UNIT_USD")],
            [InlineKeyboardButton("0", callback_data="TS_MIN_VAL_0"),
             InlineKeyboardButton("0.1", callback_data="TS_MIN_VAL_0.1"),
             InlineKeyboardButton("1", callback_data="TS_MIN_VAL_1"),
             InlineKeyboardButton("5", callback_data="TS_MIN_VAL_5")],
            [InlineKeyboardButton("10", callback_data="TS_MIN_VAL_10"),
             InlineKeyboardButton("25", callback_data="TS_MIN_VAL_25"),
             InlineKeyboardButton("50", callback_data="TS_MIN_VAL_50")],
            [InlineKeyboardButton("⬅️ Back", callback_data="TS_BACK")],
        ])
        note = "TON threshold uses *TON spent*. USD threshold uses *TON/USD* price (best-effort)."
        await msg.edit_text(f"*Min Buy*\nCurrent unit: *{unit}*\n\n{note}", parse_mode="Markdown", reply_markup=kb, disable_web_page_preview=True)
        return

    if data.startswith("TS_MIN_UNIT_"):
        unit = data.split("_")[-1]
        s["min_buy_unit"] = "USD" if unit == "USD" else "TON"
        save_groups()
        await send_token_settings(chat_id, context, msg, edit=True)
        return

    if data.startswith("TS_MIN_VAL_"):
        val = float(data.split("_", 3)[3])
        if str(s.get("min_buy_unit") or "TON").upper() == "USD":
            s["min_buy_usd"] = val
        else:
            s["min_buy_ton"] = val
        save_groups()
        await send_token_settings(chat_id, context, msg, edit=True)
        return

    # ----- Emoji / Strength -----
    if data == "TS_EMO":
        strength = bool(s.get("strength_on", True))
        emo = str(s.get("strength_emoji") or "🟢")
        step = float(s.get("strength_step_ton") or 5.0)
        mx = int(s.get("strength_max") or 30)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Strength: {'ON ✅' if strength else 'OFF ❌'}", callback_data="TS_EMO_TOG")],
            [InlineKeyboardButton("🟢", callback_data="TS_EMO_SET_GREEN"),
             InlineKeyboardButton("💎", callback_data="TS_EMO_SET_DIAMOND"),
             InlineKeyboardButton("✈️", callback_data="TS_EMO_SET_PLANE")],
            [InlineKeyboardButton("Step 1", callback_data="TS_EMO_STEP_1"),
             InlineKeyboardButton("5", callback_data="TS_EMO_STEP_5"),
             InlineKeyboardButton("10", callback_data="TS_EMO_STEP_10")],
            [InlineKeyboardButton("Max 15", callback_data="TS_EMO_MAX_15"),
             InlineKeyboardButton("30", callback_data="TS_EMO_MAX_30"),
             InlineKeyboardButton("45", callback_data="TS_EMO_MAX_45")],
            [InlineKeyboardButton("⬅️ Back", callback_data="TS_BACK")],
        ])
        await msg.edit_text(
            f"*Emoji / Buy Strength*\n• Emoji: *{emo}*\n• Step: *{step} TON*\n• Max: *{mx}*",
            parse_mode="Markdown",
            reply_markup=kb,
            disable_web_page_preview=True
        )
        return

    if data == "TS_EMO_TOG":
        s["strength_on"] = not bool(s.get("strength_on", True))
        save_groups()
        await send_token_settings(chat_id, context, msg, edit=True)
        return

    if data.startswith("TS_EMO_SET_"):
        k = data.split("_")[-1]
        s["strength_emoji"] = "🟢" if k == "GREEN" else ("💎" if k == "DIAMOND" else "✈️")
        save_groups()
        await send_token_settings(chat_id, context, msg, edit=True)
        return

    if data.startswith("TS_EMO_STEP_"):
        s["strength_step_ton"] = float(data.split("_")[-1])
        save_groups()
        await send_token_settings(chat_id, context, msg, edit=True)
        return

    if data.startswith("TS_EMO_MAX_"):
        s["strength_max"] = int(data.split("_")[-1])
        save_groups()
        await send_token_settings(chat_id, context, msg, edit=True)
        return

    # ----- Media -----
    if data == "TS_MEDIA":
        img_on = bool(s.get("buy_image_on", False))
        img_set = bool((s.get("buy_image_file_id") or "").strip())
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Image: {'ON ✅' if img_on else 'OFF ❌'}", callback_data="TS_MEDIA_TOG")],
            [InlineKeyboardButton("🖼 Set Buy Image", callback_data="IMG_SET"),
             InlineKeyboardButton("🗑 Clear Image", callback_data="IMG_CLEAR")],
            [InlineKeyboardButton("⬅️ Back", callback_data="TS_BACK")],
        ])
        await msg.edit_text(
            f"*Manage Media*\n• Image mode: *{'ON' if img_on else 'OFF'}*\n• Image: *{'set' if img_set else 'not set'}*",
            parse_mode="Markdown",
            reply_markup=kb,
            disable_web_page_preview=True
        )
        return

    if data == "TS_MEDIA_TOG":
        s["buy_image_on"] = not bool(s.get("buy_image_on", False))
        save_groups()
        await send_token_settings(chat_id, context, msg, edit=True)
        return

    # ----- Social Links -----
    if data == "TS_SOC":
        tg = ""
        if isinstance(tok, dict):
            tg = str(tok.get("telegram") or "").strip()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Set Telegram Link", callback_data="TS_SOC_SET_TG")],
            [InlineKeyboardButton("Clear Telegram", callback_data="TS_SOC_CLR_TG")],
            [InlineKeyboardButton("⬅️ Back", callback_data="TS_BACK")],
        ])
        await msg.edit_text(
            "*Social Links*\n"
            f"Telegram: {tg if tg else '—'}\n\n"
            "To set: tap *Set Telegram Link* then send the link in DM.",
            parse_mode="Markdown",
            reply_markup=kb,
            disable_web_page_preview=True
        )
        return

    if data == "TS_SOC_SET_TG":
        # Ask in DM for safety (Telegram blocks some group flows)
        AWAITING_SOCIAL[update.effective_user.id] = {"chat_id": chat_id, "field": "telegram"}
        await msg.reply_text("Send the token Telegram link now in DM (example: https://t.me/YourToken).")
        return

    if data == "TS_SOC_CLR_TG":
        if isinstance(tok, dict):
            tok["telegram"] = ""
            save_groups()
        await send_token_settings(chat_id, context, msg, edit=True)
        return

    # ----- Layout -----
    if data == "TS_LAYOUT":
        def tog_btn(key: str, label: str):
            on = bool(s.get(key, True))
            return InlineKeyboardButton(f"{label}: {'ON ✅' if on else 'OFF ❌'}", callback_data=f"TS_LAYOUT_TOG_{key}")
        kb = InlineKeyboardMarkup([
            [tog_btn("show_price", "Price"), tog_btn("show_liquidity", "Liquidity")],
            [tog_btn("show_mcap", "MCap"), tog_btn("show_holders", "Holders")],
            [InlineKeyboardButton("⬅️ Back", callback_data="TS_BACK")],
        ])
        await msg.edit_text("*Layout*\nToggle what to show in alerts:", parse_mode="Markdown", reply_markup=kb, disable_web_page_preview=True)
        return

    if data.startswith("TS_LAYOUT_TOG_"):
        key = data.split("_", 3)[3]
        s[key] = not bool(s.get(key, True))
        save_groups()
        await send_token_settings(chat_id, context, msg, edit=True)
        return

    # ----- Preview -----
    if data == "TS_PREVIEW":
        if not isinstance(tok, dict):
            await msg.reply_text("No token configured yet.")
            return
        dummy_tx = "0" * 64
        dummy_buyer = "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c"
        await msg.reply_text("📌 Sending preview alert to this group…")
        await post_buy(context.application, chat_id, tok, {"tx": dummy_tx, "buyer": dummy_buyer, "ton": 12.34, "token_amount": 123456.0}, source="Preview")
        return

    # ----- Pause / Resume -----
    if data == "TS_PAUSE":
        if not isinstance(tok, dict):
            await msg.reply_text("No token configured yet.")
            return
        tok["paused"] = not bool(tok.get("paused", False))
        tok["init_done"] = False  # baseline after resume
        save_groups()
        await send_token_settings(chat_id, context, msg, edit=True)
        return

    # ----- Remove -----
    if data == "TS_REMOVE":
        if not isinstance(tok, dict) or not tok:
            await msg.reply_text("No token configured.")
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Remove", callback_data="TS_REMOVE_CONFIRM")],
            [InlineKeyboardButton("❌ Cancel", callback_data="TS_BACK")],
        ])
        await msg.edit_text("Remove the current token for this group?", reply_markup=kb)
        return

    if data == "TS_REMOVE_CONFIRM":
        g["token"] = None
        save_groups()
        await msg.edit_text("✅ Token removed.")
        return

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


    # Social link input (Token Settings -> Social Links)
    if user.id in AWAITING_SOCIAL:
        cfg = AWAITING_SOCIAL.get(user.id) or {}
        target_chat_id = int(cfg.get("chat_id") or 0)
        field = str(cfg.get("field") or "telegram")
        if field == "telegram":
            m = re.search(r"https?://t\.me/[A-Za-z0-9_]{3,}(?:\S*)?", text)
            if not m:
                await update.message.reply_text("Send a valid Telegram link like: https://t.me/YourToken")
                return
            tg_url = m.group(0).strip()
            g = get_group(target_chat_id)
            tok = g.get("token") or {}
            if isinstance(tok, dict):
                tok["telegram"] = tg_url
                save_groups()
            AWAITING_SOCIAL.pop(user.id, None)
            await update.message.reply_text("✅ Token Telegram link saved.")
            return
        AWAITING_SOCIAL.pop(user.id, None)
        return

    # Resolve either a jetton address or a supported link (GT / DexScreener / STON / DeDust)
    addr = await _to_thread(resolve_jetton_from_text_sync, text)
    if not addr:
        return

    # Optional: token telegram link can be sent together with CA.
    # Example: EQ... https://t.me/YourToken
    tg_url = ""
    m_tg = re.search(r"https?://t\.me/[A-Za-z0-9_]{3,}(?:\S*)?", text)
    if m_tg:
        tg_url = m_tg.group(0).strip()

    # decide which chat to configure
    target_chat_id = None
    if chat.type == "private":
        cfg = AWAITING.get(user.id)
        if not cfg:
            await update.message.reply_text("Add the bot to your group, then tap *Configure Token* in that group.", parse_mode="Markdown")
            return
        if isinstance(cfg, dict):
            if cfg.get("stage") != "CA":
                await update.message.reply_text("Tap *Configure Token* again and choose a DEX first.", parse_mode="Markdown")
                return
            target_chat_id = int(cfg.get("group_id") or 0)
            dex_mode = str(cfg.get("dex") or "").strip() or "both"
        else:
            target_chat_id = int(cfg)
            dex_mode = "both"
        if not target_chat_id:
            await update.message.reply_text("Tap *Configure Token* again in your group.", parse_mode="Markdown")
            return
    else:
        # in group: only admins can configure
        if not await is_admin(context.bot, chat.id, user.id):
            return
        # If user pressed configure, it's this chat anyway
        target_chat_id = chat.id
        dex_mode = "both"

    await configure_group_token(target_chat_id, addr, context, reply_to_chat=chat.id, telegram=tg_url, dex_mode=dex_mode)
    # Clear awaiting state after successful input
    if chat.type == "private":
        AWAITING.pop(user.id, None)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Capture a buy image from an admin and store its Telegram file_id."""
    if not update.message or not update.effective_user or not update.effective_chat:
        return
    user = update.effective_user
    chat = update.effective_chat
    if user.id not in AWAITING_IMAGE:
        return

    target_chat_id = AWAITING_IMAGE.get(user.id)
    if not target_chat_id:
        return

    # In groups, ensure they are sending the photo inside the same group they are configuring.
    if chat.type in ("group", "supergroup") and chat.id != target_chat_id:
        return

    # In private, we trust the stored target_chat_id.
    if not await is_admin(context.bot, target_chat_id, user.id):
        AWAITING_IMAGE.pop(user.id, None)
        return

    photos = update.message.photo or []
    if not photos:
        return

    file_id = photos[-1].file_id  # largest
    g = get_group(target_chat_id)
    g["settings"]["buy_image_file_id"] = file_id
    g["settings"]["buy_image_on"] = True
    save_groups()
    AWAITING_IMAGE.pop(user.id, None)

    await update.message.reply_text("✅ Buy image saved. Image mode is now ON.")

async def configure_group_token(chat_id: int, jetton: str, context: ContextTypes.DEFAULT_TYPE, reply_to_chat: int, telegram: str = "", dex_mode: str = "both"):
    g = get_group(chat_id)
    # 1 token per group: confirm replace if exists and different
    existing = g.get("token") or None
    # Same token: allow updating telegram link without replacing anything.
    if existing and existing.get("address") == jetton and telegram:
        existing["telegram"] = telegram
        save_groups()
        await context.bot.send_message(chat_id=reply_to_chat, text="✅ Token Telegram link updated.")
        return
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
    await _set_token_now(chat_id, jetton, context, reply_to_chat, telegram=telegram, dex_mode=dex_mode)

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

async def _set_token_now(chat_id: int, jetton: str, context: ContextTypes.DEFAULT_TYPE, reply_chat_id: int, telegram: str = "", dex_mode: str = "both"):
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
    dex_mode = (dex_mode or "both").lower().strip()
    ston_pool = find_stonfi_ton_pair_for_token(jetton) if dex_mode in ("both","ston","stonfi") else None
    dedust_pool = find_dedust_ton_pair_for_token(jetton) if dex_mode in ("both","dedust") else None

    g = get_group(chat_id)
    # Auto-enable pools we actually found.
    # In auto/both mode we keep both enabled if pools exist (no manual DEX split required).
    try:
        s = g.get("settings") or {}
        if dex_mode in ("ston", "stonfi"):
            s["enable_ston"] = True
            s["enable_dedust"] = False
        elif dex_mode in ("dedust",):
            s["enable_ston"] = False
            s["enable_dedust"] = True
        else:
            s["enable_ston"] = bool(ston_pool)
            s["enable_dedust"] = bool(dedust_pool)
        g["settings"] = s
    except Exception:
        pass

    g["token"] = {
        "address": jetton,
        "dex_mode": ("auto" if dex_mode=="both" else dex_mode),
        "name": name,
        "symbol": sym,
        "ston_pool": ston_pool,
        "dedust_pool": dedust_pool,
        "set_at": int(time.time()),
        "init_done": False,
        "paused": False,
        "last_ston_tx": None,
        "last_dedust_trade": None,
        "ston_last_block": None,
        "ignore_before_ts": int(time.time()),
        "burst": {"window_start": int(time.time()), "count": 0},
        "telegram": telegram.strip() if telegram else "",
    }
    save_groups()

    # Prevent posting old buys right after configuration
    await warmup_seen_for_chat(chat_id, ston_pool, dedust_pool)
    # Mark init done so tracker loop doesn't skip another full cycle
    try:
        g2 = get_group(chat_id)
        if isinstance(g2.get('token'), dict):
            g2['token']['init_done'] = True
            save_groups()
    except Exception:
        pass

    disp = sym or name or "TOKEN"
    msg = (
        f"✅ *Token Added*\n"
        f"• Token: *{html.escape(disp)}*\n"
        f"• Address: `{jetton}`\n"
        f"• STON.fi pool: `{ston_pool or 'NONE'}`\n"
        f"• DeDust pool: `{dedust_pool or 'NONE'}`\n\n"
        f"Now posting buys automatically for this group.\n"
        f"Use *Settings* to set buy strength & image."
    )

    await context.bot.send_message(
        chat_id=reply_chat_id,
        text=msg,
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )
    if reply_chat_id != chat_id:
        await context.bot.send_message(
            chat_id=chat_id,
            text=msg,
            parse_mode="Markdown",
            disable_web_page_preview=True,
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

        # Pause / resume
        if bool(token.get("paused", False)):
            continue

        # One-time initialization per chat to prevent "old buys" spam.
        # If the bot restarts or a token was configured long ago, we warm up cursors/seen once
        # and skip posting on that first cycle.
        if not token.get("init_done"):
            try:
                await warmup_seen_for_chat(chat_id, token.get("ston_pool"), token.get("dedust_pool"))
            except Exception:
                pass
            token["init_done"] = True
            save_groups()
            continue

        min_buy = float(min_buy_ton_threshold(settings))
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
                latest = await _to_thread(ston_latest_block)
                if latest is None:
                    raise RuntimeError("no latest block")
                # per-token cursor to avoid posting old swaps when a new group configures a token
                last_block = token.get("ston_last_block")
                if last_block is None:
                    # initialize slightly behind to avoid missing
                    last_block = max(0, int(latest) - 5)
                from_b = int(last_block) + 1
                to_b = int(latest)
                # cap range to avoid huge pulls
                if to_b - from_b > 60:
                    from_b = to_b - 60
                evs = await _to_thread(ston_events, from_b, to_b)
                # advance cursor even if no events
                token["ston_last_block"] = to_b
                # filter swaps for this pool (STON export feed)
                ton_leg = ensure_ton_leg_for_pool(token)
                posted_any = False
                for ev in evs:
                    if (str(ev.get("eventType") or "").lower() != "swap"):
                        continue
                    ignore_before = int(token.get("ignore_before_ts") or 0)
                    ev_ts = int(ev.get("timestamp") or ev.get("time") or ev.get("ts") or 0)
                    if ignore_before and ev_ts and ev_ts < ignore_before:
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
                    dedupe_key = f"ston:{pool}:{tx}"
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
                            ignore_before = int(token.get("ignore_before_ts") or 0)
                            ut = int(txo.get("utime") or 0)
                            if ignore_before and ut and ut < ignore_before:
                                continue
                            buys = stonfi_extract_buys_from_tonapi_tx(txo, token["address"])
                            for b in buys:
                                ton_spent = float(b.get("ton") or 0.0)
                                # TonAPI sometimes returns nanoTON
                                if ton_spent > 1e5:
                                    ton_spent = ton_spent / 1e9

                                token_amt = float(b.get("token_amount") or 0.0)
                                dec = token.get("decimals")
                                try:
                                    dec_i = int(dec) if dec is not None else None
                                except Exception:
                                    dec_i = None
                                # TonAPI often returns jetton amount in minimal units
                                if dec_i is not None and token_amt > 1e8:
                                    token_amt = token_amt / (10 ** dec_i)

                                if ton_spent < min_buy:
                                    continue
                                txh = str(b.get("tx") or "").strip() or _tx_hash(txo)
                                buyer = str(b.get("buyer") or "").strip()
                                dedupe_key = f"ston:{pool}:{txh}"
                                if not dedupe_ok(chat_id, dedupe_key):
                                    continue
                                if settings.get("burst_mode", True) and burst["count"] >= max_msgs:
                                    continue
                                burst["count"] += 1
                                await post_buy(app, chat_id, token, {"tx": txh, "buyer": buyer, "ton": ton_spent, "token_amount": token_amt}, source="STON.fi v2")
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
                trades = await _to_thread(dedust_get_trades, pool, 40)
                if not isinstance(trades, list):
                    trades = []
                # Build sortable items with (lt, ts) so ordering is stable regardless of API order.
                items2 = []
                for tr in trades:
                    b = dedust_trade_to_buy(tr, token["address"])
                    if not b:
                        continue
                    # normalize timestamp (ms or sec)
                    ts_raw = (tr.get("timestamp") or tr.get("time") or tr.get("ts") or 0)
                    try:
                        ts_i = int(float(ts_raw or 0))
                        if ts_i > 10_000_000_000:
                            ts_i = ts_i // 1000
                    except Exception:
                        ts_i = 0
                    # lt/trade_id (prefer numeric)
                    lt_raw = (tr.get("lt") or b.get("trade_id") or tr.get("id") or "")
                    try:
                        lt_i = int(str(lt_raw).strip()) if str(lt_raw).strip() else 0
                    except Exception:
                        lt_i = 0
                    items2.append((lt_i, ts_i, b, tr))

                # sort oldest -> newest
                items2.sort(key=lambda x: (x[0] or 0, x[1] or 0))

                # baselines
                last_lt = 0
                last_ts = 0
                try:
                    last_lt = int(str(token.get("last_dedust_trade") or 0))
                except Exception:
                    last_lt = 0
                try:
                    last_ts = int(token.get("last_dedust_ts") or 0)
                except Exception:
                    last_ts = 0

                ignore_before = int(token.get("ignore_before_ts") or 0)

                max_seen_lt = last_lt
                max_seen_ts = last_ts

                for lt_i, ts_i, b, tr in items2:
                    # ignore old history right after token added
                    if ignore_before and ts_i and ts_i < ignore_before:
                        continue

                    is_new = False
                    if lt_i and last_lt:
                        is_new = lt_i > last_lt
                    elif lt_i and not last_lt:
                        # If we have lt but no baseline yet, treat as new only if after ignore_before
                        is_new = True
                    elif ts_i and last_ts:
                        is_new = ts_i > last_ts
                    elif ts_i and not last_ts:
                        is_new = True

                    if not is_new:
                        continue

                    ton_amt = float(b.get("ton") or 0.0)
                    if ton_amt < min_buy:
                        continue

                    # unified dedupe by normalized tx hash when possible
                    txh = _normalize_tx_hash_to_hex(b.get("tx") or "")
                    dedupe_key = f"tx:{txh}" if txh else f"dedust:{pool}:{b.get('tx')}"
                    if not dedupe_ok(chat_id, dedupe_key):
                        continue
                    if settings.get("burst_mode", True) and burst["count"] >= max_msgs:
                        continue
                    burst["count"] += 1

                    token_amt = float(b.get("token_amount") or 0.0)
                    await post_buy(app, chat_id, token, {
                        "tx": b.get("tx"),
                        "trade_id": str(lt_i or b.get("trade_id") or ""),
                        "buyer": b.get("buyer"),
                        "ton": ton_amt,
                        "token_amount": token_amt,
                    }, source="DeDust")

                    if lt_i and lt_i > max_seen_lt:
                        max_seen_lt = lt_i
                    if ts_i and ts_i > max_seen_ts:
                        max_seen_ts = ts_i

                # update baselines
                if max_seen_lt:
                    token["last_dedust_trade"] = str(max_seen_lt)
                if max_seen_ts:
                    token["last_dedust_ts"] = int(max_seen_ts)

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
    tok_amt = b.get("token_amount")
    tok_symbol = b.get("token_symbol") or sym or ""

    buyer_full = str(b.get("buyer") or "")
    buyer_short = _short_addr(buyer_full)
    buyer_url = f"https://tonviewer.com/address/{buyer_full}" if buyer_full else None
    tx = str(b.get("tx") or "")

    ston_pool = token.get("ston_pool") or ""
    dedust_pool = token.get("dedust_pool") or ""
    pool_for_market = ston_pool or dedust_pool

    # Market data (prefer GeckoTerminal)
    price_usd = liq_usd = mc_usd = None
    # Try cache first to avoid missing stats (rate limits / temporary failures)
    market_cache_key = str(pool_for_market or token_addr or "").strip()
    _mcached = MARKET_CACHE.get(market_cache_key) if market_cache_key else None
    _now = int(time.time())
    if _mcached and _now - int(_mcached.get("ts") or 0) < 900:
        price_usd = _mcached.get("price_usd")
        liq_usd = _mcached.get("liq_usd")
        mc_usd = _mcached.get("mc_usd")
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
    # Holders (keep last known value if APIs fail)
    prev_holders = None
    try:
        prev_holders = _cached.get("holders")
    except Exception:
        prev_holders = None

    holders = prev_holders
    jetton_addr = str(token.get("address") or "").strip()
    if jetton_addr:
        try:
            info = tonapi_jetton_info(jetton_addr)
            h = info.get("holders_count")
            if h is not None:
                holders = int(h)
        except Exception:
            pass
        if holders is None:
            try:
                h2 = tonapi_jetton_holders_count(jetton_addr)
                if h2 is not None:
                    holders = int(h2)
            except Exception:
                pass

    # Store/refresh cache so later messages don't lose stats
    if market_cache_key:
        MARKET_CACHE[market_cache_key] = {
            "ts": int(time.time()),
            "price_usd": price_usd,
            "liq_usd": liq_usd,
            "mc_usd": mc_usd,
            "holders": holders,
        }

    # Links row
    pair_for_links = pool_for_market or ""
    tx_hex = _normalize_tx_hash_to_hex(tx)
    # DeDust sometimes returns only LT (no hash). Resolve hash via TonAPI if possible.
    if not tx_hex and source == "DeDust":
        lt_guess = str(b.get("trade_id") or tx or "").strip()
        if lt_guess:
            resolved = tonapi_find_tx_hash_by_lt(str(dedust_pool or ""), lt_guess, limit=300)
            if not resolved:
                # quick retries for busy pools
                for _ in range(3):
                    try:
                        time.sleep(0.35)
                    except Exception:
                        pass
                    resolved = tonapi_find_tx_hash_by_lt(str(dedust_pool or ""), lt_guess, limit=600)
                    if resolved:
                        break
            tx_hex = _normalize_tx_hash_to_hex(resolved) or tx_hex
    tx_url = f"https://tonviewer.com/transaction/{tx_hex}" if tx_hex else (f"https://tonviewer.com/transaction/{quote(str(tx))}" if tx else None)
    gt_url = gecko_terminal_pool_url(pair_for_links) if pair_for_links else None
    dex_url = f"https://dexscreener.com/ton/{pair_for_links}" if pair_for_links else None
    # Token telegram button should reflect the token's own link.
    # If not set, hide the button (avoid wrong/static links).
    tg_link = (token.get("telegram") or "").strip()
    trending = TRENDING_URL

    # Pull settings for this chat (for strength + image)
    g = get_group(chat_id)
    s = g.get("settings") or DEFAULT_SETTINGS

    def fmt_usd(x: Optional[float], decimals: int = 0) -> Optional[str]:
        if x is None:
            return None
        try:
            if decimals <= 0:
                return f"${float(x):,.0f}"
            return f"${float(x):,.{decimals}f}"
        except Exception:
            return None

    # Crypton-style buy strength bar
    strength_block = ""
    if bool(s.get("strength_on", True)):
        try:
            step = float(s.get("strength_step_ton") or 5.0)
            max_n = int(s.get("strength_max") or 30)
            emo = str(s.get("strength_emoji") or "🟢")
            n = 1 if ton_amt > 0 else 0
            if step > 0:
                n = max(1, int(ton_amt // step))
            n = min(max_n, n)
            # wrap in lines of 15 emojis (like Crypton)
            per_line = 15
            rows = []
            for i in range(0, n, per_line):
                rows.append(emo * min(per_line, n - i))
            strength_block = "\n".join(rows)
        except Exception:
            strength_block = ""

    lines: List[str] = []
    # Header similar to Crypton
    lines.append(f"*{html.escape(title)} Buy!*")
    if strength_block:
        lines.append(strength_block)
    lines.append("")
    lines.append(f"Spent: *{ton_amt:,.2f} TON*")
    if tok_amt and tok_symbol:
        try:
            tok_amt_f = float(tok_amt)
            lines.append(f"Got: *{tok_amt_f:,.0f} {html.escape(str(tok_symbol))}*")
        except Exception:
            lines.append(f"Got: *{html.escape(str(tok_amt))} {html.escape(str(tok_symbol))}*")
    lines.append("")
    # Buyer wallet clickable + Txn label next to it (Crypton-style)
    if buyer_url:
        if tx_url:
            lines.append(f"[{buyer_short}]({buyer_url}) | [Txn]({tx_url})")
        else:
            # Keep the Txn label visible even if we couldn't resolve a tx hash yet
            lines.append(f"[{buyer_short}]({buyer_url}) | Txn")
    else:
        lines.append(f"{buyer_short}")

    # Stats (Crypton-style) - controlled by Layout toggles
    if bool(s.get("show_price", True)):
        lines.append(f"Price: {fmt_usd(price_usd, 6) if price_usd is not None else '—'}")
    if bool(s.get("show_liquidity", True)):
        lines.append(f"Liquidity: {fmt_usd(liq_usd, 0) if liq_usd is not None else '—'}")
    if bool(s.get("show_mcap", True)):
        lines.append(f"MCap: {fmt_usd(mc_usd, 0) if mc_usd is not None else '—'}")
    if bool(s.get("show_holders", True)):
        lines.append(f"Holders: {holders if holders is not None else '—'}")

    lines.append("")
    # Keep only TX | GT | DexS | Telegram | Trending
    link_parts: List[str] = []
    if tx_url:
        link_parts.append(f"[TX]({tx_url})")
    if gt_url:
        link_parts.append(f"[GT]({gt_url})")
    if dex_url:
        link_parts.append(f"[DexS]({dex_url})")
    if tg_link:
        link_parts.append(f"[Telegram]({tg_link})")
    if trending:
        link_parts.append(f"[Trending]({trending})")
    if link_parts:
        lines.append(" | ".join(link_parts))

    msg = "\n".join(lines)

    # Single buy button (dTrade referral + CA)
    ref = (DTRADE_REF or "https://t.me/dtrade?start=11TYq7LInG").rstrip("_")
    ca = token.get("address") or ""
    buy_url = f"{ref}_{ca}" if ca else ref
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Buy {sym or 'Token'} with dTrade", url=buy_url)]])

    # If buy image enabled and a Telegram file_id is set, send a photo with caption (not a link).
    buy_file_id = (s.get("buy_image_file_id") or "").strip()
    use_image = bool(s.get("buy_image_on", False)) and bool(buy_file_id)

    try:
        if use_image:
            await app.bot.send_photo(
                chat_id=chat_id,
                photo=buy_file_id,
                caption=msg,
                parse_mode="Markdown",
                reply_markup=kb,
            )
        else:
            await app.bot.send_message(
                chat_id=chat_id,
                text=msg,
                parse_mode="Markdown",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
    except Exception as e:
        # fallback without keyboard/markdown
        try:
            await app.bot.send_message(chat_id=chat_id, text=msg.replace("*", ""), disable_web_page_preview=True)
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
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
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


