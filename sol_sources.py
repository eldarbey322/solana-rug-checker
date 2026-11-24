"""Solana: Helius (RPC + DAS + Enhanced Transactions), RugCheck, Feyn.

Плюс две утилиты без зависимостей:
  b58decode / b58encode
  is_wallet(addr) — адрес лежит на кривой ed25519 => это обычный кошелёк.
    Пулы, кривые pump.fun и прочие программные адреса (PDA) на кривой НЕ лежат.
"""
from __future__ import annotations

import asyncio

import httpx

import config

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_IDX = {c: i for i, c in enumerate(B58)}


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58_IDX[c]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + body


def b58encode(b: bytes) -> str:
    n, out = int.from_bytes(b, "big"), ""
    while n:
        n, rem = divmod(n, 58)
        out = B58[rem] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


def is_sol_address(s: str) -> bool:
    try:
        return len(b58decode(s)) == 32
    except (KeyError, ValueError):
        return False


_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_wallet_cache: dict[str, bool] = {}


def is_wallet(addr: str) -> bool:
    """True = точка на кривой ed25519 (обычный кошелёк), False = PDA/программа."""
    if not addr:
        return False
    hit = _wallet_cache.get(addr)
    if hit is not None:
        return hit
    try:
        b = b58decode(addr)
    except (KeyError, ValueError):
        return False
    ok = False
    if len(b) == 32:
        y = int.from_bytes(b, "little") & ((1 << 255) - 1)
        if y < _P:
            u, v = (y * y - 1) % _P, (_D * y * y + 1) % _P
            x2 = u * pow(v, _P - 2, _P) % _P
            ok = x2 == 0 or pow(x2, (_P - 1) // 2, _P) == 1
    _wallet_cache[addr] = ok
    return ok


class Helius:
    def __init__(self, key=config.HELIUS_API_KEY, rpc_url=config.HELIUS_RPC_URL,
                 api_url=config.HELIUS_API_URL, concurrency=config.SOL_CONCURRENCY):
        self.key = key
        self.rpc_url = rpc_url.rstrip("/") + "/"
        self.api_url = api_url.rstrip("/")
        self.sem = asyncio.Semaphore(concurrency)
        self.client = httpx.AsyncClient(timeout=30)

    async def close(self):
        await self.client.aclose()

    async def _req(self, method, url, **kw):
        last = None
        for attempt in range(8):
            try:
                async with self.sem:
                    r = await self.client.request(method, url, **kw)
            except httpx.TransportError as e:
                last = e
                await asyncio.sleep(1 + attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                await asyncio.sleep(min(10, 1.5 * (attempt + 1)))
                continue
            if r.status_code == 401:
                raise RuntimeError("Helius отклонил ключ — проверь HELIUS_API_KEY")
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Helius не отвечает: {last}")

    async def rpc(self, method, params):
        res = await self._req("POST", self.rpc_url, params={"api-key": self.key},
                              json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in res:
            raise RuntimeError(f"Helius {method}: {res['error'].get('message')}")
        return res.get("result")

    async def account_info(self, addr):
        res = await self.rpc("getAccountInfo", [addr, {"encoding": "jsonParsed"}])
        return (res or {}).get("value")

    async def asset(self, mint):
        return await self.rpc("getAsset", {"id": mint})

    async def token_accounts(self, mint, max_pages=config.SOL_HOLDER_PAGES):
        """Все токен-аккаунты (с владельцем). Возвращает (список, complete)."""
        items = []
        for page in range(1, max_pages + 1):
            res = await self.rpc("getTokenAccounts", {"mint": mint, "page": page, "limit": 1000})
            batch = (res or {}).get("token_accounts") or []
            items += batch
            if len(batch) < 1000:
                return items, True
        return items, False

    async def signatures(self, addr, max_pages=None):
        """Подписи транзакций, от новых к старым. Возвращает (список, дошли_до_начала).
        Берём ровно столько, сколько нужно для разбора (запуск берётся отдельно)."""
        if max_pages is None:
            max_pages = config.SOL_MAX_PARSE // 1000 + 1
        out, before = [], None
        for _ in range(max_pages):
            opts = {"limit": 1000}
            if before:
                opts["before"] = before
            batch = await self.rpc("getSignaturesForAddress", [addr, opts]) or []
            out += batch
            if len(batch) < 1000:
                return out, True
            before = batch[-1]["signature"]
        return out, False

    async def parse(self, sigs):
        """Разобранные транзакции (Enhanced Transactions API), батчами по 100."""
        batches = [sigs[i:i + 100] for i in range(0, len(sigs), 100)]
        res = await asyncio.gather(*(
            self._req("POST", f"{self.api_url}/v0/transactions", params={"api-key": self.key},
                      json={"transactions": b}) for b in batches))
        return [tx for batch in res for tx in (batch or []) if tx]

    async def address_txs(self, addr, limit=100, token_accounts=None, before=None):
        """Последние разобранные транзакции кошелька (новые сначала).
        token_accounts='balanceChanged' — включая поступления на его токен-аккаунты
        (иначе кошелёк, который только ПОЛУЧАЛ токены, выглядит пустым)."""
        params = {"api-key": self.key, "limit": limit}
        if token_accounts:
            params["token-accounts"] = token_accounts
        if before:
            params["before-signature"] = before
        res = await self._req("GET", f"{self.api_url}/v0/addresses/{addr}/transactions", params=params)
        return res if isinstance(res, list) else []

    async def wallet_mint_txs(self, wallet, mint, pages=3):
        """Все транзакции кошелька с этим токеном (до pages×100 последних) — куда он дел токены."""
        out, before = [], None
        for _ in range(pages):
            batch = await self.address_txs(wallet, token_accounts="balanceChanged", before=before)
            if not batch:
                break
            out += [tx for tx in batch if any(tt.get("mint") == mint for tt in tx.get("tokenTransfers") or [])]
            if len(batch) < 100:
                break
            before = batch[-1].get("signature")
        return out

    async def oldest_txs(self, addr, total=400):
        """Самые первые транзакции адреса (от старых к новым), уже разобранные.
        Так находим создание токена, сколько бы транзакций ни было потом."""
        out, after = [], None
        while len(out) < total:
            params = {"api-key": self.key, "limit": 100, "sort-order": "asc"}
            if after:
                params["after-signature"] = after
            res = await self._req("GET", f"{self.api_url}/v0/addresses/{addr}/transactions", params=params)
            if not isinstance(res, list) or not res:
                break
            out += res
            if len(res) < 100:
                break
            after = res[-1].get("signature")
        return out

    async def sig_count(self, addr):
        res = await self.rpc("getSignaturesForAddress", [addr, {"limit": 1000}])
        return len(res or [])


class SecondOpinion:
    """Готовые чекеры как второе мнение: RugCheck (бесплатно) и Feyn (по ключу, только pump.fun)."""

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=15)

    async def close(self):
        await self.client.aclose()

    async def rugcheck(self, mint):
        r = await self.client.get(f"{config.RUGCHECK_URL}/v1/tokens/{mint}/report/summary")
        if r.status_code != 200:
            return None
        d = r.json() or {}
        risks = [x for x in d.get("risks") or [] if (x.get("level") or "").lower() in ("danger", "warn")]
        return {
            "score": d.get("score_normalised", d.get("score")),
            "rugged": bool(d.get("rugged")),
            "risks": [(x.get("level"), x.get("name")) for x in risks][:5],
            "lp_locked_pct": (float(d["lpLockedPct"]) if isinstance(d.get("lpLockedPct"), (int, float)) else None),
        }

    async def feyn(self, mint):
        if not config.FEYN_API_KEY:
            return None
        r = await self.client.get(f"{config.FEYN_URL}/v1/token/{mint}/summary",
                                  headers={"Authorization": f"Bearer {config.FEYN_API_KEY}"})
        if r.status_code != 200:
            return None
        v = ((r.json() or {}).get("token") or {}).get("verdict") or {}
        return {"level": v.get("level"), "headline": v.get("headline")}
