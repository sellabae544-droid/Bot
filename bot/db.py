from __future__ import annotations
import os
import aiosqlite
from dataclasses import dataclass
from typing import Optional

@dataclass
class TokenRow:
    token_id: int
    chat_id: int
    token_address: str
    token_symbol: str | None
    token_name: str | None
    emoji: str
    media_file_id: str | None
    min_ton: float
    token_telegram: str | None
    is_active: int

class DB:
    def __init__(self, path: str):
        self.path = path

    async def init(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """CREATE TABLE IF NOT EXISTS user_sessions (
                    user_id INTEGER PRIMARY KEY,
                    target_chat_id INTEGER NOT NULL
                );"""
            )
            await db.execute(
                """CREATE TABLE IF NOT EXISTS tokens (
                    token_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    token_address TEXT NOT NULL,
                    token_symbol TEXT,
                    token_name TEXT,
                    emoji TEXT DEFAULT '🟩',
                    media_file_id TEXT,
                    min_ton REAL DEFAULT 0,
                    token_telegram TEXT,
                    is_active INTEGER DEFAULT 1
                );"""
            )
            await db.commit()

    async def set_user_target(self, user_id: int, chat_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO user_sessions (user_id, target_chat_id)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET target_chat_id=excluded.target_chat_id""",
                (user_id, chat_id),
            )
            await db.commit()

    async def get_user_target(self, user_id: int) -> Optional[int]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT target_chat_id FROM user_sessions WHERE user_id=?",
                (user_id,),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else None

    async def add_token(self, chat_id: int, token_address: str) -> int:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE tokens SET is_active=0 WHERE chat_id=?", (chat_id,))
            cur = await db.execute(
                """INSERT INTO tokens (chat_id, token_address, is_active)
                VALUES (?, ?, 1)""",
                (chat_id, token_address),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def get_active_token(self, chat_id: int) -> Optional[TokenRow]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """SELECT token_id, chat_id, token_address, token_symbol, token_name, emoji,
                          media_file_id, min_ton, token_telegram, is_active
                   FROM tokens
                   WHERE chat_id=? AND is_active=1
                   ORDER BY token_id DESC
                   LIMIT 1""",
                (chat_id,),
            )
            row = await cur.fetchone()
            if not row:
                return None
            return TokenRow(*row)

    async def set_emoji(self, token_id: int, emoji: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE tokens SET emoji=? WHERE token_id=?", (emoji, token_id))
            await db.commit()

    async def set_min_ton(self, token_id: int, v: float) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE tokens SET min_ton=? WHERE token_id=?", (v, token_id))
            await db.commit()

    async def set_media(self, token_id: int, file_id: str | None) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE tokens SET media_file_id=? WHERE token_id=?", (file_id, token_id))
            await db.commit()
