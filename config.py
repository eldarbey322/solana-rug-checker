"""Настройки. Всё можно переопределить в файле .env."""
import os

from dotenv import load_dotenv

load_dotenv()


def _f(name, default):
    return float(os.getenv(name, default))


def _i(name, default):
    return int(os.getenv(name, default))


BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# --- Robinhood Chain (chain id 4663) ---
BLOCKSCOUT_URL = os.getenv("BLOCKSCOUT_URL", "https://robinhoodchain.blockscout.com/api/v2")
BLOCKSCOUT_API_KEY = os.getenv("BLOCKSCOUT_API_KEY", "")
# С ключом ходим в официальный API Blockscout для программ (публичный сайт отдаёт 403)
BLOCKSCOUT_PRO_URL = os.getenv("BLOCKSCOUT_PRO_URL", "https://api.blockscout.com/4663/api/v2")
# Etherscan-совместимый API Blockscout: умеет отдавать переводы токена от старых к новым
BLOCKSCOUT_ETH_API = os.getenv("BLOCKSCOUT_ETH_API", "https://api.blockscout.com/v2/api")
UNI_V4_POOL_MANAGER = os.getenv("UNI_V4_POOL_MANAGER", "0x8366a39cc670b4001a1121b8f6a443a643e40951")
LAUNCH_TRANSFERS = int(os.getenv("LAUNCH_TRANSFERS", "1000"))
EXPLORER_URL = os.getenv("EXPLORER_URL", "https://robinhoodchain.blockscout.com")
RPC_URL = os.getenv("RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
DEXSCREENER_URL = os.getenv("DEXSCREENER_URL", "https://api.dexscreener.com/latest/dex/tokens")

# --- Глубина анализа ---
MAX_HOLDER_PAGES = _i("MAX_HOLDER_PAGES", 4)        # по 50 холдеров на страницу
MAX_TRANSFER_PAGES = _i("MAX_TRANSFER_PAGES", 60)   # по 50 переводов на страницу
MAX_WALLETS_TO_TRACE = _i("MAX_WALLETS_TO_TRACE", 150)
CONCURRENCY = _i("CONCURRENCY", 8)                  # параллельных запросов к Blockscout

TOP_TRACE = _i("TOP_TRACE", 60)          # проверяем топ-N холдеров...
TAIL_SAMPLE = _i("TAIL_SAMPLE", 40)      # ...и выборку из хвоста (мелкие холдеры)
TOP_TRADERS_TRACE = _i("TOP_TRADERS_TRACE", 30)   # ...и самых активных трейдеров (кто делает объём)
LAUNCH_WALLETS_TRACE = _i("LAUNCH_WALLETS_TRACE", 40)      # кошельков запуска, чью историю смотрим целиком
MEANINGFUL_TRANSFER = _f("MEANINGFUL_TRANSFER", 1e-4)        # перевод от 0.01% сапплая — уже не пыль
AIRDROP_MIN_RECIPIENTS = _i("AIRDROP_MIN_RECIPIENTS", 20)   # рассылка токена на столько адресов = раздача
SYNC_MIN_SELLERS = _i("SYNC_MIN_SELLERS", 5)                # столько разных продавцов в одном блоке = волна

# --- Окна запуска ---
SNIPE_WINDOW_S = _f("SNIPE_WINDOW_S", 10)   # кто купил в первые N секунд = снайпер
TAX_WINDOW_S = _f("TAX_WINDOW_S", 5)        # анти-снайп налог Pons (99% -> 0 за 5 сек)

# --- Свежие кошельки ---
FRESH_MAX_AGE_H = _f("FRESH_MAX_AGE_H", 168)        # моложе 7 дней на момент покупки
FRESH_MAX_TXS = _i("FRESH_MAX_TXS", 15)

# Если у "общего спонсора" больше транзакций, чем это, считаем его биржей/бриджем
# и НЕ связываем кошельки через него (иначе все, кто вывел с Binance, станут "кластером").
EXCHANGE_TX_THRESHOLD = _i("EXCHANGE_TX_THRESHOLD", 3000)

# Известные инфраструктурные адреса (lowercase): пулы, роутеры, кривые.
KNOWN_INFRA = {
    "0x8366a39cc670b4001a1121b8f6a443a643e40951": "Uniswap v4 PoolManager",
}
for pair in filter(None, os.getenv("KNOWN_INFRA", "").split(",")):
    addr, _, name = pair.partition(":")
    KNOWN_INFRA[addr.strip().lower()] = name.strip() or "infra"

# --- Solana ---
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL", "https://mainnet.helius-rpc.com/")
HELIUS_API_URL = os.getenv("HELIUS_API_URL", "https://api.helius.xyz")
RUGCHECK_URL = os.getenv("RUGCHECK_URL", "https://api.rugcheck.xyz")
FEYN_URL = os.getenv("FEYN_URL", "https://api.feyn.fun")
FEYN_API_KEY = os.getenv("FEYN_API_KEY", "")          # необязательно, только pump.fun
SOL_CONCURRENCY = _i("SOL_CONCURRENCY", 4)
SOL_HOLDER_PAGES = _i("SOL_HOLDER_PAGES", 5)          # по 1000 токен-аккаунтов
SOL_SIG_PAGES = _i("SOL_SIG_PAGES", 20)               # по 1000 подписей (вместе с неудачными)
SOL_MAX_PARSE = _i("SOL_MAX_PARSE", 3000)             # если транзакций меньше — разбираем все
SOL_RECENT_PARSE = _i("SOL_RECENT_PARSE", 2000)       # иначе: последние N...
SOL_LAUNCH_PARSE = _i("SOL_LAUNCH_PARSE", 400)        # ...и первые N (запуск, берутся напрямую)
SOL_BUNDLE_SLOTS = _i("SOL_BUNDLE_SLOTS", 1)          # бандл = слот создания + N слотов
SOL_MAX_TRACE = _i("SOL_MAX_TRACE", 130)
SOL_EXCHANGE_SIGS = _i("SOL_EXCHANGE_SIGS", 1000)     # столько транзакций = биржа/сервис
SOL_PAYER_SERVICE = _i("SOL_PAYER_SERVICE", 5)        # платит комиссию за 5+ людей вне запуска = сервис

CACHE_TTL_S = _i("CACHE_TTL_S", 60)
PAGINATE_BUDGET_S = _i("PAGINATE_BUDGET_S", 60)    # дольше листать историю не будем
OPTIONAL_TIMEOUT_S = _i("OPTIONAL_TIMEOUT_S", 40)  # второстепенные запросы не ждём дольше


# --- Режимы проверки: быстрый (по умолчанию) и полный ---
NET_MAX = 30          # сколько кошельков из сети создателя проверяем
PAST_MAX = 12         # сколько прошлых токенов создателя проверяем
MODES = {
    "fast": dict(MAX_WALLETS_TO_TRACE=70, SOL_MAX_TRACE=70, TOP_TRACE=40, TAIL_SAMPLE=20,
                 TOP_TRADERS_TRACE=20, MAX_TRANSFER_PAGES=20, NET_MAX=12, PAST_MAX=6,
                 SOL_RECENT_PARSE=1000, SOL_MAX_PARSE=1500),
    "full": dict(MAX_WALLETS_TO_TRACE=150, SOL_MAX_TRACE=130, TOP_TRACE=60, TAIL_SAMPLE=40,
                 TOP_TRADERS_TRACE=30, MAX_TRANSFER_PAGES=60, NET_MAX=30, PAST_MAX=12,
                 SOL_RECENT_PARSE=2000, SOL_MAX_PARSE=3000),
}
# По умолчанию — ПОЛНАЯ проверка. Быстрая только по команде «fast <адрес>».
DEFAULT_MODE = os.getenv("CHECK_MODE", "full").strip().lower()
if DEFAULT_MODE not in MODES:
    DEFAULT_MODE = "full"
MODE = DEFAULT_MODE


def set_mode(mode):
    global MODE
    MODE = mode if mode in MODES else DEFAULT_MODE
    globals().update(MODES[MODE])


set_mode(DEFAULT_MODE)
