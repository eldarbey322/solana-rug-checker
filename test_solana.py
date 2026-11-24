"""Проверка Solana-логики без сети: фейковый Helius.
Запуск: python tests/test_solana.py
"""
import asyncio
import os
os.environ["RUGBOT_NO_CACHE"] = "1"   # тесты не должны брать данные из кэша прошлых запусков
import os
import random
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from report import render  # noqa: E402
from scoring import score  # noqa: E402
from sol_sources import b58encode, is_wallet  # noqa: E402
from solana import SolanaChecker  # noqa: E402

DEC = 6
SUPPLY = 1_000_000_000 * 10 ** DEC
T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
_rng = random.Random(42)


def new_addr(wallet=True):
    while True:
        a = b58encode(bytes(_rng.getrandbits(8) for _ in range(32)))
        if len(a) >= 43 and is_wallet(a) == wallet:
            return a


class FakeSol:
    def __init__(self):
        self.mint = new_addr(False)
        self.curve = new_addr(False)            # кривая/пул (PDA)
        self.mint_auth = None
        self.freeze_auth = None
        self.txs = []                           # транзакции с этим токеном
        self.wallet_txs = defaultdict(list)     # история кошельков
        self.sigcount = {}
        self.bal = defaultdict(int)
        self.assets = {}                        # прошлые токены: mint -> symbol
        self.slot = 300_000_000
        self.n = 0
        self.sig_window = None      # если задано — getSignatures видит только N последних (длинная история)
        self.lp_holders = {}        # LP-mint -> [(владелец, кол-во)]
        self.dex_id = "pumpswap"
        self.liq_usd = 60000.0
        self.rc = None              # ответ RugCheck (None = недоступен)

    def _sig(self):
        self.n += 1
        return f"sig{self.n}"

    def fund(self, frm, to, when, sol=1):
        tx = {"signature": self._sig(), "timestamp": int(when.timestamp()), "slot": self.slot,
              "feePayer": frm, "type": "TRANSFER",
              "nativeTransfers": [{"fromUserAccount": frm, "toUserAccount": to, "amount": sol * 10 ** 9}],
              "tokenTransfers": []}
        self.wallet_txs[to].insert(0, tx)
        self.wallet_txs[frm].insert(0, tx)

    def trade(self, frm, to, amount, when, slot, payer=None, kind="SWAP", source="PUMP_FUN"):
        tx = {"signature": self._sig(), "timestamp": int(when.timestamp()), "slot": slot,
              "feePayer": payer or (to if frm == self.curve else frm), "type": kind, "source": source,
              "nativeTransfers": [],
              "tokenTransfers": [{"fromUserAccount": frm if frm != "MINT" else "",
                                  "toUserAccount": to, "tokenAmount": amount / 10 ** DEC,
                                  "mint": self.mint}]}
        self.txs.append(tx)
        if frm != "MINT":
            self.bal[frm] -= amount
        self.bal[to] += amount
        return tx

    def raw(self, transfers, when, slot, payer, natives=(), kind="UNKNOWN", source="UNKNOWN"):
        """Произвольная транзакция. transfers: (from, to, ui_amount, mint); from="" = выпуск."""
        tx = {"signature": self._sig(), "timestamp": int(when.timestamp()), "slot": slot, "feePayer": payer,
              "type": kind, "source": source,
              "nativeTransfers": [{"fromUserAccount": f, "toUserAccount": t, "amount": int(a * 10 ** 9)}
                                  for f, t, a in natives],
              "tokenTransfers": [{"fromUserAccount": f, "toUserAccount": t, "tokenAmount": a, "mint": m}
                                 for f, t, a, m in transfers]}
        self.txs.append(tx)
        for f, t, a, m in transfers:
            if m == self.mint:
                raw_amt = int(a * 10 ** DEC)
                if f:
                    self.bal[f] -= raw_amt
                if t:
                    self.bal[t] += raw_amt
        return tx

    def old_launch(self, creator, symbol, when, sold_share):
        m = new_addr(False)
        self.assets[m] = symbol
        tx = {"signature": self._sig(), "timestamp": int(when.timestamp()), "slot": 1, "feePayer": creator,
              "type": "CREATE", "source": "PUMP_FUN", "nativeTransfers": [],
              "tokenTransfers": [{"fromUserAccount": "", "toUserAccount": new_addr(False),
                                  "tokenAmount": 1e9, "mint": m}]}
        self.wallet_txs[creator].insert(0, tx)
        if sold_share:
            sell = {"signature": self._sig(), "timestamp": int((when + timedelta(hours=2)).timestamp()),
                    "slot": 2, "feePayer": creator, "type": "SWAP", "nativeTransfers": [],
                    "tokenTransfers": [{"fromUserAccount": creator, "toUserAccount": new_addr(False),
                                        "tokenAmount": 1e9 * sold_share, "mint": m}]}
            self.wallet_txs[creator].insert(0, sell)


