import aiosqlite
from dataclasses import dataclass
from typing import Optional, List, Tuple

DB_PATH = "data/spyton.db"

DEFAULT_EMOJI = "🟩"

@dataclass
class TokenCfg:
    token_id: int
    chat_id: int
    token_address: str
    token_symbol: Optional[str]
    token_name: Optional[str]
    token_telegram: Optional[str]
    emoji: str
    media_file_id: Optional[str]
    min_ton: float
    last_trade_id: Optional[str]
    leaderboard_message_id: Optional[int]

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute(
            """CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                active_token_id INTEGER
            );"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS user_sessions (
    user_id INTEGER PRIMARY KEY,
    target_chat_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
                token_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                token_address TEXT NOT NULL,
                token_symbol TEXT,
                token_name TEXT,
                token_telegram TEXT,
                emoji TEXT NOT NULL DEFAULT '🟩',
                media_file_id TEXT,
                min_ton REAL NOT NULL DEFAULT 0,
                last_trade_id TEXT,
                leaderboard_message_id INTEGER,
                UNIQUE(chat_id, token_address)
            );"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS stats (
                token_id INTEGER NOT NULL,
                buyer TEXT NOT NULL,
                ton_total REAL NOT NULL DEFAULT 0,
                buy_count INTEGER NOT NULL DEFAULT 0,
                last_ts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (token_id, buyer)
            );"""
        )
        await db.commit()

