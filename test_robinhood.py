"""Проверка логики без сети: фейковый Blockscout с двумя сценариями.
Запуск: python tests/test_scenarios.py
"""
import asyncio
import os
os.environ["RUGBOT_NO_CACHE"] = "1"   # тесты не должны брать данные из кэша прошлых запусков
import random
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from evm import EvmChecker as Checker  # noqa: E402
from report import render  # noqa: E402
from scoring import score  # noqa: E402

E18 = 10 ** 18
SUPPLY = 1_000_000_000 * E18
T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
POOLMGR = "0x8366a39cc670b4001a1121b8f6a443a643e40951"


def A(prefix, i=0):
    return ("0x" + f"{prefix}{i:x}".rjust(40, "0"))[-42:].lower()


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


class FakeChain:
    def __init__(self, token, deployer, factory):
        self.token, self.deployer, self.factory = token, deployer, factory
        self.contracts = {token, factory, POOLMGR}
        self.trs, self.txs, self.ints, self.counts = [], defaultdict(list), defaultdict(list), {}
        self.bal = defaultdict(int)
        self.block, self.log = 1000, 0
        self.verified = True
        self.tx_tt = {}          # tx -> token-transfers (для NFT-позиции)
        self.window = None       # если задано — обычный API видит только N последних переводов
        self.pool_logs = []      # события PoolManager (Initialize / ModifyLiquidity)
        self.liq_usd = 40000.0   # сколько денег в пуле по DexScreener
        self.rpc_broken = False  # RPC не отвечает на eth_getCode / ownerOf
        self.bs_nft_broken = False
        self.nft_owner = {}      # (контракт, id) -> владелец
        self.past = {}          # адрес прошлого токена -> {"symbol", "creator", "sold"}
        self.launch_txs = {}    # tx hash -> адрес созданного токена

    def obj(self, a):
        return {"hash": a, "is_contract": a in self.contracts}

    def fund(self, funder, to, when, eth=1):
        tx = {"hash": f"f{len(self.txs)}{funder[-4:]}{to[-4:]}", "from": self.obj(funder),
              "to": self.obj(to), "value": str(eth * E18), "timestamp": iso(when)}
        self.txs[to].append(tx)
        self.txs[funder].append(tx)

    def transfer(self, frm, to, amount, when, block=None, method=None, tx=None):
        self.log += 1
        self.trs.append({"from": self.obj(frm), "to": self.obj(to), "total": {"value": str(amount)},
                         "timestamp": iso(when), "block_number": block or self.block, "log_index": self.log,
                         "method": method, "transaction_hash": tx or f"0xt{self.log}"})
        self.bal[frm] -= amount
        self.bal[to] += amount


