"""Проверка токенов прямо с компьютера, без Telegram.

Запуск:  python check.py            — вставляешь адреса по одному
         python check.py <адрес>    — проверить один и выйти
На Windows можно просто дважды кликнуть «Проверить токен.bat».
Каждый отчёт сохраняется в папку reports/.
"""
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from html import unescape

# Windows-консоль по умолчанию не UTF-8 и падает на эмодзи — включаем UTF-8
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
if os.name == "nt":
    os.system("chcp 65001 > nul")

import config  # noqa: E402
import core  # noqa: E402
from analyzer import detect  # noqa: E402
from bot import check, close_all, parse_mode, verdict  # noqa: E402

REPORTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


def to_plain(html_text: str) -> str:
    """HTML из Telegram-отчёта -> обычный текст для консоли."""
    text = re.sub(r"<a [^>]*>(.*?)</a>", r"\1", html_text)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text)


def save(addr: str, text: str) -> str:
    os.makedirs(REPORTS, exist_ok=True)
    name = f"{datetime.now():%Y-%m-%d_%H-%M-%S}_{addr[:10]}.txt"
    path = os.path.join(REPORTS, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


async def check_one(raw: str) -> bool:
    mode, raw = parse_mode(raw)
    chain, addr = detect(raw)
    if not addr:
        print("❌ Не вижу адреса. Нужен 0x… (Robinhood) или адрес Solana, можно ссылкой.\n")
        return False
    if chain == "solana" and not config.HELIUS_API_KEY:
        print("❌ Для Solana нужен HELIUS_API_KEY в файле .env (бесплатно на dashboard.helius.dev)\n")
        return False
    net = "Solana" if chain == "solana" else "Robinhood Chain"
    kind = "быстрая проверка" if mode == "fast" else "полная проверка"
    print(f"\n🔎 {addr} ({net}) — {kind}")
    core.set_progress(lambda m: print("   " + m, flush=True))
    t0 = time.time()
    try:
        text = to_plain(await asyncio.wait_for(check(chain, addr, mode), timeout=480))
    except asyncio.TimeoutError:
        print("⏱ Слишком долго — источник данных тормозит. Попробуй ещё раз.\n")
        return False
    except Exception as e:
        print(f"❌ Ошибка: {type(e).__name__}: {e}")
        print("   Если повторяется — скопируй эту ошибку и адрес, разберёмся.\n")
        return False
    print("\n" + "─" * 60)
    print(text)
    print("─" * 60)
    print(f"⏱ {time.time() - t0:.0f} сек · отчёт сохранён: {save(addr, text)}\n")
    return True


async def main_json(raw: str):
    """Для торгового бота: python check.py --json <адрес> -> одна строка JSON (safe_to_trade, blockers…)."""
    mode, raw = parse_mode(raw)
    chain, addr = detect(raw)
    try:
        if not addr:
            out = {"error": "не вижу адреса", "safe_to_trade": False}
        else:
            out = await asyncio.wait_for(verdict(chain, addr, mode), timeout=480)
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {e}", "safe_to_trade": False}
    finally:
        await close_all()
    print(json.dumps(out, ensure_ascii=False))


async def main():
    print("=" * 60)
    print("  Rug-чекер · Robinhood Chain + Solana")
    print("=" * 60)
    if not config.HELIUS_API_KEY:
        print("⚠️  HELIUS_API_KEY не задан — работает только Robinhood (0x…)")
    try:
        if len(sys.argv) > 1:
            await check_one(" ".join(sys.argv[1:]))
            return
        print("Вставь адрес токена или ссылку и нажми Enter — полная проверка.")
        print("Если нужно побыстрее (менее глубоко): fast <адрес>")
        print("Выход — пустая строка или «q».\n")
        while True:
            try:
                raw = (await asyncio.to_thread(input, "Адрес> ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if raw.lower() in ("", "q", "exit", "выход"):
                break
            await check_one(raw)
    finally:
        await close_all()
        print("Пока!")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--json":
        asyncio.run(main_json(" ".join(sys.argv[2:])))
        sys.exit(0)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
