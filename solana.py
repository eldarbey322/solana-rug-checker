"""Solana: достаём данные из Helius и отдаём в общее ядро.

Чем Solana отличается от Robinhood и как это учтено:
  * Контракта токена нет — есть mint-аккаунт. Проверяем mint authority, freeze authority
    и опасные расширения Token-2022 (постоянный делегат, transfer hook, налог, заморозка...).
  * Налога Pons нет. Бандл = покупки в слоте создания токена (Jito) + кошельки,
    за которых комиссию платил другой кошелёк.
  * Пулы/кривые pump.fun — программные адреса (PDA), отличаем их от кошельков математически.
  * Холдеры приходят токен-аккаунтами — сводим их к владельцам.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone

import cache
import config
from core import Report, Tr, classify_fate, progress, run_core, safe, trace_from_cache, trace_to_cache
from sol_liquidity import (cross_check_rugcheck, first_pool_event, launched_manually, liquidity_events,
                           lp_status)
from sol_sources import is_wallet

TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
MINT_SRC, BURN_DST = "MINT", "BURN"
INCINERATOR = "1nc1nerator11111111111111111111111111111111"
BURN = {MINT_SRC, BURN_DST, INCINERATOR}

OK, WARN, BAD = "✅", "⚠️", "❌"


def _ts(unix):
    return datetime.fromtimestamp(unix, timezone.utc) if unix else None


def _raw(ui, dec):
    try:
        return int(round(float(ui) * 10 ** dec))
    except (TypeError, ValueError):
        return 0


class SolanaChecker:
    def __init__(self, helius, dex, second):
        self.h, self.dex, self.second = helius, dex, second

    # ---------- методы для ядра ----------
    async def trace(self, addr):
        hit = cache.get("trace", f"sol:{addr}")
        if hit:
            return trace_from_cache(hit)
        res = await self._trace(addr)
        cache.put("trace", f"sol:{addr}", trace_to_cache(res))
        return res

    async def _trace(self, addr):
        txs = await self.h.address_txs(addr, token_accounts="balanceChanged")
        all_ts, incoming = [], []
        for tx in txs:
            ts = _ts(tx.get("timestamp"))
            if ts:
                all_ts.append(ts)
            for nt in tx.get("nativeTransfers") or []:
                f = nt.get("fromUserAccount")
                if nt.get("toUserAccount") == addr and (nt.get("amount") or 0) > 0 and f and f != addr \
                        and is_wallet(f) and ts:
                    incoming.append((ts, f))
        full = len(txs) >= 100
        funder = min(incoming)[1] if incoming and not full else None
        return {"funder": funder, "funder_tagged": False,
                "first_ts": min(all_ts) if all_ts else None,
                "tx_count": len(txs), "full_page": full}

    async def funded(self, addr):
        """Кому этот кошелёк слал SOL (последние 100 транзакций)."""
        out = []
        for tx in await self.h.address_txs(addr):
            for nt in tx.get("nativeTransfers") or []:
                to = nt.get("toUserAccount")
                if nt.get("fromUserAccount") == addr and (nt.get("amount") or 0) > 0 and to and is_wallet(to):
                    out.append(to)
        return out

    async def is_exchange(self, addr):
        hit = cache.get("exch", f"sol:{addr}")
        if hit is not None:
            return hit
        res = await self.h.sig_count(addr) >= config.SOL_EXCHANGE_SIGS
        cache.put("exch", f"sol:{addr}", res)
        return res

    async def investigate(self, r):
        """Сеть создателя и его прошлые запуски (аналог deployer.py для Solana)."""
        dep, out = r.deployer, {"funder": None, "funder_is_exchange": False, "network": {},
                                "past_tokens": [], "history_partial": False}
        if not dep:
            return out
        dep_txs, dep_trace = await asyncio.gather(safe(self.h.address_txs(dep)), safe(self.trace(dep)))
        dep_txs = dep_txs or []
        out["history_partial"] = len(dep_txs) >= 100
        funder = (dep_trace or {}).get("funder")
        out["funder"] = funder

        # спонсоры создателя (все, кто слал ему SOL, кроме бирж) и их другие подопечные
        senders = [funder] if funder else []
        for tx in dep_txs:
            for nt in tx.get("nativeTransfers") or []:
                f = nt.get("fromUserAccount")
                if nt.get("toUserAccount") == dep and (nt.get("amount") or 0) > 0 and f and f != dep \
                        and is_wallet(f) and f not in senders:
                    senders.append(f)
        senders = senders[:5]
        ex = await asyncio.gather(*(safe(self.is_exchange(f)) for f in senders))
        net, sponsors = {}, []
        for f, is_ex in zip(senders, ex):
            if f == funder:
                out["funder_is_exchange"] = bool(is_ex)
            if not is_ex:
                sponsors.append(f)
                net[f] = "спонсор создателя"
        for f, txs in zip(sponsors, await asyncio.gather(*(safe(self.h.address_txs(f)) for f in sponsors))):
            for tx in txs or []:
                for nt in tx.get("nativeTransfers") or []:
                    to = nt.get("toUserAccount")
                    if nt.get("fromUserAccount") == f and (nt.get("amount") or 0) > 0 and to \
                            and to != dep and is_wallet(to):
                        net.setdefault(to, "получал деньги от того же спонсора")
        for tx in dep_txs:   # кому создатель сам слал SOL
            for nt in tx.get("nativeTransfers") or []:
                to = nt.get("toUserAccount")
                if nt.get("fromUserAccount") == dep and (nt.get("amount") or 0) > 0 and to and is_wallet(to):
                    net.setdefault(to, "получал деньги от создателя")
        net.pop(dep, None)
        net = dict(list(net.items())[:config.NET_MAX])
        out["network"] = net

        # прошлые запуски: транзакции, где кошелёк платил комиссию и минтился новый токен в пул/кривую
        members = list(net)
        member_txs = await asyncio.gather(*(safe(self.h.address_txs(m)) for m in members))
        launches, sold = {}, defaultdict(float)
        for creator, rel, txs in [(dep, "сам создатель", dep_txs)] + \
                [(m, net[m], t or []) for m, t in zip(members, member_txs)]:
            for tx in txs:
                for tt in tx.get("tokenTransfers") or []:
                    mint = tt.get("mint")
                    if not mint or mint == r.token:
                        continue
                    if tt.get("fromUserAccount") == creator and not is_wallet(tt.get("toUserAccount") or ""):
                        sold[(creator, mint)] += float(tt.get("tokenAmount") or 0)
                    minted = not tt.get("fromUserAccount")
                    to = tt.get("toUserAccount") or ""
                    if (tx.get("feePayer") == creator and minted
                            and (tx.get("type") == "CREATE" or (to and not is_wallet(to)))
                            and mint not in launches):
                        launches[mint] = (creator, rel, _ts(tx.get("timestamp")))
        past = sorted(launches.items(), key=lambda x: x[1][2] or datetime.min.replace(tzinfo=timezone.utc),
                      reverse=True)[:config.PAST_MAX]

        async def fate(mint, creator, rel, ts):
            asset, pool = await asyncio.gather(safe(self.h.asset(mint)), safe(self.dex.pool(mint)))
            ti = (asset or {}).get("token_info") or {}
            dec = int(ti.get("decimals") or 0)
            supply_ui = (int(ti.get("supply") or 0) / 10 ** dec) if ti.get("supply") else 0
            sold_pct = 100 * sold[(creator, mint)] / supply_ui if supply_ui else 0
            liq = (pool or {}).get("liquidity_usd") or 0
            age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600 if ts else None
            sym = (((asset or {}).get("content") or {}).get("metadata") or {}).get("symbol") or ti.get("symbol")
            return {"token": mint, "symbol": sym or "?", "creator": creator, "rel": rel,
                    "status": classify_fate(liq, sold_pct, age_h), "liquidity_usd": liq,
                    "creator_sold_pct": sold_pct, "age_days": (age_h or 0) / 24}

        out["past_tokens"] = await asyncio.gather(*(fate(m, *v) for m, v in past))
        return out

    # ---------- основной анализ ----------
    async def analyze(self, mint: str) -> Report:
        r = Report(token=mint, chain="solana", launch_label="в слоте создания (бандл)")
        progress("📦 токен, холдеры, запуск и последние сделки…")

        acc, asset, (accounts, holders_complete), (sigs, sigs_complete), oldest, pool, rc, fy = await asyncio.gather(
            self.h.account_info(mint),
            safe(self.h.asset(mint)),
            self.h.token_accounts(mint),
            self.h.signatures(mint),
            safe(self.h.oldest_txs(mint, config.SOL_LAUNCH_PARSE)),
            safe(self.dex.pool(mint)),
            safe(self.second.rugcheck(mint)),
            safe(self.second.feyn(mint)),
        )
        parsed = ((acc or {}).get("data") or {}).get("parsed") or {}
        if parsed.get("type") != "mint":
            raise ValueError("это не адрес токена Solana (mint) — проверь, что вставил адрес токена, "
                             "а не кошелька или пула")
        info = parsed.get("info") or {}
        r.decimals = int(info.get("decimals") or 0)
        r.supply = int(info.get("supply") or 0)
        meta = (((asset or {}).get("content") or {}).get("metadata") or {})
        r.name, r.symbol = meta.get("name") or "?", meta.get("symbol") or "?"
        r.pool, r.rugcheck, r.feyn = pool, rc, fy
        if r.supply <= 0:
            raise ValueError("supply токена 0 — возможно, всё сожжено или адрес не тот")

        # ---------- «контракт» на Solana ----------
        checks = []
        if info.get("mintAuthority"):
            r.sol_mint_auth = True
            checks.append((BAD, "mint authority НЕ отозван — могут допечатать токены"))
        else:
            checks.append((OK, "mint authority отозван"))
        if info.get("freezeAuthority"):
            r.sol_freeze_auth = True
            checks.append((BAD, "freeze authority НЕ отозван — могут заморозить твой кошелёк, не продашь"))
        else:
            checks.append((OK, "freeze authority отозван"))
        is_2022 = (acc or {}).get("owner") == TOKEN_2022
        for ext in ((info.get("extensions") or []) if is_2022 else []):
            name, st = ext.get("extension"), ext.get("state") or {}
            if name == "permanentDelegate" and st.get("delegate"):
                r.sol_traps.append(("hard", "постоянный делегат — может забрать токены с любого кошелька"))
            elif name == "transferHook" and st.get("programId"):
                r.sol_traps.append(("mid", "transfer hook — свой код на каждый перевод, может блокировать продажи"))
            elif name == "transferFeeConfig":
                fee = ((st.get("newerTransferFee") or {}).get("transferFeeBasisPoints") or 0)
                if fee:
                    r.sol_traps.append(("fee", f"налог на перевод {fee / 100:.1f}%"))
                elif st.get("transferFeeConfigAuthority"):
                    r.sol_traps.append(("fee", "налог на перевод сейчас 0, но его могут поднять"))
            elif name == "defaultAccountState" and st.get("accountState") == "frozen":
                r.sol_traps.append(("hard", "новые кошельки заморожены по умолчанию"))
            elif name == "nonTransferable":
                r.sol_traps.append(("hard", "токен нельзя переводить/продавать"))
            elif name in ("pausableConfig", "pausable"):
                r.sol_traps.append(("hard", "торговлю могут поставить на паузу"))
        for kind, text in r.sol_traps:
            checks.append((BAD if kind != "fee" else WARN, text))
        if is_2022 and not r.sol_traps:
            checks.append((OK, "Token-2022, опасных расширений нет"))
        r.sol_checks = checks
        if mint.endswith("pump"):
            r.launchpad = "pump.fun"
        elif mint.endswith("bonk"):
            r.launchpad = "LetsBonk"

        # ---------- холдеры: токен-аккаунты -> владельцы ----------
        hb, infra = defaultdict(int), set()
        for ta in accounts:
            owner, amount = ta.get("owner"), int(ta.get("amount") or 0)
            if owner and amount > 0:
                hb[owner] += amount
        for owner in hb:
            if not is_wallet(owner):
                infra.add(owner)
        r.holders_count = len(hb)

        # ---------- история: запуск + свежие сделки ----------
        # Запуск берём напрямую (самые старые транзакции), свежие — с конца истории.
        oldest = [t for t in (oldest or []) if isinstance(t, dict) and t.get("signature")]
        launch_visible = sigs_complete or bool(oldest)
        sigs = [x for x in sigs if not x.get("err")]
        n = len(sigs)
        if sigs_complete and n <= config.SOL_MAX_PARSE:
            to_parse, complete = sigs, True
        else:
            to_parse, complete = sigs[: config.SOL_RECENT_PARSE], False
            r.notes.append(f"у токена {n}{'+' if not sigs_complete else ''} успешных транзакций — "
                           f"разобрал запуск (первые {len(oldest) or config.SOL_LAUNCH_PARSE}) и "
                           f"последние {len(to_parse)}, середину пропустил")
        # порядок внутри слота: старые транзакции из oldest идут первыми
        order = {t["signature"]: i for i, t in enumerate(oldest)}
        base = len(order) + 1
        for i, x in enumerate(reversed(sigs)):
            order.setdefault(x["signature"], base + i)
        have = {t["signature"] for t in oldest}
        need = [x["signature"] for x in to_parse if x["signature"] not in have]
        progress(f"📜 разбираю {len(need) + len(oldest)} транзакций…")
        txs = oldest + await self.h.parse(need)
        txs = [t for t in txs if not t.get("transactionError")]
        txs.sort(key=lambda t: (t.get("slot") or 0, order.get(t.get("signature"), 0)))

        trs, extra_edges, payer_linked, paid_buys = [], [], set(), []
        launch_tx = None
        for tx in txs:
            tts = [tt for tt in tx.get("tokenTransfers") or [] if tt.get("mint") == mint]
            if launch_tx is None and launch_visible and any(not tt.get("fromUserAccount") for tt in tts):
                launch_tx = tx
            rank = order.get(tx.get("signature"), 0)
            for i, tt in enumerate(tts):
                frm = tt.get("fromUserAccount") or MINT_SRC
                to = tt.get("toUserAccount") or BURN_DST
                frm_c = frm not in BURN and not is_wallet(frm)
                to_c = to not in BURN and not is_wallet(to)
                if frm_c:
                    infra.add(frm)
                if to_c:
                    infra.add(to)
                trs.append(Tr(frm, to, _raw(tt.get("tokenAmount"), r.decimals), _ts(tx.get("timestamp")),
                              int(tx.get("slot") or 0), rank * 100 + i, frm_c, to_c, tx=tx.get("signature") or ""))
                payer = tx.get("feePayer")
                if frm_c and not to_c and to not in BURN and payer and payer != to and is_wallet(payer):
                    paid_buys.append((to, payer, _ts(tx.get("timestamp"))))

        launch_ts = launch_block = None
        if launch_tx is None and launch_visible and txs:
            launch_tx = txs[0]
        if launch_tx:
            r.deployer = launch_tx.get("feePayer")
            launch_ts, launch_block = _ts(launch_tx.get("timestamp")), int(launch_tx.get("slot") or 0)
            src = (launch_tx.get("source") or "").upper()
            if src == "PUMP_FUN":
                r.launchpad = "pump.fun"
            elif src and src not in ("UNKNOWN", "SYSTEM_PROGRAM", "SOLANA_PROGRAM_LIBRARY") and not r.launchpad:
                r.launchpad = src.replace("_", " ").title()
        else:
            r.notes.append("не дошёл до создания токена — снайперов и бандл на запуске не проверял")

        # ---------- ликвидность: кто и когда сделал пул ----------
        supply_ui = r.supply / 10 ** r.decimals
        events = liquidity_events(txs, mint, supply_ui)
        manual = launched_manually(launch_tx, mint)
        if manual and launch_ts:
            # создатель выпустил сапплай себе и сам сделал пул: торговля (и бандл) начинается
            # с создания ПУЛА, а не с создания токена
            ev = first_pool_event(events)
            if ev and ev["slot"] >= (launch_block or 0) and ev["ts"]:
                launch_ts, launch_block = ev["ts"], ev["slot"]
                r.launch_label = "в слоте создания пула (бандл)"
            else:
                first = next((t for t in trs if t.frm_c and not t.to_c and t.to not in BURN and t.ts), None)
                if first:
                    launch_ts, launch_block = first.ts, first.block
                    r.launch_label = "в слоте первой покупки (бандл)"

        # «Чужая комиссия» = признак бандла ТОЛЬКО на запуске. Позже это обычно торговые
        # боты/терминалы, которые платят комиссию за своих пользователей.
        if launch_ts:
            late = defaultdict(set)
            for to, payer, ts in paid_buys:
                if ts and (ts - launch_ts).total_seconds() > config.SNIPE_WINDOW_S:
                    late[payer].add(to)
            services = {p for p, users in late.items() if len(users) >= config.SOL_PAYER_SERVICE}
            for to, payer, ts in paid_buys:
                if (ts and (ts - launch_ts).total_seconds() <= config.SNIPE_WINDOW_S
                        and payer not in services):
                    extra_edges.append((to, payer, "на запуске комиссию за покупку платил другой кошелёк"))
                    payer_linked.add(to)
        r.feepayer_links = len(payer_linked)

        # ---------- куда бандл/снайперы дели токены: их личная история по этому токену ----------
        # (середина истории токена может быть пропущена — а раздача бандла обычно именно там)
        if launch_ts:
            first_buy = {}
            for t in trs:
                if t.frm_c and not t.to_c and t.to not in BURN and t.to not in first_buy:
                    first_buy[t.to] = t
            early = [a for a, t in first_buy.items()
                     if t.ts and (t.ts - launch_ts).total_seconds() <= config.SNIPE_WINDOW_S]
            early = sorted(early, key=lambda a: -first_buy[a].value)[: config.LAUNCH_WALLETS_TRACE]
            if r.deployer and r.deployer not in early:
                early.append(r.deployer)
            if early:
                progress(f"🔍 смотрю, куда {len(early)} кошельков запуска дели токены…")
                have = {t.get("signature") for t in txs}
                hist = await asyncio.gather(*(safe(self.h.wallet_mint_txs(a, mint)) for a in early))
                extra = {}
                for lst in hist:
                    for tx in lst or []:
                        if tx.get("signature") not in have and not tx.get("transactionError"):
                            extra[tx["signature"]] = tx
                events += liquidity_events(list(extra.values()), mint, supply_ui)   # изъятие часто в середине
                for k, tx in enumerate(sorted(extra.values(), key=lambda t: t.get("slot") or 0)):
                    for i, tt in enumerate([x for x in tx.get("tokenTransfers") or [] if x.get("mint") == mint]):
                        frm = tt.get("fromUserAccount") or MINT_SRC
                        to = tt.get("toUserAccount") or BURN_DST
                        frm_c = frm not in BURN and not is_wallet(frm)
                        to_c = to not in BURN and not is_wallet(to)
                        if frm_c:
                            infra.add(frm)
                        if to_c:
                            infra.add(to)
                        trs.append(Tr(frm, to, _raw(tt.get("tokenAmount"), r.decimals), _ts(tx.get("timestamp")),
                                      int(tx.get("slot") or 0), 10 ** 9 + k * 100 + i, frm_c, to_c,
                                      tx=tx.get("signature") or ""))

        # добавление/изъятие ликвидности — не покупки и не продажи
        lp_pairs = {(e["sig"], e["wallet"]) for e in events if e["kind"] in ("add", "remove")}
        for t in trs:
            if (t.frm_c or t.to_c) and ((t.tx, t.frm) in lp_pairs or (t.tx, t.to) in lp_pairs):
                t.lp = True
        progress("💧 проверяю, кто держит ликвидность…")
        await lp_status(r, self.h, events, manual, launch_seen=launch_tx is not None)
        cross_check_rugcheck(r, manual)
        r.manual_launch = manual

        return await run_core(
            r, self, hb=dict(hb), holders_complete=holders_complete, infra=infra, burn=BURN,
            trs=trs, complete=complete, launch_ts=launch_ts, launch_block=launch_block,
            launch_mode="blocks", launch_window=config.SOL_BUNDLE_SLOTS, extra_edges=extra_edges,
            same_block_scope="launch", max_trace=config.SOL_MAX_TRACE)
