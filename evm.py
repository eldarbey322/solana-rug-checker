"""Robinhood Chain (EVM): достаём данные из Blockscout/RPC/DexScreener и отдаём в общее ядро."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import cache
import config
from core import Report, Tr, progress, run_core, safe, trace_from_cache, trace_to_cache
from deployer import investigate as evm_investigate

ZERO = "0x" + "0" * 40
BURN = {ZERO, "0x000000000000000000000000000000000000dead"}

DANGER_PATTERNS = {
    "mint": "допечатать токены",
    "blacklist": "заблокировать кошельки",
    "setbot": "пометить кошельки ботами",
    "addbot": "пометить кошельки ботами",
    "pause": "остановить торговлю",
    "settax": "менять налог",
    "setfee": "менять комиссию",
    "updatefee": "менять комиссию",
    "setsell": "менять условия продажи",
    "maxtx": "ограничить размер сделки",
    "maxwallet": "ограничить размер кошелька",
    "upgrade": "обновить код контракта",
    "settrading": "выключить торговлю",
}


def _addr(o):
    if isinstance(o, str):
        return o.lower()
    h = (o or {}).get("hash")
    return h.lower() if h else None


def _is_contract(o):
    return bool((o or {}).get("is_contract"))


def _tags(o):
    o = o or {}
    out = [o.get("name")]
    out += [t.get("label") or t.get("display_name") for t in o.get("public_tags") or []]
    out += [t.get("name") for t in ((o.get("metadata") or {}).get("tags") or [])]
    return [x for x in out if x]


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def _int(x, default=0):
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


INIT_TOPIC = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"     # PoolManager.Initialize
MODIFY_TOPIC = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec"   # PoolManager.ModifyLiquidity


def _pad(a):
    return "0x" + "0" * 24 + a[2:].lower()


def _sint(word):
    v = int(word, 16)
    return v - 2 ** 256 if v >= 2 ** 255 else v


# названия методов, которыми добавляют/забирают ликвидность (Uniswap v2/v3/v4 и т.п.)
LIQ_WORDS = ("liquidit", "addliq", "removeliq", "increaseliq", "decreaseliq",
             "mintposition", "burnposition")


def _is_liq(method):
    m = (method or "").lower()
    return any(w in m for w in LIQ_WORDS)


class EvmChecker:
    def __init__(self, bs, rpc, dex):
        self.bs, self.rpc, self.dex = bs, rpc, dex

    # --- методы, которые зовёт ядро ---
    async def trace(self, addr):
        """Кто первым прислал кошельку деньги + возраст и активность кошелька (с кэшем)."""
        hit = cache.get("trace", f"rh:{addr}")
        if hit:
            return trace_from_cache(hit)
        res = await self._trace(addr)
        cache.put("trace", f"rh:{addr}", trace_to_cache(res))
        return res

    async def _trace(self, addr):
        txs = await self.bs.address_txs(addr)
        has_in = any(_addr(t.get("to")) == addr and _int(t.get("value")) > 0 for t in txs)
        # внутренние транзакции (рассыльщики) смотрим, только если обычных пополнений не видно
        ints = [] if (has_in or len(txs) >= 50) else (await safe(self.bs.address_internal(addr)) or [])
        incoming, all_ts = [], []
        for t in txs:
            ts = _ts(t.get("timestamp"))
            if ts:
                all_ts.append(ts)
            if _addr(t.get("to")) == addr and _int(t.get("value")) > 0 and ts:
                incoming.append((ts, _addr(t.get("from")), _tags(t.get("from")), None))
        for t in ints:
            ts = _ts(t.get("timestamp"))
            if ts:
                all_ts.append(ts)
            if _addr(t.get("to")) == addr and _int(t.get("value")) > 0 and ts:
                incoming.append((ts, _addr(t.get("from")), _tags(t.get("from")),
                                 t.get("transaction_hash")))
        full = len(txs) >= 50
        funder, tagged = None, False
        if incoming and not full:   # у старого активного кошелька "первого спонсора" не видно
            ts, funder, tags, via_tx = min(incoming, key=lambda x: x[0])
            tagged = bool(tags)
            if via_tx:   # деньги пришли через контракт-рассыльщик — спонсор = отправитель tx
                tx = await safe(self.bs.tx(via_tx))
                if tx and _addr(tx.get("from")):
                    funder = _addr(tx.get("from"))
                    tagged = bool(_tags(tx.get("from")))
        return {"funder": funder, "funder_tagged": tagged,
                "first_ts": min(all_ts) if all_ts else None,
                "tx_count": len(txs), "full_page": full}

    async def funded(self, addr):
        """Кому этот кошелёк слал ETH (последние 50 транзакций)."""
        out = []
        for t in await self.bs.address_txs(addr):
            to = _addr(t.get("to"))
            if _addr(t.get("from")) == addr and _int(t.get("value")) > 0 and to and not _is_contract(t.get("to")):
                out.append(to)
        return out

    async def is_exchange(self, addr):
        hit = cache.get("exch", f"rh:{addr}")
        if hit is not None:
            return hit
        c = await self.bs.counters(addr) or {}
        res = _int(c.get("transactions_count")) > config.EXCHANGE_TX_THRESHOLD
        cache.put("exch", f"rh:{addr}", res)
        return res

    async def investigate(self, r):
        return await evm_investigate(self.bs, self.dex, self.trace, r.deployer, r.factory, r.token)

    async def _lp_from_pool(self, r):
        """ТОЧНАЯ проверка ликвидности по событиям пула Uniswap v4 — не зависит от истории переводов.
        Каждое добавление/изъятие = событие ModifyLiquidity; позиция = (кто добавил, номер NFT)."""
        pm = config.UNI_V4_POOL_MANAGER
        tok = _pad(r.token)
        init = []
        # Initialize(id, currency0, currency1, …): id = topic1, currency0 = topic2, currency1 = topic3.
        # В паре с ETH токен всегда currency1 (ETH = адрес 0 идёт первым).
        for kw in ({"topic3": tok}, {"topic2": tok}):
            res = await safe(self.bs.get_logs(pm, INIT_TOPIC, max_pages=1, **kw))
            init += res or []
        pool_ids = list(dict.fromkeys(lg["topics"][1].lower() for lg in init
                                      if len(lg.get("topics") or []) > 1 and lg["topics"][1]))
        pair = ((r.pool or {}).get("pair") or "").lower()
        if pair and len(pair) == 66 and pair not in pool_ids:
            pool_ids.append(pair)
        if not pool_ids:
            return False
        events = []
        for pid in pool_ids[:3]:
            logs = await safe(self.bs.get_logs(pm, MODIFY_TOPIC, topic1=pid, max_pages=10))
            if logs is None and self._creation_block is not None:
                logs = await safe(self.rpc.logs(pm, [MODIFY_TOPIC, pid], self._creation_block))
            events += logs or []
        positions = {}
        for lg in events:
            t, data = lg.get("topics") or [], (lg.get("data") or "0x")[2:]
            if len(t) < 3 or len(data) < 256:
                continue
            sender = "0x" + t[2][-40:].lower()
            w = [data[i:i + 64] for i in range(0, 256, 64)]
            delta, salt = _sint(w[2]), int(w[3], 16)
            p = positions.setdefault((sender, salt), {"added": 0, "removed": 0})
            if delta > 0:
                p["added"] += delta
            else:
                p["removed"] += -delta
        total_added = sum(p["added"] for p in positions.values())
        if not total_added:
            return False

        # владелец каждой живой позиции: NFT -> ownerOf(номер) (RPC, потом Blockscout); иначе — сам контракт
        async def resolve_owner(sender, salt):
            if not salt:
                return sender                            # позиция без номера — принадлежит контракту
            o = await safe(self.rpc.owner_of(sender, salt))
            if o and o != "none":
                return o
            try:                                         # второй источник — Blockscout
                inst = await self.bs.token_instance(sender, salt)
            except Exception:
                inst = False                             # сбой — не знаем
            if inst:
                return _addr(inst.get("owner")) or None
            if inst is None or o == "none":              # такого NFT нет — позиция у контракта
                return sender
            return None                                  # оба источника не ответили

        live = {k: p for k, p in positions.items() if p["added"] > p["removed"]}
        resolved = await asyncio.gather(*(resolve_owner(sd, sl) for sd, sl in live))
        owners = {k: (o, live[k]["added"] - live[k]["removed"]) for k, o in zip(live, resolved)}

        # кошелёк или контракт: RPC, потом Blockscout; не узнали — не гадаем
        addrs = {o for o, _ in owners.values() if o and o not in BURN}
        kinds = await safe(self.rpc.codes(addrs)) or {}
        for a in addrs - set(kinds):
            info = await safe(self.bs.address(a))
            if info is not None:
                kinds[a] = bool(info.get("is_contract"))
        def kind(o):
            if o is None:
                return "unknown"
            if o in BURN:
                return "burned"
            if o not in kinds:
                return "unknown"
            return "contract" if kinds[o] else "wallet"

        current = sum(n for _, n in owners.values())
        share = lambda k: sum(n for o, n in owners.values() if kind(o) == k)
        r.lp_source, r.lp_positions = "pool", len(positions)
        r.lp_removed_pct = 100 * (1 - current / total_added)
        r.lp_eoa_share = 100 * share("wallet") / current if current else 0
        liq_usd = (r.pool or {}).get("liquidity_usd")

        if r.lp_removed_pct >= 50 and (liq_usd is None or liq_usd < 5000):
            r.lp_status = "removed"                      # и позиции уменьшились, и денег в пуле почти нет
        elif current and share("wallet") >= 0.3 * current:
            r.lp_status = "held"
            r.lp_provider = max(((o, n) for o, n in owners.values() if kind(o) == "wallet"), key=lambda x: x[1])[0]
        elif current and share("unknown") >= 0.3 * current:
            r.lp_status = "held_unknown"                 # не смогли узнать владельца — честно говорим
        elif r.lp_removed_pct >= 50:
            r.lp_status = "partial"                      # позиции сильно уменьшались, но пул живой
        elif share("contract") > share("burned"):
            r.lp_holder = max(((o, n) for o, n in owners.values() if kind(o) == "contract"), key=lambda x: x[1])[0]
            r.lp_status = "launchpad" if r.factory else "locked"
        else:
            r.lp_status = "burned"
        r.lp_provider_is_dev = bool(r.lp_provider and r.lp_provider == r.deployer)
        return True

    async def _lp_check(self, r, trs, raw_by_key):
        """Кто добавил ликвидность, где сейчас его позиция и не забрал ли он её уже."""
        big = r.supply * 0.03
        method = lambda t: (raw_by_key.get(id(t)) or {}).get("method")
        adds = [t for t in trs if not t.frm_c and t.to_c and t.frm not in BURN and t.tx
                and (_is_liq(method(t)) or (t.frm == r.deployer and t.value >= big))][:8]
        tt_lists = await asyncio.gather(*(safe(self.bs.tx_token_transfers(t.tx)) for t in adds))
        prov = {}
        for t, lst in zip(adds, tt_lists):
            nft = None
            for it in lst or []:
                tok = it.get("token") or {}
                if "721" in (tok.get("type") or "") and _addr(it.get("from")) == ZERO:
                    c = (tok.get("address_hash") or tok.get("address") or "").lower()
                    tid = (it.get("total") or {}).get("token_id")
                    if c and tid is not None:
                        nft = (c, str(tid), _addr(it.get("to")))
            if nft or _is_liq(method(t)):
                p = prov.setdefault(t.frm, {"added": 0, "removed": 0, "nfts": [], "txs": set()})
                p["added"] += t.value
                p["txs"].add(t.tx)
                if nft:
                    p["nfts"].append(nft)
        if not prov:
            r.lp_status = "launchpad" if r.factory else "unknown"
            return
        for t in trs:   # изъятие: из пула обратно поставщику ликвидности
            p = prov.get(t.to)
            if p and t.frm_c and t.tx not in p["txs"] and (_is_liq(method(t)) or t.value >= 0.3 * p["added"]):
                p["removed"] += t.value
                p["txs"].add(t.tx)
        lp_txs = set().union(*(p["txs"] for p in prov.values()))
        for t in trs:
            if t.tx in lp_txs and (t.frm in prov or t.to in prov) and (t.frm_c or t.to_c):
                t.lp = True

        main_addr, main = max(prov.items(), key=lambda kv: kv[1]["added"])
        r.lp_provider, r.lp_provider_is_dev = main_addr, main_addr == r.deployer
        r.lp_removed_pct = 100 * main["removed"] / main["added"] if main["added"] else 0
        if r.lp_removed_pct >= 50:
            r.lp_status = "removed"
            return
        if not main["nfts"]:
            r.lp_status = "held_unknown"
            return
        owners = []
        for c, tid, _ in main["nfts"][:4]:
            inst = await safe(self.bs.token_instance(c, tid)) or {}
            o = inst.get("owner")
            owners.append((_addr(o), _is_contract(o) if isinstance(o, dict) else False))
        owners = [(a, is_c) for a, is_c in owners if a]      # не смогли прочитать — не гадаем
        if not owners:
            r.lp_status = "held_unknown"
        elif any(a not in BURN and not is_c for a, is_c in owners):
            r.lp_status = "held"
            r.lp_holder = next(a for a, is_c in owners if a not in BURN and not is_c)
        elif any(is_c for a, is_c in owners):
            r.lp_status = "locked"
            r.lp_holder = next(a for a, is_c in owners if is_c)
        else:
            r.lp_status = "burned"

    # --- основной анализ ---
    async def analyze(self, token: str) -> Report:
        token = token.lower()
        r = Report(token=token, chain="robinhood",
                   launch_label=f"в первые {config.TAX_WINDOW_S:.0f} сек под налогом 99%")

        info = await self.bs.token(token)
        if not info:
            raise ValueError("токен не найден в Blockscout Robinhood Chain — проверь адрес")
        r.name = info.get("name") or "?"
        r.symbol = info.get("symbol") or "?"
        r.decimals = _int(info.get("decimals"), 18)
        r.supply = _int(info.get("total_supply"))
        r.holders_count = _int(info.get("holders_count") or info.get("holders"))
        if r.supply <= 0:
            raise ValueError("не удалось получить supply токена")

        progress("📦 токен, холдеры, запуск…")
        to = config.OPTIONAL_TIMEOUT_S
        opt = lambda coro: safe(asyncio.wait_for(coro, timeout=to))   # второстепенное не ждём вечно
        addr_info, sc, (holders, holders_complete), pool, owner, asc = await asyncio.gather(
            self.bs.address(token),
            opt(self.bs.smart_contract(token)),
            self.bs.holders(token),
            opt(self.dex.pool(token)),
            opt(self.rpc.owner(token)),
            opt(self.bs.tokentx_asc(token)),
        )
        # запуск получили отдельно -> для свежих сделок хватит 20 страниц; иначе листаем глубже
        pages = min(20, config.MAX_TRANSFER_PAGES) if asc else config.MAX_TRANSFER_PAGES
        progress(f"📜 листаю свежие переводы (до {pages} стр.)…")
        raw_trs, complete = await self.bs.transfers(token, max_pages=pages)
        if asc and not complete and len(asc) < config.LAUNCH_TRANSFERS:
            complete_hist = True       # первые переводы закончились раньше лимита = история короткая
        else:
            complete_hist = complete
        addr_info = addr_info or {}
        r.pool = pool

        # контракт
        r.verified = bool(addr_info.get("is_verified") or (sc or {}).get("is_verified"))
        r.is_proxy = bool(addr_info.get("implementations")) or bool(
            (sc or {}).get("proxy_type") or addr_info.get("proxy_type"))
        seen = set()
        for item in (sc or {}).get("abi") or []:
            if item.get("type") != "function" or item.get("stateMutability") in ("view", "pure"):
                continue
            nm = (item.get("name") or "").lower()
            for pat, desc in DANGER_PATTERNS.items():
                if pat in nm and desc not in seen:
                    seen.add(desc)
                    r.danger_funcs.append(f"{item.get('name')}() — может {desc}")
        r.owner = owner
        r.owner_renounced = (owner in BURN) if owner else None

        # создатель
        creation_tx = addr_info.get("creation_transaction_hash") or addr_info.get("creation_tx_hash")
        creator = (addr_info.get("creator_address_hash") or "").lower() or None
        r.deployer = creator
        self._creation_block = None
        if creation_tx:
            tx = await safe(self.bs.tx(creation_tx))
            if tx:
                self._creation_block = _int(tx.get("block_number") or tx.get("block"), None)
                r.deployer = _addr(tx.get("from")) or creator
                to = tx.get("to")
                if to and _is_contract(to):
                    r.factory = _addr(to)

        # холдеры
        hb, infra = {}, set(config.KNOWN_INFRA)
        for h in holders:
            a = _addr(h.get("address"))
            if not a:
                continue
            hb[a] = _int(h.get("value"))
            if _is_contract(h.get("address")):
                infra.add(a)
        if r.factory:
            infra.add(r.factory)

        # переводы
        trs, raw_by_key = [], {}
        for t in raw_trs:
            frm, to = _addr(t.get("from")), _addr(t.get("to"))
            if not frm or not to:
                continue
            tr = Tr(frm, to, _int((t.get("total") or {}).get("value")), _ts(t.get("timestamp")),
                    _int(t.get("block_number") or t.get("block")), _int(t.get("log_index")),
                    _is_contract(t.get("from")) or frm in infra,
                    _is_contract(t.get("to")) or to in infra,
                    tx=(t.get("transaction_hash") or t.get("tx_hash") or "").lower())
            trs.append(tr)
            raw_by_key[id(tr)] = t
        # длинная история: запуск берём отдельным запросом «от старых к новым»
        launch_ts = None
        if not complete and asc:
            known = {}
            for t in raw_trs:
                for side in ("from", "to"):
                    a = _addr(t.get(side))
                    if a:
                        known[a] = _is_contract(t.get(side))
            unknown = {(it.get("from") or "").lower() for it in asc} | {(it.get("to") or "").lower() for it in asc}
            unknown -= set(known) | infra | BURN | {""}
            codes = await safe(self.rpc.codes(unknown)) or {}
            infra |= {a for a, c in {**known, **codes}.items() if c}
            seen = {(t.tx, t.log) for t in trs}
            for it in asc:
                frm, to = (it.get("from") or "").lower(), (it.get("to") or "").lower()
                if not frm or not to:
                    continue
                tx, log = (it.get("hash") or "").lower(), _int(it.get("logIndex"))
                if (tx, log) in seen:
                    continue
                tr = Tr(frm, to, _int(it.get("value")),
                        datetime.fromtimestamp(_int(it.get("timeStamp")), timezone.utc) if it.get("timeStamp") else None,
                        _int(it.get("blockNumber")), log, frm in infra, to in infra, tx=tx)
                trs.append(tr)
            first = min((datetime.fromtimestamp(_int(it["timeStamp"]), timezone.utc)
                         for it in asc if it.get("timeStamp")), default=None)
            if first:
                launch_ts = first
                if complete_hist:          # первые переводы и есть вся история — склеили целиком
                    complete = True
                else:
                    r.notes.append(f"переводов много — разобрал запуск (первые {len(asc)}) и последние "
                                   f"{len(raw_trs)}, середину пропустил")
        trs.sort(key=lambda t: (t.block, t.log))
        progress("💧 проверяю ликвидность (события пула)…")
        await self._lp_check(r, trs, raw_by_key)          # помечает LP-переводы, чтобы не считать их сделками
        if not await safe(self._lp_from_pool(r)):         # точная проверка по пулу; если не вышло —
            if r.lp_status:                                # остаётся оценка по переводам
                r.lp_source = "transfers"
        # «пул вытащили» подтверждаем деньгами: если в пуле ещё есть ликвидность — это не полный раг
        liq_usd = (r.pool or {}).get("liquidity_usd")
        if r.lp_status == "removed" and liq_usd is not None and liq_usd >= 5000:
            r.lp_status = "partial"
        if complete and trs:
            launch_ts = next((t.ts for t in trs if t.ts), None)

        # куда кошельки запуска дели токены — их личная история по этому токену
        if launch_ts and not complete:
            first_buy = {}
            for t in sorted(trs, key=lambda t: (t.block, t.log)):
                if t.frm_c and not t.to_c and t.to not in BURN and t.to not in first_buy:
                    first_buy[t.to] = t
            early = [a for a, t in first_buy.items()
                     if t.ts and (t.ts - launch_ts).total_seconds() <= config.SNIPE_WINDOW_S]
            early = sorted(early, key=lambda a: -first_buy[a].value)[: config.LAUNCH_WALLETS_TRACE]
            if r.deployer and r.deployer not in early:
                early.append(r.deployer)
            if early:
                progress(f"🔍 смотрю, куда {len(early)} кошельков запуска дели токены…")
                seen = {(t.tx, t.log) for t in trs}
                hist = await asyncio.gather(*(safe(self.bs.address_token_transfers(a, token)) for a in early))
                for lst in hist:
                    for it in lst or []:
                        tok = it.get("token") or {}
                        ta = (tok.get("address_hash") or tok.get("address") or token).lower()
                        if ta != token:
                            continue
                        frm, to = _addr(it.get("from")), _addr(it.get("to"))
                        tx = (it.get("transaction_hash") or it.get("tx_hash") or "").lower()
                        log = _int(it.get("log_index"))
                        if not frm or not to or (tx, log) in seen:
                            continue
                        seen.add((tx, log))
                        trs.append(Tr(frm, to, _int((it.get("total") or {}).get("value")), _ts(it.get("timestamp")),
                                      _int(it.get("block_number") or it.get("block")), log,
                                      _is_contract(it.get("from")) or frm in infra,
                                      _is_contract(it.get("to")) or to in infra, tx=tx))
        if not launch_ts:
            r.notes.append(f"история длинная — смотрел последние {len(trs)} переводов, "
                           "анализ снайперов на запуске пропущен")

        return await run_core(
            r, self, hb=hb, holders_complete=holders_complete, infra=infra, burn=BURN,
            trs=trs, complete=complete, launch_ts=launch_ts, launch_block=None,
            launch_mode="seconds", launch_window=config.TAX_WINDOW_S, same_block_scope="snipers")
