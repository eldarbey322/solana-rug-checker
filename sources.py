"""Источники данных: Blockscout (холдеры, переводы, история кошельков), RPC, DexScreener."""
from __future__ import annotations

import asyncio

import httpx

import time

import config
from core import progress


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def hexint(v, default=0):
    """'0x1a' / '26' / 26 / '0x' / None -> int."""
    if v is None:
        return default
    if isinstance(v, int):
        return v
    v = str(v).strip()
    try:
        if v.lower().startswith("0x"):
            return int(v, 16) if len(v) > 2 else 0
        return int(v) if v else default
    except ValueError:
        return default


class Blockscout:
    def __init__(self, base=None, api_key=config.BLOCKSCOUT_API_KEY,
                 concurrency=config.CONCURRENCY):
        self.key = api_key
        self.base = (base or (config.BLOCKSCOUT_PRO_URL if api_key else config.BLOCKSCOUT_URL)).rstrip("/")
        self.sem = asyncio.Semaphore(concurrency)
        self.client = httpx.AsyncClient(timeout=20, headers={
            "accept": "application/json",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) rugbot/1.0",
        })
        self._last_wait_msg = 0.0

    def _say_wait(self, why):
        if time.time() - self._last_wait_msg > 10:
            self._last_wait_msg = time.time()
            progress(f"⏳ Blockscout: {why}, жду и повторяю…")

    async def close(self):
        await self.client.aclose()

    async def get(self, path, params=None):
        params = dict(params or {})
        if self.key:
            params["apikey"] = self.key
        url = f"{self.base}{path}"
        last = None
        for attempt in range(5):
            try:
                async with self.sem:
                    r = await self.client.get(url, params=params)
            except httpx.TransportError as e:
                last = e
                self._say_wait("не отвечает")
                await asyncio.sleep(1 + attempt)
                continue
            if r.status_code == 404:
                return None
            if r.status_code in (401, 403):
                if not self.key:
                    raise RuntimeError(
                        "Blockscout не пускает без ключа (403). Возьми бесплатный ключ на "
                        "https://dev.blockscout.com и впиши его в .env: BLOCKSCOUT_API_KEY=...")
                raise RuntimeError(f"Blockscout отклонил ключ ({r.status_code}) — проверь BLOCKSCOUT_API_KEY в .env")
            if r.status_code == 429 or r.status_code >= 500:
                last = httpx.HTTPStatusError(f"{r.status_code} {url}", request=r.request, response=r)
                self._say_wait("лимит запросов" if r.status_code == 429 else f"ошибка {r.status_code}")
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Blockscout не отвечает: {last}")

    async def paginate(self, path, params=None, max_pages=10, label=None, budget_s=None):
        """Возвращает (items, complete). complete=False, если упёрлись в лимит страниц или времени."""
        items, page = [], {}
        t0 = time.time()
        budget = budget_s or config.PAGINATE_BUDGET_S
        for i in range(max_pages):
            if i and time.time() - t0 > budget:
                if label:
                    progress(f"   {label}: хватит, беру {len(items)} (лимит {budget} сек)")
                return items, False
            try:
                data = await self.get(path, {**(params or {}), **page})
            except RuntimeError:
                if items:            # что успели — то и берём
                    return items, False
                raise
            if not data:
                return items, True
            items.extend(data.get("items") or [])
            if label and (i + 1) % 5 == 0:
                progress(f"   {label}: страница {i + 1}, записей {len(items)}…")
            nxt = data.get("next_page_params")
            if not nxt:
                return items, True
            page = {k: v for k, v in nxt.items() if v is not None}
        return items, False

    # --- удобные обёртки ---
    async def token(self, addr):
        return await self.get(f"/tokens/{addr}")

    async def address(self, addr):
        return await self.get(f"/addresses/{addr}")

    async def smart_contract(self, addr):
        return await self.get(f"/smart-contracts/{addr}")

    async def holders(self, addr):
        return await self.paginate(f"/tokens/{addr}/holders", max_pages=config.MAX_HOLDER_PAGES, label="холдеры")

    async def transfers(self, addr, max_pages=None):
        return await self.paginate(f"/tokens/{addr}/transfers", max_pages=max_pages or config.MAX_TRANSFER_PAGES,
                                   label="переводы")

    async def address_txs(self, addr):
        data = await self.get(f"/addresses/{addr}/transactions")
        return (data or {}).get("items") or []

    async def address_internal(self, addr):
        data = await self.get(f"/addresses/{addr}/internal-transactions")
        return (data or {}).get("items") or []

    async def counters(self, addr):
        return await self.get(f"/addresses/{addr}/counters") or {}

    async def tx(self, tx_hash):
        return await self.get(f"/transactions/{tx_hash}")

    async def tx_token_transfers(self, tx_hash):
        data = await self.get(f"/transactions/{tx_hash}/token-transfers")
        return (data or {}).get("items") or []

    # ---------- Etherscan-совместимый API (логи событий из индекса, без ограничений по блокам) ----------
    _eth_variant = None

    async def eth_api(self, **params):
        """Пробуем варианты адреса из документации Blockscout, запоминаем рабочий."""
        if not self.key:
            return None
        variants = [
            ("https://api.blockscout.com/4663/api", {}),
            (config.BLOCKSCOUT_ETH_API, {"chain_id": "4663"}),
            (config.BLOCKSCOUT_ETH_API, {"chainid": "4663"}),
        ]
        order = [self._eth_variant] if self._eth_variant is not None else range(len(variants))
        for i in order:
            url, extra = variants[i]
            d = None
            for attempt in range(4):
                try:
                    async with self.sem:
                        r = await self.client.get(url, params={**params, **extra, "apikey": self.key})
                except httpx.HTTPError:
                    await asyncio.sleep(1 + attempt)
                    continue
                if r.status_code == 429 or r.status_code >= 500:
                    self._say_wait("лимит запросов" if r.status_code == 429 else f"ошибка {r.status_code}")
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if r.status_code == 200:
                    try:
                        d = r.json() or {}
                    except ValueError:
                        d = None
                break
            if d is None:
                continue
            if isinstance(d.get("result"), str) and "rate limit" in d["result"].lower():
                await asyncio.sleep(2)
                continue
            res = d.get("result")
            if isinstance(res, list):
                self._eth_variant = i
                return res
            if str(d.get("message", "")).lower().startswith("no "):   # «No logs found» и т.п.
                self._eth_variant = i
                return []
        return None

    async def get_logs(self, address, topic0, topic1=None, topic2=None, topic3=None, from_block=0, max_pages=10):
        """События контракта от старых к новым (по 1000 за запрос, листаем по блокам)."""
        out, frm, seen = [], from_block, set()
        for _ in range(max_pages):
            p = dict(module="logs", action="getLogs", address=address, topic0=topic0,
                     fromBlock=frm, toBlock="latest")
            if topic1:
                p.update(topic1=topic1, topic0_1_opr="and")
            if topic2:
                p.update(topic2=topic2, topic0_2_opr="and")
            if topic3:
                p.update(topic3=topic3, topic0_3_opr="and")
            res = await self.eth_api(**p)
            if res is None:
                return None            # неполные логи хуже, чем никакие
            for lg in res:
                key = (lg.get("transactionHash"), hexint(lg.get("logIndex")), hexint(lg.get("blockNumber")))
                if key not in seen:
                    seen.add(key)
                    out.append(lg)
            if len(res) < 1000:
                return out
            # следующая страница с того же блока (в нём могли остаться события), дубли отсекаем
            nxt = hexint(res[-1].get("blockNumber"))
            if nxt <= frm:
                nxt = frm + 1
            frm = nxt
        return None                    # не дошли до конца — данные неполные

    async def _first_logs(self, address, topic0):
        """Только первая страница (для запуска нам не нужна вся история)."""
        p = dict(module="logs", action="getLogs", address=address, topic0=topic0, fromBlock=0, toBlock="latest")
        return await self.eth_api(**p)

    async def tokentx_asc(self, token, n=None):
        """Первые переводы токена (от старых к новым). Сначала через логи Transfer, потом tokentx."""
        logs = await self._first_logs(token, TRANSFER_TOPIC)
        if logs:
            out = []
            for lg in logs[: n or config.LAUNCH_TRANSFERS]:
                t = lg.get("topics") or []
                if len(t) < 3:
                    continue
                hx = hexint
                out.append({"from": "0x" + t[1][-40:], "to": "0x" + t[2][-40:],
                            "value": str(hx(lg.get("data") or "0x0")),
                            "timeStamp": str(hx(lg.get("timeStamp"))), "blockNumber": str(hx(lg.get("blockNumber"))),
                            "logIndex": str(hx(lg.get("logIndex") or "0x0")), "hash": lg.get("transactionHash")})
            if out:
                return out
        return await self.eth_api(module="account", action="tokentx", contractaddress=token,
                                  sort="asc", page=1, offset=n or config.LAUNCH_TRANSFERS)

    async def token_instance(self, contract, token_id):
        return await self.get(f"/tokens/{contract}/instances/{token_id}")

    async def address_token_transfers(self, addr, token):
        items, _ = await self.paginate(f"/addresses/{addr}/token-transfers",
                                       {"token": token}, max_pages=4)
        return items