class FakeBS:
    def __init__(self, ch: FakeChain):
        self.c = ch

    async def token(self, a):
        if a in self.c.past:
            return {"name": "Old", "symbol": self.c.past[a]["symbol"], "decimals": "18",
                    "total_supply": str(SUPPLY)}
        if a != self.c.token:
            return None
        return {"name": "Test", "symbol": "TST", "decimals": "18", "total_supply": str(SUPPLY),
                "holders_count": len([1 for v in self.c.bal.values() if v > 0])}

    async def address(self, a):
        if a != self.c.token:
            return {"is_contract": a in self.c.contracts}
        return {"is_verified": self.c.verified, "creator_address_hash": self.c.factory,
                "creation_transaction_hash": "0xcreate"}

    async def smart_contract(self, a):
        if not self.c.verified:
            return None
        return {"is_verified": True, "abi": [
            {"type": "function", "name": "transfer", "stateMutability": "nonpayable"}]}

    async def holders(self, a):
        items = [{"address": self.c.obj(k), "value": str(v)}
                 for k, v in sorted(self.c.bal.items(), key=lambda x: -x[1]) if v > 0 and k != "0x" + "0" * 40]
        return items, True

    async def transfers(self, a, max_pages=None):
        items = list(reversed(self.c.trs))
        if self.c.window:
            return items[: self.c.window], False
        return items, True

    async def get_logs(self, address, topic0, topic1=None, topic2=None, topic3=None, from_block=0, max_pages=10):
        out = []
        for lg in self.c.pool_logs:
            t = lg["topics"]
            if t[0] != topic0 or (topic1 and t[1] != topic1) or (topic2 and (len(t) < 3 or t[2] != topic2)) \
                    or (topic3 and (len(t) < 4 or t[3] != topic3)):
                continue
            out.append(lg)
        return out

    async def tokentx_asc(self, token, n=None):
        out = []
        for t in sorted(self.c.trs, key=lambda t: (t["block_number"], t["log_index"]))[: n or 1000]:
            ts = datetime.fromisoformat(t["timestamp"].replace("Z", "+00:00"))
            out.append({"from": t["from"]["hash"], "to": t["to"]["hash"], "value": t["total"]["value"],
                        "timeStamp": str(int(ts.timestamp())), "blockNumber": str(t["block_number"]),
                        "logIndex": str(t["log_index"]), "hash": t["transaction_hash"]})
        return out

    async def address_txs(self, a):
        return self.c.txs.get(a, [])

    async def address_internal(self, a):
        return self.c.ints.get(a, [])

    async def counters(self, a):
        return {"transactions_count": str(self.c.counts.get(a, 5))}

    async def token_instance(self, c, tid):
        if self.c.bs_nft_broken:
            raise RuntimeError("Blockscout не отвечает")
        o = self.c.nft_owner.get((c, str(tid)))
        return {"owner": self.c.obj(o)} if o else None

    async def tx_token_transfers(self, h):
        if h in self.c.tx_tt:
            return self.c.tx_tt[h]
        tok = self.c.launch_txs.get(h)
        return [{"from": self.c.obj("0x" + "0" * 40), "to": self.c.obj(POOLMGR),
                 "token": {"address_hash": tok}}] if tok else []

    async def address_token_transfers(self, addr, tok):
        if tok == self.c.token:
            return [dict(t, token={"address_hash": tok}) for t in reversed(self.c.trs)
                    if addr in (t["from"]["hash"], t["to"]["hash"])]
        p = self.c.past.get(tok)
        if not p or p["creator"] != addr or not p["sold"]:
            return []
        return [{"from": self.c.obj(addr), "to": {"hash": POOLMGR, "is_contract": True},
                 "total": {"value": str(p["sold"])}, "token": {"address_hash": tok}}]

    async def tx(self, h):
        if h == "0xcreate":
            return {"from": self.c.obj(self.c.deployer),
                    "to": self.c.obj(self.c.factory) if self.c.factory else None}
        return None


class FakeRPC:
    def __init__(self, ch=None):
        self.c = ch

    async def owner(self, t):
        return None

    async def codes(self, addrs):
        if not self.c or self.c.rpc_broken:
            return {}
        return {a: (a in self.c.contracts) for a in addrs}

    async def owner_of(self, nft, tid):
        if not self.c or self.c.rpc_broken:
            return None
        return self.c.nft_owner.get((nft, str(tid))) or "none"

    async def logs(self, *a, **k):
        return None


class FakeDex:
    def __init__(self, ch):
        self.c = ch

    async def pool(self, t):
        if t != self.c.token:
            return None          # прошлые токены — без ликвидности (мертвы)
        base = self.c.bal[POOLMGR] / E18
        return {"dex": "uniswap", "liquidity_usd": self.c.liq_usd, "base_amount": base, "quote_amount": 8.0,
                "price_usd": 20000 / base, "quote_symbol": "ETH"}


def scam_chain():
    token, deployer, factory = A("aa"), A("de", 1), A("fa", 1)
    ch = FakeChain(token, deployer, factory)
    ch.transfer("0x" + "0" * 40, POOLMGR, SUPPLY, T0)              # минт в кривую/пул
    sponsor, brother = A("50", 9), A("b7", 1)            # общий спонсор создателя и его «брата»
    ch.fund(sponsor, deployer, T0 - timedelta(days=3))
    ch.fund(sponsor, brother, T0 - timedelta(days=20))
    for k, (sym, sold) in enumerate([("RUGA", 300_000_000 * E18), ("RUGB", 0)]):
        old_tok, h = A("01d", k), f"0xlaunch{k}"
        ch.past[old_tok] = {"symbol": sym, "creator": brother, "sold": sold}
        ch.launch_txs[h] = old_tok
        ch.txs[brother].append({"hash": h, "from": ch.obj(brother), "to": ch.obj(factory),
                                "value": "0", "timestamp": iso(T0 - timedelta(days=15 - k))})
    ch.transfer(POOLMGR, brother, 40_000_000 * E18, T0 + timedelta(minutes=3), block=1500)
    ch.transfer(POOLMGR, deployer, 30_000_000 * E18, T0)            # создатель купил 3%
    hub = A("b0", 1)                                                # общий спонсор бандла
    ch.fund(deployer, hub, T0 - timedelta(hours=2), 50)
    snipers = [A("5a", i) for i in range(40)]
    for s in snipers:
        ch.fund(hub, s, T0 - timedelta(minutes=30))
        ch.transfer(POOLMGR, s, 20_000_000 * E18, T0 + timedelta(milliseconds=200), block=1001)
    # 20 продали половину, 5 «вышли» переводом на свежие кошельки
    for i, s in enumerate(snipers[:20]):
        ch.transfer(s, POOLMGR, 10_000_000 * E18, T0 + timedelta(minutes=5 + i), block=2000 + i)
    for i, s in enumerate(snipers[20:25]):
        nw = A("ee", i)
        ch.transfer(s, nw, 20_000_000 * E18, T0 + timedelta(minutes=30 + i), block=3000 + i)
    # органика с биржи
    cex = A("cc", 1)
    ch.counts[cex] = 500_000
    for i in range(30):
        b = A("0b", i)
        ch.fund(cex, b, T0 - timedelta(days=30))
        ch.transfer(POOLMGR, b, 3_000_000 * E18, T0 + timedelta(minutes=10 + i), block=4000 + i)
    return ch