class FakeHelius:
    def __init__(self, c: FakeSol):
        self.c = c

    async def account_info(self, a):
        return {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                "data": {"parsed": {"type": "mint", "info": {
                    "decimals": DEC, "supply": str(SUPPLY),
                    "mintAuthority": self.c.mint_auth, "freezeAuthority": self.c.freeze_auth}}}}

    async def asset(self, m):
        if m == self.c.mint:
            return {"content": {"metadata": {"name": "Sol Test", "symbol": "SOLT"}}}
        return {"content": {"metadata": {"symbol": self.c.assets.get(m, "?")}},
                "token_info": {"supply": 10 ** 18, "decimals": 9}}

    async def token_accounts(self, m, max_pages=None):
        if m != self.c.mint:
            return [{"owner": o, "amount": v} for o, v in self.c.lp_holders.get(m, []) if v > 0], True
        return [{"owner": o, "amount": v} for o, v in self.c.bal.items() if v > 0], True

    async def signatures(self, a):
        sigs = [{"signature": t["signature"], "err": None} for t in reversed(self.c.txs)]
        if self.c.sig_window:
            return sigs[: self.c.sig_window], False
        return sigs, True

    async def oldest_txs(self, a, total=400):
        return sorted(self.c.txs, key=lambda t: t["slot"])[:total]

    async def parse(self, sigs):
        by = {t["signature"]: t for t in self.c.txs}
        return [by[s] for s in sigs if s in by]

    async def address_txs(self, a, limit=100, token_accounts=None, before=None):
        own = list(self.c.wallet_txs.get(a, []))
        if token_accounts:   # как у Helius: плюс поступления/списания на токен-аккаунтах
            mine = {id(t) for t in own}
            own += [tx for tx in self.c.txs if id(tx) not in mine and any(
                a in (tt.get("fromUserAccount"), tt.get("toUserAccount")) for tt in tx.get("tokenTransfers") or [])]
            own.sort(key=lambda t: -(t.get("slot") or 0))
        return own[:limit]

    async def wallet_mint_txs(self, w, mint, pages=3):
        return [tx for tx in sorted(self.c.txs, key=lambda t: -(t.get("slot") or 0))
                if any(w in (tt.get("fromUserAccount"), tt.get("toUserAccount")) and tt.get("mint") == mint
                       for tt in tx.get("tokenTransfers") or [])][: pages * 100]

    async def sig_count(self, a):
        return self.c.sigcount.get(a, len(self.c.wallet_txs.get(a, [])))


class FakeDex:
    def __init__(self, c):
        self.c = c

    async def pool(self, m):
        if m != self.c.mint:
            return None
        base = max(self.c.bal[self.c.curve], 1) / 10 ** DEC
        return {"dex": self.c.dex_id, "liquidity_usd": self.c.liq_usd, "base_amount": base,
                "price_usd": self.c.liq_usd / 2 / base}


class FakeSecond:
    def __init__(self, c=None):
        self.c = c

    async def rugcheck(self, m):
        return self.c.rc if self.c else None

    async def feyn(self, m):
        return None


def add_organic(c, n_wallets=120, n_trades=350, seed=1, start=None):
    rnd = random.Random(seed)
    cex = new_addr()
    c.sigcount[cex] = 1000
    wallets = []
    for i in range(n_wallets):
        w = new_addr()
        wallets.append(w)
        c.fund(cex, w, T0 - timedelta(days=60 + i))
        for k in range(20):   # старые активные кошельки
            c.fund(new_addr(), w, T0 - timedelta(days=50 - k), sol=0)
    held = defaultdict(int)
    when, slot = start or T0 + timedelta(minutes=3), c.slot + 500
    for _ in range(n_trades):
        when += timedelta(seconds=rnd.expovariate(1 / 40) * (5 if rnd.random() < .1 else 1))
        slot += rnd.randint(5, 200)
        w = rnd.choice(wallets)
        amt = int(rnd.lognormvariate(0, 1.6) * 200_000) * 10 ** DEC
        if held[w] > 0 and rnd.random() < 0.35:
            amt = min(amt, held[w])
            c.trade(w, c.curve, amt, when, slot)
            held[w] -= amt
        else:
            c.trade(c.curve, w, amt, when, slot)
            held[w] += amt


