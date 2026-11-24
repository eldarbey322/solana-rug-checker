"""Общее ядро анализа — одно для Robinhood Chain и Solana.

Адаптер сети (evm.py / solana.py) достаёт данные и приводит их к общему виду:
  hb      — {кошелёк: баланс} холдеров
  infra   — адреса пулов/кривых/программ (это не холдеры)
  trs     — список Tr (переводы токена), from/to — кошельки-владельцы
и даёт объект chain с методами trace(addr), is_exchange(addr), investigate(r).
Дальше всё считается здесь одинаково.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import config


import time as _time

_t0 = [0.0]


def set_progress(cb):
    """cb(текст) — печатать этапы проверки (консоль). None — молча."""
    _progress_cb_holder[0] = cb
    _t0[0] = _time.time()


_progress_cb_holder = [None]


def progress(msg):
    cb = _progress_cb_holder[0]
    if cb:
        cb(f"[{_time.time() - _t0[0]:4.0f}с] {msg}")


def short(a):
    return f"{a[:6]}…{a[-4:]}" if a else "?"


def trace_to_cache(t):
    if not t:
        return None
    d = dict(t)
    d["first_ts"] = t["first_ts"].isoformat() if t.get("first_ts") else None
    return d


def trace_from_cache(d):
    if not d:
        return None
    d = dict(d)
    d["first_ts"] = datetime.fromisoformat(d["first_ts"]) if d.get("first_ts") else None
    return d


async def safe(coro):
    try:
        return await coro
    except Exception:
        return None


class UF:
    def __init__(self):
        self.p = {}

    def find(self, a):
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


@dataclass
class Tr:
    frm: str
    to: str
    value: int
    ts: datetime | None
    block: int          # блок (EVM) или слот (Solana)
    log: int
    frm_c: bool         # отправитель — пул/кривая/программа
    to_c: bool
    tx: str = ""
    lp: bool = False    # добавление/изъятие ликвидности — это не сделка


@dataclass
class W:
    bought: int = 0
    sold: int = 0
    sent: int = 0
    received: int = 0
    first_buy_ts: datetime | None = None
    first_buy_block: int | None = None
    sent_to: dict = field(default_factory=lambda: defaultdict(int))   # кому и сколько отправил

    @property
    def net(self):
        return self.bought - self.sold - self.sent + self.received


@dataclass
class Report:
    token: str
    chain: str = "robinhood"            # robinhood | solana
    name: str = "?"
    symbol: str = "?"
    decimals: int = 18
    supply: int = 0
    holders_count: int = 0
    # контракт (EVM)
    verified: bool | None = None
    is_proxy: bool = False
    danger_funcs: list = field(default_factory=list)
    owner: str | None = None
    owner_renounced: bool | None = None
    # контракт (Solana): список (значок, текст) и флаги
    sol_checks: list = field(default_factory=list)
    sol_mint_auth: bool = False
    sol_freeze_auth: bool = False
    sol_traps: list = field(default_factory=list)   # опасные расширения Token-2022
    launchpad: str | None = None                    # pump.fun / Pons / ...
    # второе мнение (Solana)
    rugcheck: dict | None = None
    feyn: dict | None = None
    # создатель
    deployer: str | None = None
    factory: str | None = None
    deployer_pct: float = 0.0
    deployer_launches: int | None = None
    deployer_history_partial: bool = False
    dep_funder: str | None = None
    dep_funder_exchange: bool = False
    dep_network: dict = field(default_factory=dict)
    dep_network_holders: int = 0
    dep_network_hold_pct: float = 0.0
    past_tokens: list = field(default_factory=list)
    # пул
    pool: dict | None = None
    # ликвидность (кто её добавил и может ли забрать)
    lp_status: str | None = None     # launchpad|burned|locked|held|held_unknown|removed|unknown
    lp_provider: str | None = None
    lp_provider_is_dev: bool = False
    lp_removed_pct: float = 0.0
    lp_holder: str | None = None     # где сейчас NFT-позиция
    lp_source: str | None = None     # pool = по событиям пула (точно), transfers = по переводам (запасной)
    lp_eoa_share: float = 0.0        # % текущей ликвидности, которую держат кошельки
    lp_positions: int = 0
    lp_kind: str | None = None       # position = позиция без LP-токена (DLMM и т.п.)
    lp_note: str | None = None       # пояснение (например, данные RugCheck)
    manual_launch: bool = False      # Solana: создатель выпустил сапплай себе и сам сделал пул
    # данные
    coverage_complete: bool = False
    launch_known: bool = False
    transfers_count: int = 0
    launch_ts: datetime | None = None
    launch_label: str = ""
    burned_pct: float = 0.0
    infra_pct: float = 0.0
    top10_eoa_pct: float = 0.0
    # снайперы
    snipers_count: int = 0
    snipers_bought_pct: float = 0.0
    snipers_sold_pct: float = 0.0
    snipers_hold_pct: float = 0.0
    snipers_moved_pct: float = 0.0
    snipers_exit_ratio: float | None = None
    taxwin_count: int = 0           # покупатели в окне запуска (налог Pons / слот создания)
    taxwin_bought_pct: float = 0.0
    taxwin_hold_pct: float = 0.0
    feepayer_links: int = 0         # Solana: кошельков, за которых комиссию платил другой
    launch_exit_verified: bool = True   # видно ли, что бандл/снайперы реально продали
    launch_sold_pct: float = 0.0        # сколько сапплая бандл реально продал
    launch_moved_pct: float = 0.0       # сколько сапплая бандл переложил на другие кошельки
    launch_moved_wallets: int = 0       # на сколько кошельков
    launch_moved_hold_pct: float = 0.0  # сколько из этого лежит у получателей сейчас
    # зрелость: живёт ли токен своей жизнью
    age_h: float | None = None
    organic_buyers: int = 0             # покупали сами: не связанные / не свежие / не с раздачи / не бандл
    organic_vol_share: float | None = None  # доля текущего объёма от таких кошельков
    mature: bool = False
    dirty_launch: bool = False
    launch_accounted: bool = False      # видно, куда делся сапплай бандла (продан / разложен)
    launch_story: list = field(default_factory=list)   # как запускали (история)
    now_threats: list = field(default_factory=list)    # что опасно прямо сейчас
    # накрутка холдеров
    airdrop_senders: int = 0
    airdrop_recipients: int = 0
    fake_holders: int = 0           # холдеры, получившие токен раздачей и никогда не покупавшие
    fake_holders_ratio: float = 0.0
    dust_ratio: float = 0.0         # доля холдеров с «пылью»
    # синхронные продажи
    sync_waves: int = 0
    sync_wallets: int = 0
    sync_sell_share: float = 0.0    # доля продаж (по объёму), сделанных волнами
    sync_suspect_ratio: float = 0.0 # доля продавцов из волн, которые связанные/свежие
    # пузыри
    clusters: list = field(default_factory=list)
    linked_pct: float = 0.0
    danger_pct: float = 0.0
    fresh_pct: float = 0.0
    fresh_count: int = 0
    fresh_ratio: float = 0.0
    traced_wallets: int = 0
    trace_attempted: int = 0
    tail_sampled: int = 0            # сколько мелких холдеров проверили выборкой
    tail_fresh: int = 0
    tail_linked: int = 0
    tail_hold_pct: float = 0.0       # сколько сапплая у всех холдеров вне топа
    tail_risky_est_pct: float = 0.0  # оценка: доля сапплая у свежих/связанных мелких кошельков
    wash: dict | None = None
    # слив
    drain_pct: float | None = None
    price_drop_pct: float | None = None
    drain_usd: float | None = None
    team_pct: float = 0.0                 # связанные группы + создатель (одна команда)
    team_drain_pct: float | None = None
    team_drop_pct: float | None = None
    team_drain_usd: float | None = None
    snipers_left_pct: float = 0.0         # снайперы и их «переложенные» кошельки вне команды
    impact_1pct: float | None = None      # на сколько упадёт цена от продажи 1% сапплая (тонкость пула)
    # итог
    score: int = 0
    category: tuple = ()
    reasons: list = field(default_factory=list)
    notes: list = field(default_factory=list)


# ---------------- накрутка объёма ----------------
def _pctl(vals, q):
    return vals[min(len(vals) - 1, int(len(vals) * q))]


def wash_stats(trades, last_n=500):
    """Признаки накрутки объёма по последним сделкам. trades: (ts, кошелёк, 'b'/'s', кол-во)."""
    tr = [t for t in trades if t[0] and t[3] > 0][-last_n:]
    if len(tr) < 30:
        return None
    per = defaultdict(lambda: {"b": 0, "s": 0, "bv": 0, "sv": 0, "seq": []})
    for ts, w, side, v in tr:
        p = per[w]
        p[side] += 1
        p[side + "v"] += v
        p["seq"].append(side)
    total = sum(t[3] for t in tr)
    rt = [w for w, p in per.items() if p["b"] >= 2 and p["s"] >= 2 and p["sv"] >= 0.7 * p["bv"]]
    rt_vol = sum(per[w]["bv"] + per[w]["sv"] for w in rt)
    pingpong = sum(1 for p in per.values()
                   if sum(1 for a, b in zip(p["seq"], p["seq"][1:]) if a != b) >= 4)
    spreads, tiny, whale = [], [], []
    for i in range(0, max(1, len(tr) - 39), 20):
        vals = sorted(t[3] for t in tr[i:i + 40])
        if len(vals) < 20:
            continue
        med = _pctl(vals, 0.5)
        spreads.append(_pctl(vals, 0.9) / max(_pctl(vals, 0.1), 1))
        tiny.append(sum(1 for v in vals if v < med / 10) / len(vals))
        whale.append(sum(1 for v in vals if v > med * 10) / len(vals))
    spreads.sort()
    gaps = [(b[0] - a[0]).total_seconds() for a, b in zip(tr, tr[1:])]
    gaps = [g for g in gaps if g > 0]
    cv = None
    if len(gaps) >= 20:
        m = sum(gaps) / len(gaps)
        cv = (sum((g - m) ** 2 for g in gaps) / len(gaps)) ** 0.5 / m if m else None
    return {
        "trades": len(tr),
        "traders": len(per),
        "trades_per_wallet": len(tr) / len(per),
        "roundtrip_wallets": len(rt),
        "roundtrip_vol_pct": 100 * rt_vol / total if total else 0,
        "pingpong_wallets": pingpong,
        "size_spread": spreads[len(spreads) // 2] if spreads else None,
        "tiny_share": sum(tiny) / len(tiny) if tiny else None,
        "whale_share": sum(whale) / len(whale) if whale else None,
        "interval_cv": cv,
    }


def wash_cluster_share(trades, linked, last_n=500):
    tr = [t for t in trades if t[0] and t[3] > 0][-last_n:]
    total = sum(t[3] for t in tr)
    vol = sum(t[3] for t in tr if t[1] in linked)
    return {"cluster_vol_pct": 100 * vol / total if total else 0}


# ---------------- судьба прошлого токена ----------------
DEAD_LIQ_USD = 1000
RUG_SOLD_PCT = 10


def classify_fate(liq_usd, sold_pct, age_h):
    if age_h is not None and age_h < 24:
        return "свежий"
    if (liq_usd or 0) < DEAD_LIQ_USD:
        return "раг" if sold_pct >= RUG_SOLD_PCT else "мёртв"
    return "жив"


# ---------------- общий анализ ----------------
async def run_core(r: Report, chain, *, hb, holders_complete, infra, burn, trs, complete,
                   launch_ts, launch_block, launch_mode, launch_window, extra_edges=(),
                   same_block_scope="snipers", max_trace=None):
    """launch_mode: 'seconds' (окно в секундах) или 'blocks' (окно в слотах/блоках)."""
    pct = lambda amount: 100.0 * amount / r.supply if r.supply else 0.0

    # ---------- холдеры ----------
    r.burned_pct = pct(sum(v for a, v in hb.items() if a in burn))
    r.infra_pct = pct(sum(v for a, v in hb.items() if a in infra and a not in burn))
    eoa = sorted(((a, v) for a, v in hb.items() if a not in infra and a not in burn),
                 key=lambda x: -x[1])
    r.top10_eoa_pct = pct(sum(v for _, v in eoa[:10]))

    # ---------- переводы ----------
    trs = sorted(trs, key=lambda t: (t.block, t.log))
    r.transfers_count = len(trs)
    r.coverage_complete = complete
    r.launch_ts = launch_ts
    r.launch_known = launch_ts is not None

    ws: dict[str, W] = defaultdict(W)
    p2p, trades = [], []
    for t in trs:
        if t.lp:                              # ликвидность — не покупка и не продажа
            continue
        if t.frm in burn:                     # минт
            continue
        if t.to in burn:                      # сжигание
            ws[t.frm].sent += t.value
        elif t.frm_c and not t.to_c:          # покупка
            w = ws[t.to]
            w.bought += t.value
            trades.append((t.ts, t.to, "b", t.value))
            if w.first_buy_ts is None:
                w.first_buy_ts, w.first_buy_block = t.ts, t.block
        elif t.to_c and not t.frm_c:          # продажа
            ws[t.frm].sold += t.value
            trades.append((t.ts, t.frm, "s", t.value))
        elif not t.frm_c and not t.to_c:      # кошелёк -> кошелёк
            ws[t.frm].sent += t.value
            ws[t.frm].sent_to[t.to] += t.value
            ws[t.to].received += t.value
            p2p.append((t.frm, t.to, t.value))

    # перевод меньше этого — «пыль»: её рассылают кому угодно (накрутка холдеров, реклама),
    # поэтому по пыли кошельки НЕ связываем
    meaningful = r.supply * config.MEANINGFUL_TRANSFER

    # ---------- раздача токена (накрутка холдеров) ----------
    fanout = defaultdict(set)
    for a, b, _v in p2p:
        if a not in infra and b not in infra:
            fanout[a].add(b)
    airdrop_src = {a for a, rec in fanout.items() if len(rec) >= config.AIRDROP_MIN_RECIPIENTS}
    recipients = set().union(*(fanout[a] for a in airdrop_src)) if airdrop_src else set()
    r.airdrop_senders, r.airdrop_recipients = len(airdrop_src), len(recipients)
    holders_now = {a for a, v in hb.items() if v > 0 and a not in infra and a not in burn}
    fake = {a for a in recipients if a in holders_now and ws[a].bought == 0}
    real_recipients = {b for a in airdrop_src for b, v in ws[a].sent_to.items() if v >= meaningful}
    r.fake_holders = len(fake)
    r.fake_holders_ratio = len(fake) / len(holders_now) if holders_now else 0.0
    dust_cut = r.supply * 1e-5               # меньше 0.001% сапплая
    r.dust_ratio = (sum(1 for a in holders_now if hb[a] < dust_cut) / len(holders_now)) if holders_now else 0.0

    # ---------- синхронные продажи: волны, где много разных кошельков продают в одном блоке ----------
    sells_by_block = defaultdict(lambda: [set(), 0])
    total_sold = 0
    for t in trs:
        if not t.lp and t.to_c and not t.frm_c and t.frm not in burn and t.to not in burn:
            sells_by_block[t.block][0].add(t.frm)
            sells_by_block[t.block][1] += t.value
            total_sold += t.value
    waves = {blk: v for blk, v in sells_by_block.items() if len(v[0]) >= config.SYNC_MIN_SELLERS}
    wave_wallets = set().union(*(v[0] for v in waves.values())) if waves else set()
    r.sync_waves, r.sync_wallets = len(waves), len(wave_wallets)
    r.sync_sell_share = (sum(v[1] for v in waves.values()) / total_sold) if total_sold else 0.0

    def bal(a):
        if a in hb:
            return hb[a]
        if holders_complete:
            return 0
        if complete and a in ws:
            return max(0, ws[a].net)
        return 0

    if r.deployer:
        r.deployer_pct = pct(bal(r.deployer))

    # ---------- снайперы и окно запуска ----------
    snipers, launchwin = set(), set()
    launch_recipients = set()
    if launch_ts:
        for a, w in ws.items():
            if a in infra or a == r.deployer or not w.first_buy_ts:
                continue
            dt = (w.first_buy_ts - launch_ts).total_seconds()
            if dt <= config.SNIPE_WINDOW_S:
                snipers.add(a)
            if launch_mode == "seconds":
                if dt <= launch_window:
                    launchwin.add(a)
            elif launch_block is not None and w.first_buy_block is not None:
                if w.first_buy_block - launch_block <= launch_window:
                    launchwin.add(a)
    moved, frontier = set(), set(snipers)
    for _ in range(3):
        nxt = set()
        for a in frontier:
            w = ws.get(a)
            for b, v in (w.sent_to.items() if w else ()):
                if v < meaningful:
                    continue
                if b not in snipers and b not in moved and b not in infra and b not in burn:
                    moved.add(b)
                    nxt.add(b)
        frontier = nxt
    if snipers:
        bought = sum(ws[a].bought for a in snipers)
        sold = sum(ws[a].sold for a in snipers)
        hold = sum(bal(a) for a in snipers)
        moved_hold = sum(bal(a) for a in moved)
        r.snipers_count = len(snipers)
        r.snipers_bought_pct = pct(bought)
        r.snipers_sold_pct = pct(sold)
        r.snipers_hold_pct = pct(hold)
        r.snipers_moved_pct = pct(moved_hold)
        if bought:
            if complete:
                r.snipers_exit_ratio = min(1.0, sold / bought)
            else:   # середина истории не видна — считаем по тому, что у них осталось
                r.snipers_exit_ratio = max(0.0, min(1.0, 1 - (hold + moved_hold) / bought))
    if launchwin:
        r.taxwin_count = len(launchwin)
        lw_bought = sum(ws[a].bought for a in launchwin)
        lw_sold = sum(ws[a].sold for a in launchwin)
        r.taxwin_bought_pct = pct(lw_bought)
        r.taxwin_hold_pct = pct(sum(bal(a) for a in launchwin))
        # «вышли» = видим их продажи; если середина истории пропущена — токены могли просто переехать
        r.launch_exit_verified = complete or (lw_bought > 0 and lw_sold >= 0.8 * lw_bought)
        r.launch_sold_pct = pct(lw_sold)
        lw_moved_first = sum(v for a in launchwin for v in [ws[a].sent])
        rec, front = set(), set(launchwin)
        for _ in range(3):
            nxt = set()
            for a in front:
                for b, v in (ws[a].sent_to.items() if a in ws else ()):
                    if v < meaningful:
                        continue
                    if b not in launchwin and b not in rec and b not in infra and b not in burn and b != r.deployer:
                        rec.add(b)
                        nxt.add(b)
            front = nxt
        r.launch_moved_pct = pct(lw_moved_first)
        r.launch_moved_wallets = len(rec)
        r.launch_moved_hold_pct = pct(sum(bal(a) for a in rec))
        launch_recipients = rec

    # ---------- кто кого финансировал ----------
    # текущая торговля (без ажиотажа первых 5 минут)
    if launch_ts:
        cutoff = launch_ts + timedelta(minutes=5)
        wash_trades = [t for t in trades if t[0] and t[0] > cutoff]
    else:
        wash_trades = trades
    vol_by = defaultdict(int)
    for t in [t for t in wash_trades if t[3] > 0][-500:]:
        vol_by[t[1]] += t[3]
    active_traders = [a for a, _ in sorted(vol_by.items(), key=lambda x: -x[1])
                      if a not in infra and a not in burn][: config.TOP_TRADERS_TRACE]

    # кого проверяем: снайперы + топ холдеров + кто делает объём + выборка из хвоста
    # (сапплай часто раздроблен по сотням мелких одноразовых кошельков вне топа)
    # (сапплай часто раздроблен по сотням мелких одноразовых кошельков вне топа)
    limit = max_trace or config.MAX_WALLETS_TO_TRACE
    top_n = config.TOP_TRACE
    tail_pool = [a for a, _ in eoa[top_n:top_n + 2000] if a != r.deployer]
    step = max(1, len(tail_pool) // max(1, config.TAIL_SAMPLE))
    tail = tail_pool[::step][: config.TAIL_SAMPLE]
    traders_first = [a for a in active_traders if a != r.deployer and a not in tail]
    prio = (set(snipers) | moved | {a for a, _ in eoa[:top_n]}) - {r.deployer} - set(tail) - set(traders_first)
    prio = sorted(prio, key=lambda a: (-bal(a), -(ws[a].bought if a in ws else 0)))
    room = max(0, limit - len(tail) - len(traders_first))
    cand = traders_first + prio[:room] + tail
    progress(f"👛 проверяю {len(cand)} кошельков и создателя…")
    done = [0]

    async def tr(a):
        res = await safe(chain.trace(a))
        done[0] += 1
        if done[0] % 20 == 0:
            progress(f"   кошельков проверено {done[0]}/{len(cand)}")
        return res

    # кошельки и расследование создателя — одновременно
    traced_list, inv = await asyncio.gather(
        asyncio.gather(*(tr(a) for a in cand)), safe(chain.investigate(r)))
    inv = inv or {}
    traced = {a: t for a, t in zip(cand, traced_list) if t}
    failed = [a for a in cand if a not in traced]
    if failed:                                   # второй заход медленнее — обычно это лимит API
        progress(f"   {len(failed)} кошельков не ответили — пробую ещё раз помедленнее…")
        await asyncio.sleep(3)
        sem = asyncio.Semaphore(2)

        async def retry(a):
            async with sem:
                return await safe(chain.trace(a))
        for a, t in zip(failed, await asyncio.gather(*(retry(a) for a in failed))):
            if t:
                traced[a] = t
    r.traced_wallets = len(traced)
    r.trace_attempted = len(cand)
    if cand and len(traced) < 0.7 * len(cand):
        r.notes.append(f"проверил только {len(traced)} кошельков из {len(cand)} — остальные запросы не прошли "
                       "(лимит API?). Связи и свежие кошельки могут быть недооценены — проверь ещё раз позже")

    fresh = set()
    for a, t in traced.items():
        ref = (ws[a].first_buy_ts if a in ws else None) or launch_ts
        if t["tx_count"] == 0:                       # сам ничего не делал, только получил токены
            fresh.add(a)
        elif (t["first_ts"] and ref and not t["full_page"] and t["tx_count"] < config.FRESH_MAX_TXS
                and ref - t["first_ts"] <= timedelta(hours=config.FRESH_MAX_AGE_H)):
            fresh.add(a)
    r.fresh_pct = pct(sum(bal(a) for a in fresh))
    # кошелёк, которому только прислали пыль и который сам ничего не покупал, — это накрутка холдеров
    # (видна в строке «Холдеры»), а не угроза: в счётчик свежих его не берём
    dust_only = {a for a in fresh if bal(a) < dust_cut and ws[a].bought == 0}
    r.fresh_count = len(fresh - dust_only)
    r.fresh_ratio = r.fresh_count / max(1, len(traced) - len(dust_only)) if traced else 0.0
    r.wash = wash_stats(wash_trades)

    # ---------- расследование создателя (уже посчитано параллельно) ----------
    r.dep_funder = inv.get("funder")
    r.dep_funder_exchange = inv.get("funder_is_exchange", False)
    r.dep_network = inv.get("network") or {}
    r.past_tokens = inv.get("past_tokens") or []
    r.deployer_history_partial = inv.get("history_partial", False)
    r.deployer_launches = sum(1 for t in r.past_tokens if t["creator"] == r.deployer)
    net_holding = [a for a in r.dep_network if bal(a) > 0 and a not in infra]
    r.dep_network_holders = len(net_holding)
    r.dep_network_hold_pct = pct(sum(bal(a) for a in net_holding))

    # ---------- пузыри ----------
    progress("🫧 связываю кошельки…")
    edges = []
    for a in net_holding:
        edges.append((r.deployer, a, "из сети создателя"))
    groups = defaultdict(list)
    for a, t in traced.items():
        f = t["funder"]
        if not f:
            continue
        if f == r.deployer:
            edges.append((a, f, "получали деньги от создателя"))
        elif f in traced:
            edges.append((a, f, "один финансировал другого"))
        groups[f].append(a)
    shared = {f: m for f, m in groups.items() if len(m) >= 2 and f != r.deployer}
    exch = await asyncio.gather(*(safe(chain.is_exchange(f)) for f in shared))
    hubs = []
    for (f, members), is_ex in zip(shared.items(), exch):
        if is_ex or traced[members[0]].get("funder_tagged"):
            continue
        hubs.append(f)
        for m in members[1:]:
            edges.append((members[0], m, f"общий источник денег {short(f)}"))
    # дёшево расширяем группу: кому ещё спонсор слал деньги (1 запрос на спонсора)
    funded_lists = await asyncio.gather(*(safe(chain.funded(f)) for f in hubs))
    for f, funded in zip(hubs, funded_lists):
        anchor = groups[f][0]
        for a in funded or []:
            if a != anchor and a not in infra and a not in burn and (a in ws or a in hb):
                edges.append((anchor, a, f"общий источник денег {short(f)}"))
    hub_traces = await asyncio.gather(*(safe(chain.trace(f)) for f in hubs))
    for f, t in zip(hubs, hub_traces):
        hf = (t or {}).get("funder")
        if hf and (hf == r.deployer or hf in traced):
            edges.append((groups[f][0], hf, "спонсора кластера профинансировал создатель"
                          if hf == r.deployer else "спонсора кластера профинансировал холдер"))
    for a, b, v in p2p:
        if v < meaningful and b not in fresh:      # пыль активному кошельку — это не связь
            continue
        edges.append((a, b, "раздача токена с одного кошелька" if a in airdrop_src
                      else "переводили токен друг другу"))
    scope = launchwin if same_block_scope == "launch" else snipers
    by_block = defaultdict(list)
    for a in scope:
        by_block[ws[a].first_buy_block].append(a)
    for members in by_block.values():
        for m in members[1:]:
            edges.append((members[0], m, "купили в одном блоке на запуске"))
    for a, b, reason in extra_edges:
        if a not in infra and b not in infra:
            edges.append((a, b, reason))

    uf = UF()
    for a, b, _ in edges:
        uf.union(a, b)
    why, members_of = defaultdict(set), defaultdict(set)
    for a, b, reason in edges:
        root = uf.find(a)
        why[root].add(reason)
        members_of[root].update((a, b))
    clusters = []
    for root, mem in members_of.items():
        mem = {m for m in mem if m not in infra and m not in burn}
        if len(mem) < 2:
            continue
        clusters.append({
            "size": len(mem), "hold_pct": pct(sum(bal(m) for m in mem)), "members": mem,
            "with_deployer": bool(r.deployer and r.deployer in mem),
            "reasons": sorted(why[root], key=lambda s: (not s.startswith("общий"), s)),
        })
    clusters.sort(key=lambda c: -c["hold_pct"])
    r.clusters = clusters

    linked = set(launch_recipients) if r.launch_moved_pct >= 10 else set()   # бандл разложил сапплай — это одна команда
    for c in clusters:
        if c["size"] >= 3 or c["hold_pct"] >= 1:
            linked |= c["members"]
    if r.deployer:
        linked.add(r.deployer)
    r.linked_pct = pct(sum(bal(a) for a in linked))
    if r.wash:
        r.wash.update(wash_cluster_share(wash_trades, linked))

    # хвост: что за мелкие кошельки держат остаток сапплая
    tail_traced = [a for a in tail if a in traced]
    r.tail_sampled = len(tail_traced)
    r.tail_fresh = sum(1 for a in tail_traced if a in fresh and a not in dust_only)
    r.tail_linked = sum(1 for a in tail_traced if a in linked)
    tail_total = sum(v for _, v in eoa[top_n:])
    r.tail_hold_pct = pct(tail_total)
    samp = sum(bal(a) for a in tail_traced)
    samp_bad = sum(bal(a) for a in tail_traced if a in linked or a in fresh)
    if samp and r.tail_sampled >= 10:
        r.tail_risky_est_pct = pct(tail_total * samp_bad / samp)

    if wave_wallets:
        sus = {a for a in wave_wallets if a in linked or a in fresh or a in real_recipients}
        checked = {a for a in wave_wallets if a in traced or a in linked or a in real_recipients}
        r.sync_suspect_ratio = len(sus) / len(checked) if checked else 0.0

    # ---------- зрелость: сколько настоящих покупателей и какую часть торговли делают они ----------
    if launch_ts:
        last_ts = max((t.ts for t in trs if t.ts), default=None)
        if last_ts:
            r.age_h = (last_ts - launch_ts).total_seconds() / 3600
    suspicious = linked | fresh | real_recipients | fake | launchwin | launch_recipients | {r.deployer}
    r.organic_buyers = sum(1 for a, w in ws.items()
                           if w.bought > 0 and a not in suspicious and a not in infra and a not in burn)
    recent = [t for t in wash_trades if t[3] > 0][-500:]
    vol_total = sum(t[3] for t in recent)
    if vol_total and len(recent) >= 30:
        r.organic_vol_share = sum(t[3] for t in recent if t[1] not in suspicious) / vol_total

    danger = linked | snipers | moved
    danger_amt = sum(bal(a) for a in danger if a not in infra and a not in burn)
    r.danger_pct = pct(danger_amt)

    # ---------- риск слива ----------
    # команда = связанные группы + создатель (продают синхронно); снайперы считаются отдельно —
    # на pump.fun это часто независимые боты
    team = {a for a in linked if a not in infra and a not in burn}
    team_amt = sum(bal(a) for a in team)
    r.team_pct = pct(team_amt)
    r.snipers_left_pct = pct(sum(bal(a) for a in (snipers | moved) - team if a not in infra and a not in burn))
    pool = r.pool
    x = None
    if pool:
        x = pool.get("base_amount")
        if not x and pool.get("liquidity_usd") and pool.get("price_usd"):
            x = pool["liquidity_usd"] / 2 / pool["price_usd"]

    def impact(amount):
        """(доля пула, которую вынут %, падение цены %, $) при продаже amount токенов в пул x·y=k."""
        s_ = amount / 10 ** r.decimals
        if not x or s_ <= 0:
            return 0.0, 0.0, 0.0
        return (100 * s_ / (x + s_), 100 * (1 - (x / (x + s_)) ** 2),
                (pool.get("liquidity_usd") or 0) / 2 * s_ / (x + s_))

    if pool and x:
        if danger_amt:
            r.drain_pct, r.price_drop_pct, r.drain_usd = impact(danger_amt)
        r.team_drain_pct, r.team_drop_pct, r.team_drain_usd = impact(team_amt)
        r.impact_1pct = impact(r.supply // 100)[1]
    elif not pool:
        r.notes.append("пул не найден в DexScreener — риск слива по пулу не посчитан")
    return r
