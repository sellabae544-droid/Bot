import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: set[int]
    gecko_base_url: str
    spyton_trending_url: str
    spyton_listing_url: str
    book_trend_bot_url: str
    dtrade_ref_base: str
    poll_seconds: int
    leaderboard_top_n: int

def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is required")

    return Config(
        bot_token=bot_token,
        admin_ids=set(),
        gecko_base_url=os.getenv("GECKO_BASE_URL", "https://api.geckoterminal.com/api/v2").strip(),
        spyton_trending_url=os.getenv("SPYTON_TRENDING_URL", "https://t.me/SpyTonTrending").strip(),
        spyton_listing_url=os.getenv("SPYTON_LISTING_URL", "https://t.me/TonProjectListing").strip(),
        book_trend_bot_url=os.getenv("BOOK_TREND_BOT_URL", "https://t.me/SpyTONTrndBot").strip(),
        dtrade_ref_base=os.getenv("DTRADE_REF_BASE", "https://t.me/dtrade?start=11TYq7LInG").strip(),
        poll_seconds=int(os.getenv("POLL_SECONDS", "25").strip()),
        leaderboard_top_n=int(os.getenv("LEADERBOARD_TOP_N", "10").strip()),
    )