def launch(c, dev):
    c.trade("MINT", c.curve, SUPPLY, T0, c.slot, payer=dev, kind="CREATE")
    c.trade(c.curve, dev, 20_000_000 * 10 ** DEC, T0, c.slot, payer=dev, kind="CREATE")


def scam():
    c = FakeSol()
    dev, sponsor, brother = new_addr(), new_addr(), new_addr()
    c.fund(sponsor, dev, T0 - timedelta(days=2), 30)
    c.fund(sponsor, brother, T0 - timedelta(days=30), 30)
    c.old_launch(brother, "RUGX", T0 - timedelta(days=20), 0.4)
    c.old_launch(brother, "RUGY", T0 - timedelta(days=10), 0.3)
    launch(c, dev)
    hub, master = new_addr(), new_addr()
    c.fund(dev, hub, T0 - timedelta(hours=3), 50)
    bundle = [new_addr() for _ in range(25)]
    for i, w in enumerate(bundle):                                  # Jito-бандл в слоте создания
        c.fund(hub, w, T0 - timedelta(minutes=40))
        c.trade(c.curve, w, 30_000_000 * 10 ** DEC, T0, c.slot, payer=master if i < 12 else w)
    for i, w in enumerate(bundle[:8]):                              # часть «вышла» на новые кошельки
        c.trade(w, new_addr(), 30_000_000 * 10 ** DEC, T0 + timedelta(minutes=20 + i), c.slot + 3000 + i,
                kind="TRANSFER", source="SYSTEM_PROGRAM")
    add_organic(c, n_wallets=60, n_trades=150, seed=5)
    return c


def clean(seed=2):
    c = FakeSol()
    dev = new_addr()
    c.fund(new_addr(), dev, T0 - timedelta(days=300))
    launch(c, dev)
    add_organic(c, seed=seed)
    return c


def relayer():
    """Нормальный токен, но торговый терминал платит комиссию за своих пользователей."""
    c = clean(seed=4)
    relay = new_addr()
    users = [new_addr() for _ in range(20)]
    when, slot = T0 + timedelta(hours=2), c.slot + 90_000
    for i, u in enumerate(users):
        c.fund(new_addr(), u, T0 - timedelta(days=90))
        for k in range(20):
            c.fund(new_addr(), u, T0 - timedelta(days=80 - k), sol=0)
        c.trade(c.curve, u, (1_000_000 + 37_000 * i * i) * 10 ** DEC, when + timedelta(minutes=7 * i),
                slot + 50 * i, payer=relay)
    return c


def exited_bundle():
    """Как $SCAT: бандл на запуске забрал 75% и уже всё продал, у создателя куча мёртвых токенов."""
    c = FakeSol()
    dev = new_addr()
    c.fund(new_addr(), dev, T0 - timedelta(days=60))
    for k, (sym, sold) in enumerate([("MEI", .25), ("BBEN", .32), ("A", 0), ("B", 0), ("C", 0), ("D", 0)]):
        c.old_launch(dev, sym, T0 - timedelta(days=30 - k), sold)
    launch(c, dev)
    c.trade(dev, c.curve, 20_000_000 * 10 ** DEC, T0 + timedelta(seconds=30), c.slot + 70)
    hub, master = new_addr(), new_addr()
    bundle = [new_addr() for _ in range(25)]
    for i, w in enumerate(bundle):
        c.fund(hub, w, T0 - timedelta(minutes=40))
        c.trade(c.curve, w, 30_000_000 * 10 ** DEC, T0, c.slot, payer=master if i < 11 else w)
    for i, w in enumerate(bundle):                                  # бандл всё слил
        c.trade(w, c.curve, 30_000_000 * 10 ** DEC, T0 + timedelta(seconds=40 + i), c.slot + 100 + i)
    add_organic(c, n_wallets=150, n_trades=400, seed=9)
    return c


