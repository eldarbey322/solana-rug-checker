"""Определяем сеть по адресу и отправляем в нужный адаптер."""
from __future__ import annotations

import re

from sol_sources import is_sol_address

EVM_RE = re.compile(r"0x[a-fA-F0-9]{40}")
SOL_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def detect(text: str):
    """Ищет адрес в тексте/ссылке. -> ('robinhood'|'solana', адрес) или (None, None)."""
    text = text or ""
    m = EVM_RE.search(text)
    if m:
        return "robinhood", m.group(0).lower()
    for m in SOL_RE.finditer(text):
        if is_sol_address(m.group(0)):
            return "solana", m.group(0)
    return None, None


class Checker:
    def __init__(self, evm=None, sol=None):
        self.evm, self.sol = evm, sol

    async def analyze(self, chain, addr):
        if chain == "robinhood":
            return await self.evm.analyze(addr)
        if self.sol is None:
            raise ValueError("Solana выключена: впиши HELIUS_API_KEY в .env (бесплатно на helius.dev)")
        return await self.sol.analyze(addr)
