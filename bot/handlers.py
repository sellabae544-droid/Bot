from __future__ import annotations
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

from .db import DB
from .keyboards import menu_kb, click_here_kb

router = Router()

class Flow(StatesGroup):
    wait_token = State()
    wait_emoji = State()
    wait_min = State()
    wait_media = State()

def _anon(message: Message) -> bool:
    return message.from_user is None

async def _get_target_chat(state: FSMContext, db: DB, message: Message) -> int | None:
    data = await state.get_data()
    tc = data.get("target_chat_id")
    if isinstance(tc, int):
        return tc
    if message.from_user:
        return await db.get_user_target(message.from_user.id)
    return None

@router.message(CommandStart())
async def start(message: Message, state: FSMContext, db: DB):
    await state.clear()

    if message.chat.type in ("group", "supergroup", "channel"):
        if _anon(message):
            await message.reply("Turn off Anonymous Admin, then send /start again.")
            return
        await db.set_user_target(message.from_user.id, message.chat.id)
        kb = await click_here_kb(message.bot, message.chat.id)
        await message.reply(
            "✅ Chat added successfully.\nTo continue setup, click “Click Here!”",
            reply_markup=kb,
        )
        return

    # Private chat
    args = ""
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) == 2:
            args = parts[1].strip()

    target = None
    if args.startswith("cfg_"):
        try:
            target = int(args.replace("cfg_", "", 1))
        except Exception:
            target = None

    if target is None and message.from_user:
        target = await db.get_user_target(message.from_user.id)

    if target is None:
        await message.reply("Go to your group/channel, type /start, then tap Click Here!.")
        return

    await state.update_data(target_chat_id=target)
    t = await db.get_active_token(target)
    if t:
        await message.reply(
            f"🕵️‍♂️ SpyTON BuyBot\nActive token: {t.token_address[:10]}…\n\nChoose an option:",
            reply_markup=menu_kb(),
        )
    else:
        await message.reply(
            "🕵️‍♂️ SpyTON BuyBot\nNo token yet. Add one now:",
            reply_markup=menu_kb(),
        )

@router.callback_query(F.data == "status")
async def status(call: CallbackQuery, state: FSMContext, db: DB):
    await call.answer()
    target = await _get_target_chat(state, db, call.message)
    if target is None:
        await call.message.reply("Open your group and press /start, then Click Here!.")
        return
    t = await db.get_active_token(target)
    if not t:
        await call.message.reply("No active token. Tap ➕ Add Token.", reply_markup=menu_kb())
        return
    await call.message.reply(
        f"ℹ️ Status\n"
        f"- Address: {t.token_address}\n"
        f"- Emoji: {t.emoji}\n"
        f"- Min TON: {t.min_ton}\n"
        f"- Media: {'✅' if t.media_file_id else '—'}",
        reply_markup=menu_kb(),
    )

@router.callback_query(F.data == "add_token")
async def add_token(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(Flow.wait_token)
    await call.message.reply("Paste the TON token address (Jetton master).")

@router.message(Flow.wait_token)
async def add_token_msg(message: Message, state: FSMContext, db: DB):
    target = await _get_target_chat(state, db, message)
    if target is None:
        await message.reply("Open your group and press /start, then Click Here!.")
        await state.clear()
        return
    addr = (message.text or "").strip()
    if len(addr) < 20:
        await message.reply("Address too short. Paste full token address.")
        return
    tid = await db.add_token(target, addr)
    await state.clear()
    await state.update_data(target_chat_id=target)
    await message.reply(f"✅ Token saved (ID {tid}). Now set emoji/media/min TON if you want.", reply_markup=menu_kb())

@router.callback_query(F.data == "set_emoji")
async def set_emoji(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(Flow.wait_emoji)
    await call.message.reply("Send the emoji you want (example: 🟩 or 🚀).")

@router.message(Flow.wait_emoji)
async def set_emoji_msg(message: Message, state: FSMContext, db: DB):
    target = await _get_target_chat(state, db, message)
    if target is None:
        await message.reply("Open your group and press /start, then Click Here!.")
        await state.clear()
        return
    t = await db.get_active_token(target)
    if not t:
        await message.reply("Add token first.")
        await state.clear()
        return
    emo = (message.text or "").strip()
    if not emo:
        await message.reply("Send an emoji.")
        return
    await db.set_emoji(t.token_id, emo[:8])
    await state.clear()
    await state.update_data(target_chat_id=target)
    await message.reply("✅ Emoji updated.", reply_markup=menu_kb())

@router.callback_query(F.data == "set_min")
async def set_min(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(Flow.wait_min)
    await call.message.reply("Send minimum buy amount in TON (example: 5). Use 0 to disable.")

@router.message(Flow.wait_min)
async def set_min_msg(message: Message, state: FSMContext, db: DB):
    target = await _get_target_chat(state, db, message)
    if target is None:
        await message.reply("Open your group and press /start, then Click Here!.")
        await state.clear()
        return
    t = await db.get_active_token(target)
    if not t:
        await message.reply("Add token first.")
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
    await db.set_min_ton(t.token_id, v)
    await state.clear()
    await state.update_data(target_chat_id=target)
    await message.reply("✅ Min TON updated.", reply_markup=menu_kb())

@router.callback_query(F.data == "set_media")
async def set_media(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(Flow.wait_media)
    await call.message.reply("Send a photo or gif now. Send /skip to remove media.")

@router.message(Command("skip"), Flow.wait_media)
async def skip_media(message: Message, state: FSMContext, db: DB):
    target = await _get_target_chat(state, db, message)
    if target is None:
        await message.reply("Open your group and press /start, then Click Here!.")
        await state.clear()
        return
    t = await db.get_active_token(target)
    if not t:
        await message.reply("Add token first.")
        await state.clear()
        return
    await db.set_media(t.token_id, None)
    await state.clear()
    await state.update_data(target_chat_id=target)
    await message.reply("✅ Media removed.", reply_markup=menu_kb())

@router.message(Flow.wait_media)
async def set_media_msg(message: Message, state: FSMContext, db: DB):
    target = await _get_target_chat(state, db, message)
    if target is None:
        await message.reply("Open your group and press /start, then Click Here!.")
        await state.clear()
        return
    t = await db.get_active_token(target)
    if not t:
        await message.reply("Add token first.")
        await state.clear()
        return

    file_id = None
    if message.animation:
        file_id = message.animation.file_id
    elif message.photo:
        file_id = message.photo[-1].file_id

    if not file_id:
        await message.reply("Please send a photo or gif (animation). Or /skip.")
        return

    await db.set_media(t.token_id, file_id)
    await state.clear()
    await state.update_data(target_chat_id=target)
    await message.reply("✅ Media saved.", reply_markup=menu_kb())
