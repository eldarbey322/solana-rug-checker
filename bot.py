"""Telegram-бот: кидаешь адрес токена (Robinhood 0x… или Solana) или ссылку — получаешь отчёт.

Запуск:              python bot.py
Проверка без телеги: python bot.py <адрес>
"""
import asyncio
import re
import sys
import time
from html import escape

import cache
import config
from analyzer import Checker, detect
from evm import EvmChecker
from report import render
from scoring import score
from sources import RPC, Blockscout, DexScreener

_closers = []


def build_checker():
    dex = DexScreener()
    bs, rpc = Blockscout(), RPC()
    _closers.extend([dex, bs, rpc])
    sol = None
    if config.HELIUS_API_KEY:
        from sol_sources import Helius, SecondOpinion
        from solana import SolanaChecker
        h, second = Helius(), SecondOpinion()
        _closers.extend([h, second])
        sol = SolanaChecker(h, dex, second)
    return Checker(EvmChecker(bs, rpc, dex), sol)


_checker = build_checker()


_lock = asyncio.Lock()


def parse_mode(text: str):
    """По умолчанию полная проверка. «fast <адрес>» / «быстро …» / «-…» — быстрая."""
    t = (text or "").strip()
    low = t.lower()
    for p in ("/fast", "fast", "быстро", "быстрая", "-"):
        if low.startswith(p):
            return "fast", t[len(p):].strip()
    for p in ("/full", "full", "полный", "полная", "фулл", "+"):
        if low.startswith(p):
            return "full", t[len(p):].strip()
    return config.DEFAULT_MODE, t


_reports: dict = {}
SAFE_LP = {"launchpad", "burned", "locked"}     # ликвидность нельзя вытащить (или нельзя быстро)


async def report(chain: str, addr: str, mode: str = None):
    """Полный разбор токена -> объект Report (с кэшем на CACHE_TTL_S секунд)."""
    mode = mode or config.DEFAULT_MODE
    key = f"{chain}:{addr}:{mode}"
    hit = _reports.get(key)
    if hit and time.time() - hit[0] < config.CACHE_TTL_S:
        return hit[1]
    async with _lock:                       # по одной проверке за раз (режим — общий)
        config.set_mode(mode)
        t0 = time.time()
        try:
            rep = score(await _checker.analyze(chain, addr))
        finally:
            cache.save()
        label = "быстрая" if mode == "fast" else "полная"
        rep.notes.append(f"{label} проверка за {time.time() - t0:.0f} сек")
    _reports[key] = (time.time(), rep)
    return rep


async def check(chain: str, addr: str, mode: str = None) -> str:
    """Отчёт текстом (HTML для Telegram)."""
    return render(await report(chain, addr, mode))


async def verdict(chain: str, addr: str, mode: str = None) -> dict:
    """Ответ для торгового бота: можно ли заходить. Торговать ТОЛЬКО если safe_to_trade == True.
    Одного числа риска мало: пул, который создатель может вытащить, — это стоп при любом риске."""
    r = await report(chain, addr, mode)
    blockers = []
    if r.score >= 35:
        blockers.append(f"риск {r.score}/100 ({r.category[2]})")
    if r.lp_status not in SAFE_LP:
        blockers.append(f"ликвидность: {r.lp_status or 'не проверена'}")
    blockers += [t for t in r.now_threats if t not in blockers]
    return {
        "chain": r.chain, "token": r.token, "symbol": r.symbol,
        "score": r.score, "category": r.category[2] if r.category else None,
        "lp_status": r.lp_status, "lp_by_dev": r.lp_provider_is_dev,
        "now_threats": list(r.now_threats), "dirty_launch": r.dirty_launch, "mature": r.mature,
        "safe_to_trade": not blockers, "blockers": blockers,
    }


async def close_all():
    await asyncio.gather(*(c.close() for c in _closers), return_exceptions=True)


async def run_bot():
    from aiogram import Bot, Dispatcher, F
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.filters import CommandStart
    from aiogram.types import LinkPreviewOptions, Message

    if not config.BOT_TOKEN:
        sys.exit("Нет BOT_TOKEN — впиши его в .env (получить у @BotFather)")
    if not config.HELIUS_API_KEY:
        print("⚠️  HELIUS_API_KEY не задан — Solana работать не будет, только Robinhood")

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(
        parse_mode=ParseMode.HTML, link_preview=LinkPreviewOptions(is_disabled=True)))
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(m: Message):
        await m.answer(
            "Кинь адрес токена или ссылку (pump.fun, dexscreener, solscan, blockscout) — проверю на скам.\n\n"
            "Поддерживаю:\n• Robinhood Chain — адрес вида 0x…\n• Solana — адрес вида 7GCi…pump\n\n"
            "Смотрю контракт, пул, снайперов и бандлы, связанные кошельки, свежие кошельки, "
            "накрутку объёма, создателя и его прошлые токены, риск слива.\n\n"
            "По умолчанию — полная проверка. Если нужно побыстрее: fast <адрес>")

    @dp.message(F.text)
    async def on_text(m: Message):
        mode, rest = parse_mode(m.text)
        chain, addr = detect(rest)
        if not addr:
            await m.answer("Не вижу адреса токена. Нужен адрес 0x… (Robinhood) или адрес Solana.")
            return
        net = "Solana" if chain == "solana" else "Robinhood Chain"
        wait = "быстрая проверка, ~1 мин" if mode == "fast" else "полная проверка, 1–4 мин"
        msg = await m.answer(f"🔎 Проверяю ({net}), {wait}…")
        try:
            text = await asyncio.wait_for(check(chain, addr, mode), timeout=420)
        except asyncio.TimeoutError:
            text = "⏱ Слишком долго — источник данных тормозит. Попробуй ещё раз через минуту."
        except Exception as e:
            text = f"❌ Ошибка: {escape(str(e))}"
        await msg.edit_text(text)

    print("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        await close_all()


async def run_cli(text):
    mode, text = parse_mode(text)
    chain, addr = detect(text)
    if not addr:
        sys.exit("Не вижу адреса токена")
    try:
        print(re.sub(r"<[^>]+>", "", await check(chain, addr, mode)))
    finally:
        await close_all()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        asyncio.run(run_cli(sys.argv[1]))
    else:
        asyncio.run(run_bot())
