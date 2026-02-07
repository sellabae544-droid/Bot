from __future__ import annotations
import re
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from .config import Config
from .db import (
    ensure_chat, add_token, get_active_token, list_tokens, set_active_token,
    set_emoji, set_media, set_min_ton, set_token_telegram, remove_token, get_token
)
from .gecko import GeckoClient, parse_token_meta
from .keyboards import menu_keyboard
from .leaderboard import render_leaderboard, ensure_leaderboard_message

router = Router()

class Flow(StatesGroup):
    waiting_token = State()
    waiting_select = State()
    waiting_emoji = State()
    waiting_min_ton = State()
    waiting_media = State()
    waiting_token_tg = State()

def _is_url(x: str) -> bool:
    return bool(re.match(r"^https?://", x.strip(), re.I))

async def is_allowed(message: Message, cfg: Config) -> bool:
    if message.chat.type not in ("group", "supergroup"):
        return False
    member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.status not in ("administrator", "creator"):
        return False
    if cfg.admin_ids and message.from_user.id not in cfg.admin_ids:
        return False
    return True

@router.message(CommandStart())
async def start(message: Message, state: FSMContext, cfg: Config):
    await state.clear()
    if not await is_allowed(message, cfg):
        await message.reply("Add me to a group and configure using a **real admin** (not Anonymous).")
        return
    await ensure_chat(message.chat.id)
    active = await get_active_token(message.chat.id)
    if active:
        token_line = f"Active: {active.token_symbol or active.token_name or 'Token'}"
    else:
        token_line = "No token yet. Add one."
    await message.reply(f"🕵️‍♂️ SpyTON BuyBot\n{token_line}\n\nChoose an option:", reply_markup=menu_keyboard())

@router.callback_query(F.data == "status")
async def status_cb(call: CallbackQuery, cfg: Config):
    await call.answer()
    t = await get_active_token(call.message.chat.id)
    if not t:
        await call.message.reply("No active token. Tap **Add new token**.", reply_markup=menu_keyboard())
        return
    await call.message.reply(
        f"🧾 Status\n"
        f"- Token: {t.token_symbol or '—'}\n"
        f"- Address: {t.token_address}\n"
        f"- Emoji: {t.emoji}\n"
        f"- Media: {'✅' if t.media_file_id else '—'}\n"
        f"- Min buy: {t.min_ton} TON\n"
        f"- Token TG: {t.token_telegram or '—'}",
        reply_markup=menu_keyboard(),
    )

@router.callback_query(F.data == "add_token")
async def add_token_cb(call: CallbackQuery, state: FSMContext, cfg: Config):
    await call.answer()
    await state.set_state(Flow.waiting_token)
    await call.message.reply("Paste the **TON token address** (Jetton master address).")

@router.message(Flow.waiting_token)
async def add_token_msg(message: Message, state: FSMContext, cfg: Config):
    if not await is_allowed(message, cfg):
        await state.clear()
        return
    token_address = (message.text or "").strip()
    if len(token_address) < 20:
        await message.reply("That address looks too short. Paste the full token address.")
        return

    # Fetch meta (optional). Even if it fails, we still accept token (no CMC dependency).
    symbol = name = None
    try:
        import aiohttp
        gecko = GeckoClient(cfg.gecko_base_url)
        async with aiohttp.ClientSession(headers={"accept":"application/json"}) as session:
            token_json = await gecko.get_token(session, token_address)
            symbol, name = parse_token_meta(token_json)
    except Exception:
        pass

    token_id = await add_token(message.chat.id, token_address, symbol, name)
    await state.clear()
    await message.reply(f"✅ Token added and selected. ID: {token_id}\nNow you can set emoji/media/min TON.", reply_markup=menu_keyboard())

@router.callback_query(F.data == "list_tokens")
async def list_tokens_cb(call: CallbackQuery, cfg: Config):
    await call.answer()
    toks = await list_tokens(call.message.chat.id)
    if not toks:
        await call.message.reply("No tokens yet. Tap **Add new token**.", reply_markup=menu_keyboard())
        return
    lines = ["🪙 Tokens in this group:"]
    for t in toks[:25]:
        name = t.token_symbol or t.token_name or "Token"
        lines.append(f"- ID {t.token_id}: {name} ({t.token_address[:8]}…)")
    await call.message.reply("\n".join(lines), reply_markup=menu_keyboard())

@router.callback_query(F.data == "select_token")
async def select_token_cb(call: CallbackQuery, state: FSMContext, cfg: Config):
    await call.answer()
    await state.set_state(Flow.waiting_select)
    await call.message.reply("Send the token ID to select (example: 3).")

@router.message(Flow.waiting_select)
async def select_token_msg(message: Message, state: FSMContext, cfg: Config):
    if not await is_allowed(message, cfg):
        await state.clear()
        return
    raw = (message.text or "").strip()
    try:
        token_id = int(raw)
    except Exception:
        await message.reply("Send a number token ID.")
        return
    t = await get_token(token_id)
    if not t or t.chat_id != message.chat.id:
        await message.reply("That token ID is not in this group.")
        return
    await set_active_token(message.chat.id, token_id)
    await state.clear()
    await message.reply(f"✅ Selected token ID {token_id}.", reply_markup=menu_keyboard())

@router.callback_query(F.data == "set_emoji")
async def set_emoji_cb(call: CallbackQuery, state: FSMContext, cfg: Config):
    await call.answer()
    if not await get_active_token(call.message.chat.id):
        await call.message.reply("Add/select a token first.", reply_markup=menu_keyboard())
        return
    await state.set_state(Flow.waiting_emoji)
    await call.message.reply("Send the emoji you want (example: 🟩 or 🚀).")

