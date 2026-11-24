"""Solana: кто добавил ликвидность в пул и может ли её забрать.

Работает для любого пула (Raydium, Meteora, PumpSwap, Orca…) без разбора их форматов:
  * добавление ликвидности = в одной транзакции КОШЕЛЁК отдаёт в программу И токен, И SOL/стейбл
    (при покупке/продаже одно уходит, другое приходит);
  * изъятие = кошелёк в одной транзакции получает из программы И токен, И SOL/стейбл;
  * квитанция = LP-токен или NFT позиции, выпущенный этому кошельку в транзакции добавления.
    Где она сейчас — у кошелька (может забрать пул), сожжена или в программе-локере.
  * миграция с лаунчпада = крупный перевод токена из программы в программу (pump.fun -> PumpSwap и т.п.):
    такую ликвидность добавил не человек.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone

from core import safe
from sol_sources import is_wallet

WSOL = "So11111111111111111111111111111111111111112"
QUOTE_MINTS = {
    WSOL,
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
    "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB",   # USD1
}
INCINERATOR = "1nc1nerator11111111111111111111111111111111"
MIN_NATIVE = 50_000_000            # 0.05 SOL: меньше — это аренда аккаунтов, а не ликвидность
MIGRATION_SHARE = 0.10             # перевод ≥10% сапплая между программами = миграция с лаунчпада
LAUNCHPAD_SUFFIXES = {"pump": "pump.fun", "bonk": "LetsBonk"}


def _f(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _ts(unix):
    return datetime.fromtimestamp(unix, timezone.utc) if unix else None


def liquidity_events(txs, mint, supply_ui):
    """-> список событий: {kind: add|remove|migration, wallet, tok, quote, sig, slot, ts, receipts}"""
    events = []
    for tx in txs:
        if tx.get("transactionError"):
            continue
        tts = tx.get("tokenTransfers") or []
        legs = defaultdict(lambda: {"out_tok": 0.0, "in_tok": 0.0, "out_q": 0.0, "in_q": 0.0})
        receipts = defaultdict(dict)
        migration = 0.0
        for tt in tts:
            m, frm, to = tt.get("mint"), tt.get("fromUserAccount") or "", tt.get("toUserAccount") or ""
            amt = _f(tt.get("tokenAmount"))
            if m == mint:
                if frm and is_wallet(frm) and to and not is_wallet(to) and to != INCINERATOR:
                    legs[frm]["out_tok"] += amt
                if to and is_wallet(to) and frm and not is_wallet(frm):
                    legs[to]["in_tok"] += amt
                if (frm and to and not is_wallet(frm) and not is_wallet(to) and to != INCINERATOR
                        and supply_ui and amt >= MIGRATION_SHARE * supply_ui):
                    migration += amt
            elif m in QUOTE_MINTS:
                if frm and is_wallet(frm) and to and not is_wallet(to):
                    legs[frm]["out_q"] += amt
                if to and is_wallet(to) and frm and not is_wallet(frm):
                    legs[to]["in_q"] += amt
            elif m and not frm and to and is_wallet(to) and amt > 0:
                receipts[to][m] = receipts[to].get(m, 0.0) + amt          # выпущен LP-токен / NFT позиции
        for nt in tx.get("nativeTransfers") or []:
            frm, to, lam = nt.get("fromUserAccount") or "", nt.get("toUserAccount") or "", nt.get("amount") or 0
            if lam < MIN_NATIVE:
                continue
            if frm and is_wallet(frm) and to and not is_wallet(to):
                legs[frm]["out_q"] += lam / 1e9
            if to and is_wallet(to) and frm and not is_wallet(frm):
                legs[to]["in_q"] += lam / 1e9
        base = {"sig": tx.get("signature"), "slot": int(tx.get("slot") or 0), "ts": _ts(tx.get("timestamp"))}
        for w, g in legs.items():
            if g["out_tok"] > 0 and g["out_q"] > 0 and g["in_tok"] == 0:
                events.append({**base, "kind": "add", "wallet": w, "tok": g["out_tok"], "quote": g["out_q"],
                               "receipts": dict(receipts.get(w, {}))})
            elif g["in_tok"] > 0 and g["in_q"] > 0 and g["out_tok"] == 0:
                events.append({**base, "kind": "remove", "wallet": w, "tok": g["in_tok"], "quote": g["in_q"],
                               "receipts": {}})
        if migration:
            events.append({**base, "kind": "migration", "wallet": None, "tok": migration, "quote": 0.0,
                           "receipts": {}})
    return events


def launched_manually(launch_tx, mint):
    """Создатель выпустил весь сапплай себе на кошелёк (а не в кривую лаунчпада) — пул он делает сам."""
    if not launch_tx:
        return False
    for tt in launch_tx.get("tokenTransfers") or []:
        if tt.get("mint") == mint and not tt.get("fromUserAccount") and is_wallet(tt.get("toUserAccount") or ""):
            return True
    return False


def first_pool_event(events):
    adds = [e for e in events if e["kind"] in ("add", "migration")]
    return min(adds, key=lambda e: e["slot"]) if adds else None


async def lp_status(r, helius, events, manual, launch_seen):
    """Заполняет r.lp_*: launchpad | burned | locked | held | held_unknown | partial | removed | unknown."""
    r.lp_source = "pool"
    providers = defaultdict(lambda: {"tok_add": 0.0, "q_add": 0.0, "tok_rm": 0.0, "q_rm": 0.0, "receipts": {}})
    migration_tok = 0.0
    for e in events:
        if e["kind"] == "migration":
            migration_tok += e["tok"]
            continue
        p = providers[e["wallet"]]
        if e["kind"] == "add":
            p["tok_add"] += e["tok"]
            p["q_add"] += e["quote"]
            for m, a in e["receipts"].items():
                p["receipts"][m] = p["receipts"].get(m, 0.0) + a
        else:
            p["tok_rm"] += e["tok"]
            p["q_rm"] += e["quote"]
    added = {w: p for w, p in providers.items() if p["tok_add"] > 0}
    liq_usd = (r.pool or {}).get("liquidity_usd")
    on_curve = (r.pool or {}).get("dex") in ("pumpfun", "pump.fun")
    known_pad = r.launchpad or next((v for k, v in LAUNCHPAD_SUFFIXES.items() if r.token.endswith(k)), None)

    if not added:
        if migration_tok or on_curve or (launch_seen and not manual) or known_pad:
            r.lp_status = "launchpad"
        elif not launch_seen:
            r.lp_status = "unknown"
        else:
            r.lp_status = "held_unknown"           # пул сделал создатель, а кто добавил ликвидность — не видно
        return

    main_w, main = max(added.items(), key=lambda kv: kv[1]["tok_add"])
    total = sum(p["tok_add"] for p in added.values()) + migration_tok
    r.lp_provider = main_w
    r.lp_provider_is_dev = bool(r.deployer and main_w == r.deployer)
    r.lp_positions = len(added)
    if migration_tok >= main["tok_add"]:
        r.lp_status = "launchpad"                  # основная ликвидность пришла с лаунчпада
        return

    share = main["tok_add"] / total if total else 1.0
    base_rm = main["q_add"] or 0
    rm_ratio = (main["q_rm"] / base_rm) if base_rm else (main["tok_rm"] / main["tok_add"])
    r.lp_removed_pct = 100 * min(1.0, rm_ratio)
    if rm_ratio >= 0.5:
        r.lp_status = "removed" if (liq_usd is None or liq_usd < 5000) else "partial"
        return

    receipts = sorted(main["receipts"].items(), key=lambda kv: -kv[1])[:3]
    if not receipts:
        # квитанции нет (DLMM и т.п.): позиция записана прямо на кошелёк — он может её закрыть
        r.lp_status = "held"
        r.lp_eoa_share = 100 * share
        r.lp_kind = "position"
        return
    res = await asyncio.gather(*(safe(helius.token_accounts(m, max_pages=1)) for m, _ in receipts))
    by_kind = defaultdict(float)
    top_wallet, top_prog = defaultdict(float), defaultdict(float)
    failed = False
    for out in res:
        if out is None:
            failed = True
            continue
        items = out[0] if isinstance(out, tuple) else out
        for ta in items or []:
            owner, amt = ta.get("owner"), _f(ta.get("amount"))
            if not owner or amt <= 0:
                continue
            if owner == INCINERATOR:
                by_kind["burned"] += amt
            elif is_wallet(owner):
                by_kind["wallet"] += amt
                top_wallet[owner] += amt
            else:
                by_kind["program"] += amt
                top_prog[owner] += amt
    held_total = sum(by_kind.values())
    if failed and not held_total:
        r.lp_status = "held_unknown"
    elif not held_total:
        r.lp_status = "burned"                     # квитанций больше нет ни у кого — сожжены
    elif by_kind["wallet"] >= 0.3 * held_total:
        r.lp_status = "held"
        r.lp_holder = max(top_wallet.items(), key=lambda kv: kv[1])[0]
        r.lp_eoa_share = 100 * share * by_kind["wallet"] / held_total
        if r.lp_holder != main_w:
            r.lp_provider = r.lp_holder
            r.lp_provider_is_dev = bool(r.deployer and r.lp_holder == r.deployer)
    elif by_kind["burned"] >= by_kind["program"]:
        r.lp_status = "burned"
    else:
        r.lp_status = "locked"
        r.lp_holder = max(top_prog.items(), key=lambda kv: kv[1])[0]


def cross_check_rugcheck(r, manual):
    """Сверяем с RugCheck: он видит локеры, которые мы можем не распознать, и наоборот."""
    lp = (r.rugcheck or {}).get("lp_locked_pct")
    if lp is None:
        return
    if r.lp_status in ("held", "held_unknown", "unknown") and lp >= 90:
        r.lp_status = "locked"
        r.lp_holder = None                       # где именно заблокирована — знает RugCheck, не мы
        r.lp_note = f"по данным RugCheck LP заблокирована на {lp:.0f}%"
    elif (manual and r.lp_status in ("launchpad", "burned", "locked") and lp < 30 and r.pool
          and (r.pool.get("dex") or "") not in ("pumpfun", "pump.fun")):
        r.lp_status = "held_unknown"
        r.lp_note = f"RugCheck: LP заблокирована только на {lp:.0f}% — сверь вручную"