def clean_chain(seed=1):
    """Живой токен: разные размеры сделок (от мелочи до китов), хаотичные интервалы."""
    rnd = random.Random(seed)
    token, deployer, factory = A("ab"), A("de", 2), A("fa", 2)
    ch = FakeChain(token, deployer, factory)
    ch.transfer("0x" + "0" * 40, POOLMGR, SUPPLY, T0)
    ch.fund(A("e1"), deployer, T0 - timedelta(days=200))
    ch.transfer(POOLMGR, deployer, 15_000_000 * E18, T0)            # создатель 1.5%
    when, blk = T0 + timedelta(minutes=2), 5000
    wallets = []
    for i in range(150):
        b = A("0c", i)
        wallets.append(b)
        ch.fund(A("f0", i), b, T0 - timedelta(days=60 + i))         # у каждого свой спонсор
        for k in range(20):                                         # «старые» активные кошельки
            ch.txs[b].append({"hash": f"x{i}{k}", "from": ch.obj(b), "to": ch.obj(A("99", k)),
                              "value": "0", "timestamp": iso(T0 - timedelta(days=50 - k))})
    held = defaultdict(int)
    for n in range(400):
        when += timedelta(seconds=rnd.expovariate(1 / 40) * (5 if rnd.random() < .1 else 1))
        blk += 1
        b = rnd.choice(wallets)
        amt = int(rnd.lognormvariate(0, 1.6) * 300_000) * E18      # от мелочи до китов
        if held[b] > 0 and rnd.random() < 0.35:
            amt = min(amt, held[b])
            ch.transfer(b, POOLMGR, amt, when, block=blk)
            held[b] -= amt
        else:
            ch.transfer(POOLMGR, b, amt, when, block=blk)
            held[b] += amt
    return ch


