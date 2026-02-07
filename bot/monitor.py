import asyncio
import aiohttp
import time
from aiogram import Bot

from .config import Config
from .db import list_all_tokens, set_last_trade_id, add_stat, get_token
from .gecko import GeckoClient
from .formatting import TradeMsg, short_addr, fmt_ton, fmt_token_amt, fmt_usd
from .keyboards import trade_keyboard
from .leaderboard import render_leaderboard

def format_buy_message(token, trade: TradeMsg) -> str:
    token_name = token.token_name or token.token_symbol or "Token"
    sym = token.token_symbol or ""
    header = f"{token.emoji} | {token_name}".strip()
    lines = [
        header,
        "BUY — TON DEX",
        "",
        f"💎 {fmt_ton(trade.ton_amount)}",
        f"🪙 {fmt_token_amt(trade.token_amount)} {sym}".strip(),
        f"👤 {short_addr(trade.buyer)} | Txn",
        f"🏦 MC: {fmt_usd(trade.market_cap_usd)}  |  💧 LP: {fmt_usd(trade.liquidity_usd)}",
    ]
    return "\n".join(lines)

async def pick_best_pool(gecko: GeckoClient, session: aiohttp.ClientSession, token_address: str):
    pools = await gecko.get_token_pools(session, token_address, page=1)
    data = pools.get("data") or []
    best_liq = -1.0
    best_pool_addr = None
    best_pool_url = None
    mc = None
    liq = None

    for p in data:
        attr = p.get("attributes") or {}
        liq_usd = attr.get("reserve_in_usd") or attr.get("liquidity_usd")
        try:
            liq_f = float(liq_usd) if liq_usd is not None else 0.0
        except Exception:
            liq_f = 0.0

        if liq_f > best_liq:
            best_liq = liq_f
            best_pool_addr = p.get("id")
            best_pool_url = attr.get("gt_url") or attr.get("url")
            mc = attr.get("market_cap_usd")
            liq = liq_usd

    if best_pool_addr and best_pool_addr.startswith("ton_"):
        best_pool_addr = best_pool_addr.replace("ton_", "", 1)

    try:
        mc = float(mc) if mc is not None else None
    except Exception:
        mc = None
    try:
        liq = float(liq) if liq is not None else None
    except Exception:
        liq = None

    return best_pool_addr, best_pool_url, mc, liq

async def fetch_new_trades(gecko: GeckoClient, session: aiohttp.ClientSession, pool_addr: str, last_trade_id: str|None):
    trades = await gecko.get_pool_trades(session, pool_addr, page=1)
    data = trades.get("data") or []
    new = []
    for t in data:  # newest-first
        tid = t.get("id")
        if not tid:
            continue
        if last_trade_id and tid == last_trade_id:
            break
        new.append(t)
    return list(reversed(new))

def parse_buy(t: dict, pool_url: str|None, mc: float|None, liq: float|None) -> TradeMsg|None:
    attr = t.get("attributes") or {}
    # Gecko returns trade_type relative to base token; we only want buys.
    if attr.get("trade_type") != "buy":
        return None

    try:
        ton_amount = float(attr.get("quote_amount") or 0)
    except Exception:
        ton_amount = 0.0
    try:
        token_amount = float(attr.get("base_amount") or 0)
    except Exception:
        token_amount = 0.0

    buyer = (attr.get("tx_from_address") or attr.get("from_address") or "—")
    tx_hash = attr.get("tx_hash") or attr.get("transaction_hash")
    ts = int(attr.get("block_timestamp") or attr.get("timestamp") or time.time())

    return TradeMsg(
        ton_amount=ton_amount,
        token_amount=token_amount,
        buyer=buyer,
        tx_hash=tx_hash,
        pool_url=pool_url,
        market_cap_usd=mc,
        liquidity_usd=liq,
        ts=ts,
    )

async def monitor_loop(bot: Bot, cfg: Config):
    gecko = GeckoClient(cfg.gecko_base_url)
    async with aiohttp.ClientSession(headers={"accept": "application/json"}) as session:
        while True:
            try:
                tokens = await list_all_tokens()
                for token in tokens:
                    if not token.token_address:
                        continue

                    pool_addr, pool_url, mc, liq = await pick_best_pool(gecko, session, token.token_address)
                    if not pool_addr:
                        continue

                    new_trades = await fetch_new_trades(gecko, session, pool_addr, token.last_trade_id)
                    last_seen = token.last_trade_id

                    for tr in new_trades:
                        tid = tr.get("id")
                        buy = parse_buy(tr, pool_url, mc, liq)
                        if buy is None:
                            last_seen = tid
                            continue
                        if buy.ton_amount < token.min_ton:
                            last_seen = tid
                            continue

                        # Send buy alert
                        text = format_buy_message(token, buy)
                        kb = trade_keyboard(cfg, buy, token.token_address, pool_url)

                        if token.media_file_id:
                            try:
                                await bot.send_animation(token.chat_id, token.media_file_id, caption=text, reply_markup=kb)
                            except Exception:
                                await bot.send_photo(token.chat_id, token.media_file_id, caption=text, reply_markup=kb)
                        else:
                            await bot.send_message(token.chat_id, text, reply_markup=kb)

                        # Update stats + leaderboard
                        await add_stat(token.token_id, buy.buyer, buy.ton_amount, buy.ts)
                        try:
                            fresh_token = await get_token(token.token_id)
                            if fresh_token:
                                lb_text = await render_leaderboard(token.token_id, cfg.leaderboard_top_n)
                                # edit existing leaderboard if exists
                                if fresh_token.leaderboard_message_id:
                                    try:
                                        await bot.edit_message_text(
                                            chat_id=fresh_token.chat_id,
                                            message_id=fresh_token.leaderboard_message_id,
                                            text=lb_text,
                                            parse_mode="Markdown",
                                            disable_web_page_preview=True,
                                        )
                                    except Exception:
                                        pass
                        except Exception:
                            pass

                        last_seen = tid

                    if last_seen and last_seen != token.last_trade_id:
                        await set_last_trade_id(token.token_id, last_seen)

                await asyncio.sleep(cfg.poll_seconds)
            except Exception:
                await asyncio.sleep(max(10, cfg.poll_seconds))
