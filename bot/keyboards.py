from aiogram.utils.keyboard import InlineKeyboardBuilder

def menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Add Token", callback_data="add_token")
    kb.button(text="😀 Set Emoji", callback_data="set_emoji")
    kb.button(text="💎 Set Min TON", callback_data="set_min")
    kb.button(text="🖼 Set Media", callback_data="set_media")
    kb.button(text="ℹ️ Status", callback_data="status")
    kb.adjust(2, 2, 1)
    return kb.as_markup()

async def click_here_kb(bot, chat_id: int):
    me = await bot.get_me()
    url = f"https://t.me/{me.username}?start=cfg_{chat_id}"
    kb = InlineKeyboardBuilder()
    kb.button(text="Click Here!", url=url)
    kb.adjust(1)
    return kb.as_markup()
