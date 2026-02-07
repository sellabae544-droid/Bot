from __future__ import annotations
import time
from aiogram.types import Message
from .db import top_stats, set_leaderboard_message_id, TokenCfg

def _short(addr: str) -> str:
    if not addr or len(addr) < 10:
        return addr
    return f"{addr[:4]}…{addr[-4:]}"

async def render_leaderboard(token_id: int, top_n: int) -> str:
    rows = await top_stats(token_id, top_n)
    lines = ["🏆 *SpyTON Leaderboard* (Top buyers)"]
    if not rows:
        lines.append("\nNo buys yet.")
        return "\n".join(lines)

    lines.append("")
    for i, (buyer, ton_total, buy_count) in enumerate(rows, start=1):
        lines.append(f"{i}. `{_short(buyer)}` — *{ton_total:,.2f} TON* ({buy_count} buys)")
    return "\n".join(lines)

async def ensure_leaderboard_message(msg: Message, token: TokenCfg, text: str):
    # If we already have a message id, try edit it. Else create a new message and store id.
    if token.leaderboard_message_id:
        try:
            await msg.bot.edit_message_text(
                chat_id=token.chat_id,
                message_id=token.leaderboard_message_id,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            return
        except Exception:
            # message deleted or can't edit
            await set_leaderboard_message_id(token.token_id, None)

    sent = await msg.bot.send_message(
        token.chat_id,
        text,
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )
    await set_leaderboard_message_id(token.token_id, sent.message_id)