def scat_like(seed=11):
    """Как $SCAT на самом деле: бандл вышел, но группа из 40 свежих кошельков сидит в хвосте
    холдеров мелкими суммами и гоняет объём, а потом синхронно сливает."""
    rnd = random.Random(seed)
    c = exited_bundle()
    hub = new_addr()
    group = [new_addr() for _ in range(40)]
    when, slot = T0 + timedelta(minutes=50), c.slot + 50_000
    for w in group:
        c.fund(hub, w, T0 + timedelta(minutes=40))
    for n in range(240):                      # покупают/продают туда-сюда, держат по чуть-чуть
        when += timedelta(seconds=rnd.uniform(3, 12))
        slot += rnd.randint(3, 30)
        w = group[n % 40]
        amt = int(rnd.uniform(150_000, 400_000)) * 10 ** DEC
        if (n // 40) % 2 == 0:
            c.trade(c.curve, w, amt, when, slot)
        else:
            c.trade(w, c.curve, min(amt, max(c.bal[w] - 1_500_000 * 10 ** DEC, 0)) or 1, when, slot)
    for w in group:                           # каждый держит по чуть-чуть (~0.15%)
        when += timedelta(seconds=rnd.uniform(3, 12))
        slot += 5
        if c.bal[w] < 1_500_000 * 10 ** DEC:
            c.trade(c.curve, w, 1_500_000 * 10 ** DEC - c.bal[w], when, slot)
    return c


def swordbull_like(seed=13):
    """Как $swordbull: бандл 5 кошельков купил 79% на запуске, создатель раздал пыль 250 адресам,
    в середине истории (не видна) бандл переложил токены на 100 свежих кошельков, те сливают волнами."""
    rnd = random.Random(seed)
    c = FakeSol()
    dev = new_addr()
    c.fund(new_addr(), dev, T0 - timedelta(days=90))
    launch(c, dev)
    bundle = [new_addr() for _ in range(5)]
    for w in bundle:
        c.fund(new_addr(), w, T0 - timedelta(minutes=20))
        c.trade(c.curve, w, 158_000_000 * 10 ** DEC, T0, c.slot, payer=w)
    c.bal[dev] += 5_000 * 10 ** DEC * 250
    for i in range(250):                                       # раздача пыли — накрутка холдеров
        c.trade(dev, new_addr(), 5_000 * 10 ** DEC, T0 + timedelta(minutes=2), c.slot + 10 + i,
                kind="TRANSFER", source="SYSTEM_PROGRAM")
    add_organic(c, n_wallets=60, n_trades=150, seed=seed, start=T0 + timedelta(minutes=5))
    # середина: бандл раскладывает токены по 100 свежим кошелькам (это бот не увидит)
    when, slot = T0 + timedelta(hours=1), c.slot + 200_000
    splits = [new_addr() for _ in range(100)]
    hubs = [new_addr() for _ in range(5)]
    for i, w in enumerate(splits):
        c.fund(hubs[i % 5], w, when - timedelta(hours=2))
        c.trade(bundle[i % 5], w, 7_900_000 * 10 ** DEC, when + timedelta(seconds=i), slot + i,
                kind="TRANSFER", source="SYSTEM_PROGRAM")
    add_organic(c, n_wallets=60, n_trades=200, seed=seed + 1, start=when + timedelta(minutes=10))
    # конец: синхронные продажи волнами по 7 кошельков в одном слоте
    when, slot = when + timedelta(hours=3), slot + 500_000
    for wave in range(14):
        for w in splits[wave * 7:(wave + 1) * 7]:
            c.trade(w, c.curve, 7_900_000 * 10 ** DEC, when + timedelta(seconds=wave * 3), slot + wave * 5)
    c.sig_window = 320                                         # видны только последние 320 транзакций
    return c


def swordansem_like(seed=17):
    """Как $swordansem: бандл 6 кошельков купил 82% на запуске; в середине истории (не видна) бандл
    раскидал сапплай по 600 пустым кошелькам (сами ничего не делали — только получили токены).
    Синхронных сливов нет, топ-10 «чистый», конец истории — обычная торговля."""
    c = FakeSol()
    dev = new_addr()
    c.fund(new_addr(), dev, T0 - timedelta(days=90))
    launch(c, dev)
    bundle = [new_addr() for _ in range(6)]
    for w in bundle:
        c.fund(new_addr(), w, T0 - timedelta(minutes=15))
        c.trade(c.curve, w, 136_000_000 * 10 ** DEC, T0, c.slot, payer=w)
    add_organic(c, n_wallets=60, n_trades=150, seed=seed, start=T0 + timedelta(minutes=5))
    when, slot = T0 + timedelta(hours=1), c.slot + 200_000
    for i in range(600):                                   # раздача сапплая бандлом — в середине истории
        c.trade(bundle[i % 6], new_addr(), 1_300_000 * 10 ** DEC, when + timedelta(seconds=i), slot + i,
                kind="TRANSFER", source="SYSTEM_PROGRAM")
    add_organic(c, n_wallets=80, n_trades=320, seed=seed + 1, start=when + timedelta(hours=1))
    c.sig_window = 300
    return c


def dopebat_like(seed=23):
    """Как $DOPEBAT: запуск грязный — бандл 7 кошельков купил 68% и раздал сапплай по 504 кошелькам,
    но получатели давно всё продали. Дальше токен много часов живёт сам: сотни настоящих покупателей,
    флипперы, разные размеры сделок. В конце кто-то рассылает пыль 500 адресам (200 из них — живые трейдеры)."""
    rnd = random.Random(seed)
    c = FakeSol()
    dev, cex0 = new_addr(), new_addr()
    c.sigcount[cex0] = 5000
    c.fund(cex0, dev, T0 - timedelta(days=120))
    launch(c, dev)
    bundle = [new_addr() for _ in range(7)]
    for w in bundle:
        c.fund(new_addr(), w, T0 - timedelta(minutes=30))
        c.trade(c.curve, w, 97_142_857 * 10 ** DEC, T0, c.slot, payer=w)
    # середина: бандл продал 7% и раздал 61% по 504 кошелькам; получатели всё продали за пару часов
    when, slot = T0 + timedelta(minutes=30), c.slot + 5_000
    recips = [new_addr() for _ in range(504)]
    for w in bundle:
        c.trade(w, c.curve, 10_000_000 * 10 ** DEC, when, slot)
    for i, rcp in enumerate(recips):
        c.trade(bundle[i % 7], rcp, 1_210_317 * 10 ** DEC, when + timedelta(seconds=i), slot + 10 + i,
                kind="TRANSFER", source="SYSTEM_PROGRAM")
    for i, rcp in enumerate(recips):
        c.trade(rcp, c.curve, c.bal[rcp], when + timedelta(minutes=40, seconds=8 * i), slot + 2_000 + 3 * i)
    # потом — много часов обычной торговли
    start = T0 + timedelta(hours=2, minutes=30)
    before = set(c.bal)
    add_organic(c, n_wallets=600, n_trades=1200, seed=seed, start=start)
    traders = [a for a in c.bal if a not in before and c.bal[a] > 0]
    # в конце — рассылка пыли (накрутка холдеров): 200 живым трейдерам и 300 пустым адресам
    air = new_addr()
    c.bal[air] += 5_000 * 10 ** DEC * 500
    last = max(t["slot"] for t in c.txs)
    last_ts = max(t["timestamp"] for t in c.txs)
    dust_to = rnd.sample(traders, min(200, len(traders))) + [new_addr() for _ in range(300)]
    for i, d in enumerate(dust_to):
        c.trade(air, d, 5_000 * 10 ** DEC, datetime.fromtimestamp(last_ts, timezone.utc) + timedelta(seconds=i),
                last + 10 + i, kind="TRANSFER", source="SYSTEM_PROGRAM")
    c.sig_window = 1200 + 500 + 20                               # середина (раздача бандла) не видна
    return c


WSOL = "So11111111111111111111111111111111111111112"
INCIN = "1nc1nerator11111111111111111111111111111111"


def manual_pool(lp="dev", removed=False, bundle=True, receipt=True, seed=29):
    """Как $Topgainers: токен НЕ с лаунчпада. Создатель выпустил сапплай себе, сам сделал пул на Raydium
    (90% токенов + 40 SOL), получил LP-токен. В том же слоте — бандл. Потом обычная торговля."""
    c = FakeSol()
    c.dex_id, c.liq_usd = "raydium", 80000.0
    dev = new_addr()
    c.fund(new_addr(), dev, T0 - timedelta(days=200))
    c.raw([("", dev, 1e9, c.mint)], T0 - timedelta(minutes=3), c.slot - 400, dev, kind="CREATE")
    lp_mint = new_addr(False)
    transfers = [(dev, c.curve, 9e8, c.mint), (dev, c.curve, 40.0, WSOL)]
    if receipt:
        transfers.append(("", dev, 1000.0, lp_mint))
    c.raw(transfers, T0, c.slot, dev, natives=[(dev, new_addr(False), 40.0)], kind="CREATE_POOL", source="RAYDIUM")
    holder = {"dev": dev, "burn": INCIN, "locker": new_addr(False), "gone": None}[lp]
    c.lp_holders[lp_mint] = [(holder, 1000)] if holder else []
    if bundle:
        hub = new_addr()
        for i in range(15):
            w = new_addr()
            c.fund(hub, w, T0 - timedelta(minutes=20))
            c.trade(c.curve, w, 40_000_000 * 10 ** DEC, T0, c.slot, payer=w, source="RAYDIUM")
    add_organic(c, n_wallets=150, n_trades=350, seed=seed, start=T0 + timedelta(minutes=2))
    if removed:
        last = max(t["slot"] for t in c.txs) + 5
        when = datetime.fromtimestamp(max(t["timestamp"] for t in c.txs), timezone.utc) + timedelta(minutes=1)
        pool_tok = c.bal[c.curve] / 10 ** DEC
        c.raw([(c.curve, dev, pool_tok * 0.99, c.mint), (c.curve, dev, 371.9, WSOL), (dev, "", 1000.0, lp_mint)],
              when, last, dev, natives=[(new_addr(False), dev, 371.9)], kind="WITHDRAW_LIQUIDITY", source="RAYDIUM")
        c.liq_usd = 0.0
    return c


def pump_plus_small_lp(seed=31):
    """pump.fun-токен вышел на PumpSwap (миграция кривая -> пул), а потом кто-то добавил маленький пул
    на Meteora. Это НЕ должно считаться «пул у кошелька»."""
    c = clean(seed=seed)
    pool2 = new_addr(False)
    c.raw([(c.curve, pool2, 2.07e8, c.mint)], T0 + timedelta(minutes=1), c.slot + 50, new_addr(), kind="MIGRATE")
    lp_guy = new_addr()
    c.fund(new_addr(), lp_guy, T0 - timedelta(days=50))
    c.bal[lp_guy] += 5_000_000 * 10 ** DEC
    lp_mint = new_addr(False)
    c.raw([(lp_guy, new_addr(False), 5e6, c.mint), (lp_guy, new_addr(False), 2.0, WSOL), ("", lp_guy, 10.0, lp_mint)],
          T0 + timedelta(hours=2), c.slot + 90_000, lp_guy, kind="ADD_LIQUIDITY", source="METEORA")
    c.lp_holders[lp_mint] = [(lp_guy, 10)]
    return c


def long_history():
    """Тот же бандл-скам, но история огромная: обычным способом виден только хвост."""
    c = scam()
    c.sig_window = 120
    return c


def freeze():
    c = clean(seed=3)
    c.freeze_auth = new_addr()
    return c


async def run(name, c, expect):
    rep = score(await SolanaChecker(FakeHelius(c), FakeDex(c), FakeSecond(c)).analyze(c.mint))
    print(f"\n===== {name} =====")
    print(re.sub(r"<[^>]+>", "", render(rep)))
    assert rep.category[2] in expect, f"{name}: получили «{rep.category[2]}», ждали {expect}"
    return rep


async def main():
    r1 = await run("SOLANA: БАНДЛ-СКАМ", scam(), {"Точно скам"})
    assert r1.taxwin_count >= 25 and r1.feepayer_links >= 12
    assert any(t["status"] == "раг" for t in r1.past_tokens)
    r2 = await run("SOLANA: ЧИСТЫЙ", clean(), {"Почти нулевая вероятность скама", "Низкий риск"})
    r4 = await run("SOLANA: ТЕРМИНАЛ ПЛАТИТ КОМИССИЮ (не бандл)", relayer(),
                   {"Почти нулевая вероятность скама", "Низкий риск"})
    assert r4.feepayer_links == 0
    r6 = await run("SOLANA: БАНДЛ УЖЕ ВЫШЕЛ + СОЗДАТЕЛЬ-СЕРИЙЩИК (как $SCAT)", exited_bundle(),
                   {"Так себе"})
    r7 = await run("SOLANA: КАК $SCAT НА САМОМ ДЕЛЕ (группа в хвосте гоняет объём)", scat_like(),
                   {"Вероятный скам", "Точно скам"})
    assert r7.tail_sampled >= 10
    r8 = await run("SOLANA: КАК $SWORDBULL (бандл переехал, раздача, синхронный слив)", swordbull_like(),
                   {"Так себе", "Вероятный скам", "Точно скам"})
    assert not r8.launch_exit_verified and r8.fake_holders >= 200 and r8.sync_waves >= 10, \
        (r8.launch_exit_verified, r8.fake_holders, r8.sync_waves)
    r9 = await run("SOLANA: КАК $SWORDANSEM (бандл раскидал сапплай пылью в середине, без сливов)",
                   swordansem_like(), {"Вероятный скам", "Точно скам"})
    assert r9.launch_moved_wallets >= 500, r9.launch_moved_wallets
    rd = await run("SOLANA: КАК $DOPEBAT (грязный запуск, но команда вышла, токен живёт сам)", dopebat_like(),
                   {"Почти нулевая вероятность скама", "Низкий риск", "Так себе"})
    assert rd.mature and not rd.now_threats and rd.launch_story, (rd.mature, rd.now_threats, rd.launch_story)
    m1 = await run("SOLANA: КАК $TOPGAINERS (пул сделал создатель, LP у него, бандл)", manual_pool("dev"),
                   {"Вероятный скам", "Точно скам"})
    assert m1.lp_status == "held" and m1.lp_provider_is_dev and m1.manual_launch, (m1.lp_status, m1.lp_provider_is_dev)
    assert m1.taxwin_count >= 15, m1.taxwin_count        # бандл в слоте создания ПУЛА найден
    m1b = await run("SOLANA: LP у создателя, бандла нет (всё равно нельзя «низкий»)",
                    manual_pool("dev", bundle=False, seed=37), {"Вероятный скам", "Точно скам"})
    assert m1b.lp_status == "held"
    m2 = await run("SOLANA: пул вытащили", manual_pool("dev", removed=True, bundle=False, seed=41), {"Точно скам"})
    assert m2.lp_status == "removed", m2.lp_status
    m3 = await run("SOLANA: создатель сжёг LP", manual_pool("burn", bundle=False, seed=43),
                   {"Почти нулевая вероятность скама", "Низкий риск", "Так себе"})
    assert m3.lp_status == "burned", m3.lp_status
    m4 = await run("SOLANA: LP в локере", manual_pool("locker", bundle=False, seed=47),
                   {"Почти нулевая вероятность скама", "Низкий риск", "Так себе"})
    assert m4.lp_status == "locked", m4.lp_status
    m5 = await run("SOLANA: позиция без LP-токена (DLMM)", manual_pool("dev", bundle=False, receipt=False, seed=53),
                   {"Вероятный скам", "Точно скам"})
    assert m5.lp_status == "held" and m5.lp_kind == "position"
    c6 = manual_pool("dev", bundle=False, seed=59)
    c6.rc = {"score": 1, "rugged": False, "risks": [], "lp_locked_pct": 100.0}
    m6 = await run("SOLANA: LP у кошелька, но RugCheck видит блокировку 100%", c6,
                   {"Почти нулевая вероятность скама", "Низкий риск", "Так себе"})
    assert m6.lp_status == "locked" and m6.lp_note
    m7 = await run("SOLANA: pump.fun + чей-то маленький пул на Meteora (не тревога)", pump_plus_small_lp(),
                   {"Почти нулевая вероятность скама", "Низкий риск"})
    assert m7.lp_status == "launchpad", m7.lp_status
    r5 = await run("SOLANA: ДЛИННАЯ ИСТОРИЯ (запуск напрямую)", long_history(),
                   {"Вероятный скам", "Точно скам"})
    assert r5.launch_known and r5.deployer and r5.taxwin_count >= 25
    r3 = await run("SOLANA: FREEZE AUTHORITY", freeze(), {"Вероятный скам", "Точно скам"})
    print("\n✅ все 18 Solana-сценариев дали ожидаемую категорию")


if __name__ == "__main__":
    asyncio.run(main())
