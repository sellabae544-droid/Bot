from aiogram.utils.keyboard import InlineKeyboardBuilder
from .config import Config
from .formatting import TradeMsg

TONVIEWER_TX = "https://tonviewer.com/transaction/"
DEXSCREENER_SEARCH = "https://dexscreener.com/ton?query="

def trade_keyboard(cfg: Config, trade: TradeMsg, token_address: str, pool_url: str | None):
    kb = InlineKeyboardBuilder()
    if trade.tx_hash:
        kb.button(text="Txn", url=f"{TONVIEWER_TX}{trade.tx_hash}")
    if pool_url:
        kb.button(text="GT", url=pool_url)
    kb.button(text="DexS", url=f"{DEXSCREENER_SEARCH}{token_address}")
    kb.button(text="Book Trend", url=cfg.book_trend_bot_url)
    kb.button(text="Trending", url=cfg.spyton_trending_url)
    kb.adjust(2, 2, 1)
    return kb.as_markup()

def menu_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Add new token", callback_data="add_token")
    kb.button(text="🧾 Status", callback_data="status")
    kb.button(text="🪙 My tokens", callback_data="list_tokens")
    kb.button(text="✅ Select token", callback_data="select_token")
    kb.button(text="🖼 Set media", callback_data="set_media")
    kb.button(text="😀 Set emoji", callback_data="set_emoji")
    kb.button(text="💎 Min buy (TON)", callback_data="set_min_ton")
    kb.button(text="🔗 Token Telegram", callback_data="set_token_tg")
    kb.button(text="📌 Show leaderboard", callback_data="show_lb")
    kb.button(text="🗑 Remove token", callback_data="remove_token")
    kb.adjust(2, 2, 2, 2, 2)
    return kb.as_markup()


async def setup_link_keyboard(bot, chat_id: int):
    me = await bot.get_me()
    url = f"https://t.me/{me.username}?start=cfg_{chat_id}"
    kb = InlineKeyboardBuilder()
    kb.button(text="Click Here!", url=url)
    kb.adjust(1)
    return kb.as_markup()