@router.message(Flow.waiting_emoji)
async def set_emoji_msg(message: Message, state: FSMContext, cfg: Config):
    if not await is_allowed(message, cfg):
        await state.clear()
        return
    t = await get_active_token(message.chat.id)
    if not t:
        await state.clear()
        return
    emoji = (message.text or "").strip()
    if not emoji:
        await message.reply("Send an emoji.")
        return
    await set_emoji(t.token_id, emoji[:8])
    await state.clear()
    await message.reply("✅ Emoji updated.", reply_markup=menu_keyboard())

@router.callback_query(F.data == "set_min_ton")
async def set_min_ton_cb(call: CallbackQuery, state: FSMContext, cfg: Config):
    await call.answer()
    if not await get_active_token(call.message.chat.id):
        await call.message.reply("Add/select a token first.", reply_markup=menu_keyboard())
        return
    await state.set_state(Flow.waiting_min_ton)
    await call.message.reply("Send minimum buy amount in TON (example: 5). Use 0 to disable.")

@router.message(Flow.waiting_min_ton)
async def set_min_ton_msg(message: Message, state: FSMContext, cfg: Config):
    if not await is_allowed(message, cfg):
        await state.clear()
        return
    t = await get_active_token(message.chat.id)
    if not t:
        await state.clear()
        return
    raw = (message.text or "").strip().replace(",", ".")
    try:
        v = float(raw)
        if v < 0:
            raise ValueError()
    except Exception:
        await message.reply("Send a number like 0 or 2.5")
        return
    await set_min_ton(t.token_id, v)
    await state.clear()
    await message.reply("✅ Min TON updated.", reply_markup=menu_keyboard())

@router.callback_query(F.data == "set_media")
async def set_media_cb(call: CallbackQuery, state: FSMContext, cfg: Config):
    await call.answer()
    if not await get_active_token(call.message.chat.id):
        await call.message.reply("Add/select a token first.", reply_markup=menu_keyboard())
        return
    await state.set_state(Flow.waiting_media)
    await call.message.reply("Send a **photo or gif** now. Send /skip to remove media.")

@router.message(Command("skip"), Flow.waiting_media)
async def media_skip(message: Message, state: FSMContext, cfg: Config):
    if not await is_allowed(message, cfg):
        await state.clear()
        return
    t = await get_active_token(message.chat.id)
    if not t:
        await state.clear()
        return
    await set_media(t.token_id, None)
    await state.clear()
    await message.reply("✅ Media removed.", reply_markup=menu_keyboard())

@router.message(Flow.waiting_media)
async def set_media_msg(message: Message, state: FSMContext, cfg: Config):
    if not await is_allowed(message, cfg):
        await state.clear()
        return
    t = await get_active_token(message.chat.id)
    if not t:
        await state.clear()
        return
    file_id = None
    if message.animation:
        file_id = message.animation.file_id
    elif message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and message.document.mime_type in ("image/gif",):
        file_id = message.document.file_id

    if not file_id:
        await message.reply("Please send a **photo** or **gif** (animation). Or /skip.")
        return

    await set_media(t.token_id, file_id)
    await state.clear()
    await message.reply("✅ Media saved.", reply_markup=menu_keyboard())

@router.callback_query(F.data == "set_token_tg")
async def set_token_tg_cb(call: CallbackQuery, state: FSMContext, cfg: Config):
    await call.answer()
    if not await get_active_token(call.message.chat.id):
        await call.message.reply("Add/select a token first.", reply_markup=menu_keyboard())
        return
    await state.set_state(Flow.waiting_token_tg)
    await call.message.reply("Send the token Telegram link (example: https://t.me/YourTokenGroup). Send /skip to clear.")

@router.message(Command("skip"), Flow.waiting_token_tg)
async def token_tg_skip(message: Message, state: FSMContext, cfg: Config):
    if not await is_allowed(message, cfg):
        await state.clear()
        return
    t = await get_active_token(message.chat.id)
    if not t:
        await state.clear()
        return
    await set_token_telegram(t.token_id, None)
    await state.clear()
    await message.reply("✅ Token Telegram link cleared.", reply_markup=menu_keyboard())

@router.message(Flow.waiting_token_tg)
async def token_tg_msg(message: Message, state: FSMContext, cfg: Config):
    if not await is_allowed(message, cfg):
        await state.clear()
        return
    t = await get_active_token(message.chat.id)
    if not t:
        await state.clear()
        return
    url = (message.text or "").strip()
    if not _is_url(url):
        await message.reply("Send a full link starting with http(s)://")
        return
    await set_token_telegram(t.token_id, url)
    await state.clear()
    await message.reply("✅ Token Telegram link saved.", reply_markup=menu_keyboard())

@router.callback_query(F.data == "show_lb")
async def show_lb_cb(call: CallbackQuery, cfg: Config):
    await call.answer()
    t = await get_active_token(call.message.chat.id)
    if not t:
        await call.message.reply("Add/select a token first.", reply_markup=menu_keyboard())
        return
    # Create or update leaderboard message now
    text = await render_leaderboard(t.token_id, cfg.leaderboard_top_n)
    await ensure_leaderboard_message(call.message, t, text)

@router.callback_query(F.data == "remove_token")
async def remove_token_cb(call: CallbackQuery, cfg: Config):
    await call.answer()
    t = await get_active_token(call.message.chat.id)
    if not t:
        await call.message.reply("No active token.", reply_markup=menu_keyboard())
        return
    await remove_token(t.token_id)
    await call.message.reply("🗑 Token removed. Add a new token anytime.", reply_markup=menu_keyboard())
