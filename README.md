# SpyTON BuyBot (TON) — v2 (Own code)

This is a **from-scratch** TON buybot with a setup UX like DuckBuyBot:
- Add bot to group
- Give admin rights
- `/start`
- Add token (paste Jetton master address)
- Optional: set media / emoji / min TON / token telegram link
- Done ✅

## Key points
- ✅ **Supports any TON token by address** (no CoinMarketCap dependency).
- ✅ Tracks buys using **GeckoTerminal TON** pools/trades.
- ✅ Has an **auto-updating Leaderboard** (single message edited, not spammy).
- ✅ Buttons: Txn, GT, DexS, Book Trend, Trending.

## Deploy on Railway
1) Push code to GitHub
2) Railway → Deploy from GitHub
3) Set Variables:
   - `BOT_TOKEN`
   - `DTRADE_REF_BASE`
   - optional: `ADMIN_IDS`
4) Deploy

## Use
In your group:
- Add bot as admin
- `/start`
- Add new token → paste address

### Leaderboard
The bot will create (or reuse) one leaderboard message per token and keep editing it.

## Notes
- You must configure from a real admin (not Anonymous admin).
- Storage: SQLite at `data/spyton.db`


### Fix note
This version uses HTML parse mode to avoid Telegram Markdown errors.


### v8 change
This version matches Crypton flow: /start in group -> Click Here button -> configure in private.
