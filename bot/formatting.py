from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

def short_addr(addr: str, n: int = 4) -> str:
    if not addr or len(addr) < (n*2 + 3):
        return addr
    return f"{addr[:n]}…{addr[-n:]}"

def fmt_usd(x: Optional[float]) -> str:
    if x is None:
        return "—"
    if x >= 1_000_000_000:
        return f"${x/1_000_000_000:.2f}B"
    if x >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x/1_000:.2f}K"
    return f"${x:.2f}"

def fmt_ton(x: float) -> str:
    return f"{x:,.2f} TON"

def fmt_token_amt(x: float) -> str:
    if x >= 1_000_000:
        return f"{x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"{x/1_000:.2f}K"
    return f"{x:,.2f}"

@dataclass
class TradeMsg:
    ton_amount: float
    token_amount: float
    buyer: str
    tx_hash: Optional[str]
    pool_url: Optional[str]
    market_cap_usd: Optional[float]
    liquidity_usd: Optional[float]
    ts: int
