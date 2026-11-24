"""Robinhood (EVM): расследование создателя — его сеть кошельков и судьба прошлых токенов."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import config
from core import classify_fate

ZERO = "0x" + "0" * 40


def _addr(o):
    if isinstance(o, str):
        return o.lower()
    h = (o or {}).get("hash")
    return h.lower() if h else None


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def _tok_addr(item):
    t = item.get("token") or {}
    a = t.get("address_hash") or t.get("address")
    return a.lower() if isinstance(a, str) else None


async def _safe(coro):
    try:
        return await coro
    except Exception:
        return None


async def investigate(bs, dex, trace, deployer, factory, token):
    """Возвращает dict: спонсор, сеть кошельков (адрес -> связь), прошлые токены."""
    out = {"funder": None, "funder_is_exchange": False, "network": {}, "past_tokens": [],
           "history_partial": False}
    if not deployer:
        return out

    dep_txs, dep_trace = await asyncio.gather(_safe(bs.address_txs(deployer)), _safe(trace(deployer)))
    dep_txs = dep_txs or []
    out["history_partial"] = len(dep_txs) >= 50
    net = {}

    # 1. все, кто присылал создателю деньги (спонсоры), и их другие "подопечные"
    funder = (dep_trace or {}).get("funder")
    out["funder"] = funder
    senders = []
    if funder:
        senders.append(funder)
    for t in dep_txs:
        f = _addr(t.get("from"))
        if (_addr(t.get("to")) == deployer and _int(t.get("value")) > 0 and f and f != deployer
                and f not in senders and not (t.get("from") or {}).get("is_contract")):
            senders.append(f)
    senders = senders[:5]
    counts = await asyncio.gather(*(_safe(bs.counters(f)) for f in senders))
    sponsors = []
    for f, cnt in zip(senders, counts):
        exch = _int((cnt or {}).get("transactions_count")) > config.EXCHANGE_TX_THRESHOLD
        if f == funder:
            exch = exch or bool((dep_trace or {}).get("funder_tagged"))
            out["funder_is_exchange"] = bool(exch)
        if not exch:
            sponsors.append(f)
            net[f] = "спонсор создателя"
    sponsor_txs = await asyncio.gather(*(_safe(bs.address_txs(f)) for f in sponsors))
    for f, txs in zip(sponsors, sponsor_txs):
        for t in txs or []:
            to = _addr(t.get("to"))
            if (_addr(t.get("from")) == f and _int(t.get("value")) > 0 and to
                    and to != deployer and not (t.get("to") or {}).get("is_contract")):
                net.setdefault(to, "получал деньги от того же спонсора")

    # 2. кому создатель сам отправлял ETH (часто — свои же кошельки/сборщики)
    for t in dep_txs:
        to = _addr(t.get("to"))
        if (_addr(t.get("from")) == deployer and _int(t.get("value")) > 0 and to
                and not (t.get("to") or {}).get("is_contract")):
            net.setdefault(to, "получал деньги от создателя")

    net.pop(deployer, None)
    net = dict(list(net.items())[:config.NET_MAX])
    out["network"] = net

    # 3. прошлые запуски создателя и его сети
    members = list(net)
    member_txs = await asyncio.gather(*(_safe(bs.address_txs(m)) for m in members))
    launches = []   # (кто создал, связь, tx_hash, время, адрес контракта если сразу известен)
    for creator, rel, txs in [(deployer, "сам создатель", dep_txs)] + \
            [(m, net[m], tx or []) for m, tx in zip(members, member_txs)]:
        for t in txs:
            if _addr(t.get("from")) != creator:
                continue
            created = _addr(t.get("created_contract"))
            via_factory = factory and _addr(t.get("to")) == factory
            if created or via_factory:
                launches.append((creator, rel, t.get("hash"), _ts(t.get("timestamp")), created))
    launches.sort(key=lambda x: x[3] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    async def resolve(l):
        creator, rel, h, ts, created = l
        if created:
            return creator, rel, created, ts
        for it in await _safe(bs.tx_token_transfers(h)) or []:
            if _addr(it.get("from")) == ZERO and _tok_addr(it):
                return creator, rel, _tok_addr(it), ts
        return None

    resolved = await asyncio.gather(*(resolve(l) for l in launches[: config.PAST_MAX * 2]))
    seen, past = set(), []
    for x in resolved:
        if x and x[2] != token and x[2] not in seen:
            seen.add(x[2])
            past.append(x)
    past = past[:config.PAST_MAX]

    async def fate(creator, rel, tok, ts):
        info, pool, trs = await asyncio.gather(
            _safe(bs.token(tok)), _safe(dex.pool(tok)), _safe(bs.address_token_transfers(creator, tok)))
        if not info:
            return None   # создан не токен (обычный контракт)
        supply = _int(info.get("total_supply"))
        sold = sum(_int((it.get("total") or {}).get("value")) for it in trs or []
                   if _addr(it.get("from")) == creator and (it.get("to") or {}).get("is_contract")
                   and _tok_addr(it) in (None, tok))
        sold_pct = 100 * sold / supply if supply else 0
        liq = (pool or {}).get("liquidity_usd") or 0
        age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600 if ts else None
        status = classify_fate(liq, sold_pct, age_h)
        return {"token": tok, "symbol": info.get("symbol") or "?", "creator": creator, "rel": rel,
                "status": status, "liquidity_usd": liq, "creator_sold_pct": sold_pct,
                "age_days": (age_h or 0) / 24}

    fates = await asyncio.gather(*(fate(*p) for p in past))
    out["past_tokens"] = [f for f in fates if f]
    return out
