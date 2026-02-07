import aiohttp
from typing import Any, Optional

class GeckoClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def _get(self, session: aiohttp.ClientSession, path: str, params: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        async with session.get(url, params=params, timeout=20) as r:
            r.raise_for_status()
            return await r.json()

    async def get_token(self, session: aiohttp.ClientSession, token_address: str) -> dict:
        return await self._get(session, f"/networks/ton/tokens/{token_address}")

    async def get_token_pools(self, session: aiohttp.ClientSession, token_address: str, page: int = 1) -> dict:
        return await self._get(session, f"/networks/ton/tokens/{token_address}/pools", params={"page": page})

    async def get_pool_trades(self, session: aiohttp.ClientSession, pool_address: str, page: int = 1) -> dict:
        return await self._get(session, f"/networks/ton/pools/{pool_address}/trades", params={"page": page})

def parse_token_meta(token_json: dict) -> tuple[Optional[str], Optional[str]]:
    attr = token_json.get("data", {}).get("attributes", {}) if isinstance(token_json, dict) else {}
    return attr.get("symbol"), attr.get("name")