def wash_chain(seed=2):
    """Запуск чистый, но 15 ботов гоняют объём: сделки одного размера, ровный ритм, туда-сюда."""
    rnd = random.Random(seed)
    ch = clean_chain(seed)
    bots = [A("70", i) for i in range(15)]
    cex = A("cd", 1)
    ch.counts[cex] = 900_000
    when = T0 + timedelta(days=1)
    blk = 20000
    for b in bots:
        ch.fund(cex, b, T0 - timedelta(days=90))
        for k in range(20):
            ch.txs[b].append({"hash": f"y{b}{k}", "from": ch.obj(b), "to": ch.obj(A("98", k)),
                              "value": "0", "timestamp": iso(T0 - timedelta(days=80 - k))})
    for n in range(500):
        when += timedelta(seconds=20 + rnd.uniform(-4, 4))
        blk += 1
        b = bots[n % 15]
        amt = int(rnd.uniform(1_000_000, 3_000_000)) * E18          # «по 100–300 долларов»
        if (n // 15) % 2 == 0:
            ch.transfer(POOLMGR, b, amt, when, block=blk)
        else:
            amt = min(amt, max(ch.bal[b], 1))
            ch.transfer(b, POOLMGR, amt, when, block=blk)
    return ch


def serial_rugger_chain():
    """Токен выглядит чисто, но сеть создателя уже рагнула несколько токенов."""
    ch = clean_chain(4)
    sponsor = A("51", 1)
    ch.fund(sponsor, ch.deployer, T0 - timedelta(days=5))
    for i in range(2):
        bro = A("b8", i)
        ch.fund(sponsor, bro, T0 - timedelta(days=40))
        for k in range(3):
            old_tok, h = A("02d", i * 10 + k), f"0xl{i}{k}"
            ch.past[old_tok] = {"symbol": f"OLD{i}{k}", "creator": bro,
                                "sold": 250_000_000 * E18 if k < 2 else 0}
            ch.launch_txs[h] = old_tok
            ch.txs[bro].append({"hash": h, "from": ch.obj(bro), "to": ch.obj(ch.factory),
                                "value": "0", "timestamp": iso(T0 - timedelta(days=30 - k))})
    return ch


NFTPM = A("4f", 4)   # контракт NFT-позиций Uniswap v4


def direct_lp_chain(nft_owner="dev", removed=False, seed=7):
    """Как Zero: токен запущен напрямую в Uniswap v4, ликвидность добавил создатель."""
    rnd = random.Random(seed)
    token, deployer = A("ac"), A("de", 7)
    ch = FakeChain(token, deployer, None)
    ch.contracts.add(NFTPM)
    ch.fund(A("e7"), deployer, T0 - timedelta(days=100))
    ch.transfer("0x" + "0" * 40, deployer, SUPPLY, T0 - timedelta(minutes=1))       # минт создателю
    ch.transfer(deployer, POOLMGR, 900_000_000 * E18, T0, method="modifyLiquidities", tx="0xlpadd")
    ch.tx_tt["0xlpadd"] = [{"from": ch.obj("0x" + "0" * 40), "to": ch.obj(deployer),
                            "token": {"address_hash": NFTPM, "type": "ERC-721"}, "total": {"token_id": "7"}}]
    owner = {"dev": deployer, "dead": "0x000000000000000000000000000000000000dead"}[nft_owner]
    ch.nft_owner[(NFTPM, "7")] = owner
    when, blk = T0 + timedelta(minutes=2), 5000
    wallets = [A("0e", i) for i in range(120)]
    for i, b in enumerate(wallets):
        ch.fund(A("f1", i), b, T0 - timedelta(days=60 + i))
        for k in range(20):
            ch.txs[b].append({"hash": f"z{i}{k}", "from": ch.obj(b), "to": ch.obj(A("97", k)),
                              "value": "0", "timestamp": iso(T0 - timedelta(days=50 - k))})
    for n in range(300):
        when += timedelta(seconds=rnd.expovariate(1 / 40))
        blk += 1
        ch.transfer(POOLMGR, rnd.choice(wallets), int(rnd.lognormvariate(0, 1.5) * 300_000) * E18, when, block=blk)
    if removed:
        ch.transfer(POOLMGR, deployer, 700_000_000 * E18, when + timedelta(minutes=5), block=blk + 10,
                    method="modifyLiquidities", tx="0xlprm")
        ch.liq_usd = 0.0                    # как у Zero после рага
    return ch


def long_scam_chain():
    """Тот же скам-запуск, но история длинная: обычным способом видны только последние 60 переводов."""
    ch = scam_chain()
    ch.window = 60
    return ch


POSM = A("4f", 5)    # PositionManager Uniswap v4
PID = "0x" + "ab" * 32


def _word(v):
    return format(v % 2 ** 256, "064x")


def pool_lp_chain(owner="dev", removed=False):
    """Как Twainpad/Zero: история длинная (видно только хвост), но ликвидность проверяем по событиям пула."""
    from evm import INIT_TOPIC, MODIFY_TOPIC, _pad
    ch = direct_lp_chain("dev", seed=21)
    ch.window = 40                          # переводы запуска не видны
    ch.contracts.add(POSM)
    ch.pool_logs.append({"topics": [INIT_TOPIC, PID, "0x" + "0" * 64, _pad(ch.token)], "data": "0x"})
    ch.pool_logs.append({"topics": [MODIFY_TOPIC, PID, _pad(POSM)],
                         "data": "0x" + _word(-600) + _word(600) + _word(10 ** 20) + _word(42)})
    if removed:
        ch.pool_logs.append({"topics": [MODIFY_TOPIC, PID, _pad(POSM)],
                             "data": "0x" + _word(-600) + _word(600) + _word(-9 * 10 ** 19) + _word(42)})
    ch.nft_owner[(POSM, "42")] = {"dev": ch.deployer, "dead": "0x000000000000000000000000000000000000dead",
                                  "locker": NFTPM}[owner]
    if removed:
        ch.liq_usd = 300.0                  # после рага в пуле почти пусто
    return ch


def pool_launchpad_rpc_broken():
    """Ликвидностью владеет контракт лаунчпада (позиция без номера), а RPC не отвечает.
    Раньше бот принял бы контракт за кошелёк и поднял ложную тревогу."""
    from evm import INIT_TOPIC, MODIFY_TOPIC, _pad
    ch = direct_lp_chain("dead", seed=31)
    ch.window = 40
    launchpad = A("1a", 9)
    ch.contracts.add(launchpad)
    ch.rpc_broken = True
    ch.pool_logs.append({"topics": [INIT_TOPIC, PID, "0x" + "0" * 64, _pad(ch.token)], "data": "0x"})
    ch.pool_logs.append({"topics": [MODIFY_TOPIC, PID, _pad(launchpad)],
                         "data": "0x" + _word(-600) + _word(600) + _word(10 ** 20) + _word(0)})
    return ch


def pool_owner_unknown():
    """Позиция с номером, но и RPC, и Blockscout не смогли назвать владельца. Нельзя писать «в локере»."""
    ch = pool_lp_chain("dev")
    ch.rpc_broken = True
    ch.bs_nft_broken = True
    return ch


def pool_concentrated_churn():
    """Создатель сжёг позицию, а посторонний добавил и забрал узкую позицию с огромным числом
    «единиц ликвидности». Деньги в пуле на месте — это НЕ раг."""
    from evm import MODIFY_TOPIC, _pad
    ch = pool_lp_chain("dead")
    ch.pool_logs.append({"topics": [MODIFY_TOPIC, PID, _pad(POSM)],
                         "data": "0x" + _word(-10) + _word(10) + _word(10 ** 23) + _word(77)})
    ch.pool_logs.append({"topics": [MODIFY_TOPIC, PID, _pad(POSM)],
                         "data": "0x" + _word(-10) + _word(10) + _word(-10 ** 23) + _word(77)})
    return ch


def rh_redistribute_chain():
    """Robinhood: 30 освобождённых от налога кошельков купили 75% и в середине истории (не видна)
    раскидали всё по 400 пустым кошелькам. Конец истории — обычная торговля."""
    rnd = random.Random(41)
    token, deployer, factory = A("ad"), A("de", 9), A("fa", 9)
    ch = FakeChain(token, deployer, factory)
    ch.transfer("0x" + "0" * 40, POOLMGR, SUPPLY, T0)
    ch.fund(A("e9"), deployer, T0 - timedelta(days=100))
    bundle = [A("5b", i) for i in range(30)]
    for w in bundle:
        ch.fund(A("e8"), w, T0 - timedelta(days=30))
        for k in range(20):
            ch.txs[w].append({"hash": f"o{w}{k}", "from": ch.obj(w), "to": ch.obj(A("96", k)),
                              "value": "0", "timestamp": iso(T0 - timedelta(days=29 - k))})
        ch.transfer(POOLMGR, w, 25_000_000 * E18, T0 + timedelta(milliseconds=300), block=1001)
    for i in range(400):
        ch.transfer(bundle[i % 30], A("77", i), 1_875_000 * E18, T0 + timedelta(hours=1, seconds=i), block=3000 + i)
    when, blk = T0 + timedelta(hours=3), 9000
    wallets = [A("0f", i) for i in range(80)]
    for i, b in enumerate(wallets):
        ch.fund(A("f2", i), b, T0 - timedelta(days=60 + i))
        for k in range(20):
            ch.txs[b].append({"hash": f"q{i}{k}", "from": ch.obj(b), "to": ch.obj(A("95", k)),
                              "value": "0", "timestamp": iso(T0 - timedelta(days=50 - k))})
    for n in range(250):
        when += timedelta(seconds=rnd.expovariate(1 / 40))
        blk += 1
        ch.transfer(POOLMGR, rnd.choice(wallets), int(rnd.lognormvariate(0, 1.5) * 300_000) * E18, when, block=blk)
    ch.window = 200
    return ch


def unverified_chain():
    ch = clean_chain(3)
    ch.verified = False
    return ch


async def run(name, ch, expect):
    rep = score(await Checker(FakeBS(ch), FakeRPC(ch), FakeDex(ch)).analyze(ch.token))
    print(f"\n===== {name} =====")
    import re
    print(re.sub(r"<[^>]+>", "", render(rep)))
    assert rep.category[2] in expect, f"{name}: получили «{rep.category[2]}», ждали {expect}"
    return rep


async def main():
    r1 = await run("СКАМ-ЗАПУСК", scam_chain(), {"Точно скам"})
    assert r1.snipers_count == 40 and r1.snipers_moved_pct > 9 and r1.fresh_count >= 40
    assert any(t["status"] == "раг" for t in r1.past_tokens) and r1.dep_network_holders >= 1
    r2 = await run("ЧИСТЫЙ ТОКЕН", clean_chain(),
                   {"Почти нулевая вероятность скама", "Низкий риск"})
    assert r2.linked_pct < 5
    r3 = await run("ВОЛЮМ-БОТЫ", wash_chain(), {"Так себе", "Вероятный скам"})
    assert r3.wash["roundtrip_vol_pct"] > 40
    r5 = await run("СЕРИЙНЫЙ РАГЕР (сам токен чистый)", serial_rugger_chain(), {"Так себе"})
    r6 = await run("LP У СОЗДАТЕЛЯ (как Zero до рага)", direct_lp_chain("dev"), {"Вероятный скам"})
    assert r6.lp_status == "held" and r6.lp_provider_is_dev
    r7 = await run("LP ЗАБРАЛИ (как Zero после рага)", direct_lp_chain("dev", removed=True), {"Точно скам"})
    assert r7.lp_status == "removed"
    rp = direct_lp_chain("dev", removed=True)
    rp.liq_usd = 40000.0                    # «забрал» по переводам, но в пуле ещё $40k — это не полный раг
    # статус ликвидности — «частично», но создатель держит 80% сапплая при живом пуле -> всё равно скам
    r10 = await run("LP: крупный приход из пула, но пул на месте", rp, {"Вероятный скам", "Точно скам"})
    assert r10.lp_status == "partial", r10.lp_status
    r8 = await run("LP СОЖЖЕНА", direct_lp_chain("dead"), {"Почти нулевая вероятность скама", "Низкий риск"})
    assert r8.lp_status == "burned"
    r9 = await run("СКАМ, ДЛИННАЯ ИСТОРИЯ (запуск одним запросом)", long_scam_chain(), {"Точно скам"})
    assert r9.launch_known and r9.taxwin_count >= 40
    p1 = await run("ПУЛ: позиция у создателя, история длинная", pool_lp_chain("dev"), {"Вероятный скам", "Точно скам"})
    assert p1.lp_source == "pool" and p1.lp_status == "held" and p1.lp_provider_is_dev
    p2 = await run("ПУЛ: ликвидность забрали, история длинная", pool_lp_chain("dev", removed=True), {"Точно скам"})
    assert p2.lp_status == "removed"
    p3 = await run("ПУЛ: позиция сожжена", pool_lp_chain("dead"), {"Почти нулевая вероятность скама", "Низкий риск", "Так себе"})
    assert p3.lp_status == "burned"
    p4 = await run("ПУЛ: позиция в контракте-локере", pool_lp_chain("locker"), {"Почти нулевая вероятность скама", "Низкий риск", "Так себе"})
    assert p4.lp_status == "locked"
    p5 = await run("ПУЛ: у лаунчпада, RPC сломан (не должно быть ложной тревоги)", pool_launchpad_rpc_broken(),
                   {"Почти нулевая вероятность скама", "Низкий риск", "Так себе"})
    assert p5.lp_status in ("locked", "launchpad"), p5.lp_status
    p6 = await run("ПУЛ: владельца узнать не удалось (не должно быть «в локере»)", pool_owner_unknown(),
                   {"Так себе", "Вероятный скам", "Точно скам"})
    assert p6.lp_status in ("held_unknown", "held"), p6.lp_status
    p7 = await run("ПУЛ: посторонний добавил/забрал узкую позицию (не раг)", pool_concentrated_churn(),
                   {"Почти нулевая вероятность скама", "Низкий риск", "Так себе"})
    assert p7.lp_status in ("partial", "burned"), p7.lp_status
    rr = await run("ROBINHOOD: бандл раскидал сапплай в середине истории", rh_redistribute_chain(),
                   {"Вероятный скам", "Точно скам"})
    assert rr.launch_moved_wallets >= 300, rr.launch_moved_wallets
    r4 = await run("НЕ ВЕРИФИЦИРОВАН", unverified_chain(), {"Так себе", "Вероятный скам"})
    print("\n✅ все 18 сценариев дали ожидаемую категорию")


if __name__ == "__main__":
    asyncio.run(main())