class RPC:
    def __init__(self, url=config.RPC_URL):
        self.url = url
        self.client = httpx.AsyncClient(timeout=15)

    async def close(self):
        await self.client.aclose()

    async def eth_call(self, to, data):
        body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": to, "data": data}, "latest"]}
        r = await self.client.post(self.url, json=body)
        res = r.json()
        if "error" in res:
            return None
        return res.get("result")

    async def codes(self, addrs):
        """Какие адреса — контракты. -> {адрес: True/False}. Чего узнать не удалось — в ответе нет."""
        out = {}
        addrs = list(addrs)
        for i in range(0, len(addrs), 100):
            chunk = addrs[i:i + 100]
            body = [{"jsonrpc": "2.0", "id": j, "method": "eth_getCode", "params": [a, "latest"]}
                    for j, a in enumerate(chunk)]
            try:
                r = await self.client.post(self.url, json=body)
                data = r.json()
            except (httpx.HTTPError, ValueError):
                data = None
            if isinstance(data, list):                 # батч поддерживается
                for item in data:
                    if isinstance(item, dict) and "result" in item and isinstance(item.get("id"), int):
                        out[chunk[item["id"]]] = (item["result"] or "0x") not in ("0x", "0x0")
                continue
            for a in chunk:                            # батча нет — по одному
                try:
                    r = await self.client.post(self.url, json={"jsonrpc": "2.0", "id": 1,
                                                               "method": "eth_getCode", "params": [a, "latest"]})
                    res = r.json().get("result")
                except (httpx.HTTPError, ValueError, AttributeError):
                    res = None
                if res is not None:
                    out[a] = res not in ("0x", "0x0")
        return out

    async def block_number(self):
        r = await self.client.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []})
        return int(r.json()["result"], 16)

    async def logs(self, address, topics, from_block, to_block=None, max_calls=80):
        """eth_getLogs с разбиением диапазона, если узел ругается на слишком большой запрос."""
        to_block = to_block if to_block is not None else await self.block_number()
        out, calls, step, cur = [], 0, to_block - from_block + 1, from_block
        while cur <= to_block and calls < max_calls:
            end = min(to_block, cur + step - 1)
            calls += 1
            r = await self.client.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
                                                        "params": [{"address": address, "topics": topics,
                                                                    "fromBlock": hex(cur), "toBlock": hex(end)}]})
            res = r.json()
            if "error" in res:
                if step <= 1000:
                    return out if out else None
                step //= 4
                continue
            out += res.get("result") or []
            cur = end + 1
        return out if cur > to_block else None          # не просмотрели весь диапазон — данные неполные

    async def owner_of(self, nft_contract, token_id):
        """-> адрес владельца | 'none' (вызов отклонён: такого NFT нет / это не NFT-контракт) | None (сбой)."""
        body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": nft_contract, "data": "0x6352211e" + format(token_id, "064x")}, "latest"]}
        try:
            res = (await self.client.post(self.url, json=body)).json()
        except (httpx.HTTPError, ValueError):
            return None
        if "error" in res:
            msg = str(res["error"]).lower()
            return "none" if ("revert" in msg or "execution" in msg) else None
        out = res.get("result") or ""
        if len(out) < 66:
            return "none"
        return ("0x" + out[-40:]).lower()

    async def owner(self, token):
        """owner() -> адрес или None, если такой функции нет."""
        res = await self.eth_call(token, "0x8da5cb5b")
        if not res or len(res) < 66:
            return None
        return ("0x" + res[-40:]).lower()


class DexScreener:
    def __init__(self, url=config.DEXSCREENER_URL):
        self.url = url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=15)

    async def close(self):
        await self.client.aclose()

    async def pool(self, token):
        """Самый ликвидный пул токена (или None, если токен ещё на кривой / не найден)."""
        r = await self.client.get(f"{self.url}/{token}")
        if r.status_code != 200:
            return None
        pairs = (r.json() or {}).get("pairs") or []
        pairs = [p for p in pairs
                 if (p.get("baseToken") or {}).get("address", "").lower() == token.lower()]
        if not pairs:
            return None
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        liq = best.get("liquidity") or {}
        return {
            "dex": best.get("dexId"),
            "pair": best.get("pairAddress"),
            "chain": best.get("chainId"),
            "liquidity_usd": float(liq.get("usd") or 0),
            "base_amount": float(liq["base"]) if liq.get("base") else None,
            "quote_amount": float(liq["quote"]) if liq.get("quote") else None,
            "quote_symbol": (best.get("quoteToken") or {}).get("symbol") or "ETH",
            "price_usd": float(best.get("priceUsd") or 0),
            "labels": best.get("labels") or [],
        }