async def ensure_chat(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO chats(chat_id) VALUES (?)", (chat_id,))
        await db.commit()

async def set_active_token(chat_id: int, token_id: int | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO chats(chat_id) VALUES (?)", (chat_id,))
        await db.execute("UPDATE chats SET active_token_id=? WHERE chat_id=?", (token_id, chat_id))
        await db.commit()

async def get_active_token_id(chat_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await db.execute_fetchone("SELECT active_token_id FROM chats WHERE chat_id=?", (chat_id,))
        if not row:
            return None
        return row[0]

async def add_token(chat_id: int, token_address: str, symbol: str | None, name: str | None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO chats(chat_id) VALUES (?)", (chat_id,))
        cur = await db.execute(
            "INSERT OR IGNORE INTO tokens(chat_id, token_address, token_symbol, token_name) VALUES (?,?,?,?)",
            (chat_id, token_address, symbol, name),
        )
        await db.commit()
        # fetch id
        row = await db.execute_fetchone(
            "SELECT token_id FROM tokens WHERE chat_id=? AND token_address=?",
            (chat_id, token_address),
        )
        token_id = int(row[0])
        # set as active
        await db.execute("UPDATE chats SET active_token_id=? WHERE chat_id=?", (token_id, chat_id))
        await db.commit()
        return token_id

async def get_token(token_id: int) -> TokenCfg | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await db.execute_fetchone("SELECT * FROM tokens WHERE token_id=?", (token_id,))
        if not row:
            return None
        return TokenCfg(
            token_id=row["token_id"],
            chat_id=row["chat_id"],
            token_address=row["token_address"],
            token_symbol=row["token_symbol"],
            token_name=row["token_name"],
            token_telegram=row["token_telegram"],
            emoji=row["emoji"] or DEFAULT_EMOJI,
            media_file_id=row["media_file_id"],
            min_ton=float(row["min_ton"] or 0),
            last_trade_id=row["last_trade_id"],
            leaderboard_message_id=row["leaderboard_message_id"],
        )

async def get_active_token(chat_id: int) -> TokenCfg | None:
    token_id = await get_active_token_id(chat_id)
    if not token_id:
        return None
    return await get_token(int(token_id))

async def list_tokens(chat_id: int) -> List[TokenCfg]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall("SELECT * FROM tokens WHERE chat_id=? ORDER BY token_id DESC", (chat_id,))
        out: List[TokenCfg] = []
        for row in rows:
            out.append(TokenCfg(
                token_id=row["token_id"],
                chat_id=row["chat_id"],
                token_address=row["token_address"],
                token_symbol=row["token_symbol"],
                token_name=row["token_name"],
                token_telegram=row["token_telegram"],
                emoji=row["emoji"] or DEFAULT_EMOJI,
                media_file_id=row["media_file_id"],
                min_ton=float(row["min_ton"] or 0),
                last_trade_id=row["last_trade_id"],
                leaderboard_message_id=row["leaderboard_message_id"],
            ))
        return out

async def update_token_meta(token_id: int, symbol: str|None, name: str|None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tokens SET token_symbol=?, token_name=? WHERE token_id=?", (symbol, name, token_id))
        await db.commit()

async def set_token_telegram(token_id: int, url: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tokens SET token_telegram=? WHERE token_id=?", (url, token_id))
        await db.commit()

async def set_emoji(token_id: int, emoji: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tokens SET emoji=? WHERE token_id=?", (emoji, token_id))
        await db.commit()

async def set_media(token_id: int, media_file_id: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tokens SET media_file_id=? WHERE token_id=?", (media_file_id, token_id))
        await db.commit()

async def set_min_ton(token_id: int, min_ton: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tokens SET min_ton=? WHERE token_id=?", (min_ton, token_id))
        await db.commit()

async def set_last_trade_id(token_id: int, last_trade_id: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tokens SET last_trade_id=? WHERE token_id=?", (last_trade_id, token_id))
        await db.commit()

async def set_leaderboard_message_id(token_id: int, message_id: int | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tokens SET leaderboard_message_id=? WHERE token_id=?", (message_id, token_id))
        await db.commit()

async def remove_token(token_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        # clear active if needed
        row = await db.execute_fetchone("SELECT chat_id FROM tokens WHERE token_id=?", (token_id,))
        if row:
            chat_id = int(row[0])
            active = await db.execute_fetchone("SELECT active_token_id FROM chats WHERE chat_id=?", (chat_id,))
            if active and active[0] == token_id:
                await db.execute("UPDATE chats SET active_token_id=NULL WHERE chat_id=?", (chat_id,))
        await db.execute("DELETE FROM stats WHERE token_id=?", (token_id,))
        await db.execute("DELETE FROM tokens WHERE token_id=?", (token_id,))
        await db.commit()

async def list_all_tokens() -> List[TokenCfg]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall("SELECT * FROM tokens")
        out: List[TokenCfg] = []
        for row in rows:
            out.append(TokenCfg(
                token_id=row["token_id"],
                chat_id=row["chat_id"],
                token_address=row["token_address"],
                token_symbol=row["token_symbol"],
                token_name=row["token_name"],
                token_telegram=row["token_telegram"],
                emoji=row["emoji"] or DEFAULT_EMOJI,
                media_file_id=row["media_file_id"],
                min_ton=float(row["min_ton"] or 0),
                last_trade_id=row["last_trade_id"],
                leaderboard_message_id=row["leaderboard_message_id"],
            ))
        return out

async def add_stat(token_id: int, buyer: str, ton_amount: float, ts: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO stats(token_id, buyer, ton_total, buy_count, last_ts)
               VALUES (?,?,?,?,?)
               ON CONFLICT(token_id, buyer) DO UPDATE SET
                 ton_total = ton_total + excluded.ton_total,
                 buy_count = buy_count + 1,
                 last_ts = excluded.last_ts
            """,
            (token_id, buyer, float(ton_amount), 1, int(ts)),
        )
        await db.commit()

async def top_stats(token_id: int, top_n: int) -> List[Tuple[str, float, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await db.execute_fetchall(
            "SELECT buyer, ton_total, buy_count FROM stats WHERE token_id=? ORDER BY ton_total DESC LIMIT ?",
            (token_id, top_n),
        )
        return [(r[0], float(r[1]), int(r[2])) for r in rows]


async def set_user_target(user_id: int, target_chat_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO user_sessions (user_id, target_chat_id) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET target_chat_id=excluded.target_chat_id",
            (user_id, target_chat_id),
        )
        await db.commit()

async def get_user_target(user_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT target_chat_id FROM user_sessions WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return int(row[0]) if row else None
