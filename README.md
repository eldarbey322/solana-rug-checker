# Rugbot — memecoin rug checker for Solana + Robinhood Chain

Paste a token address or a link (pump.fun, DexScreener, Solscan, Blockscout) — the bot detects the chain,
analyses the token on-chain and prints a point-by-point breakdown ✅ / ⚠️ / ❌, followed by a final
risk category and the main reasons.

Works as a **console tool** (`check.py`), a **Telegram bot** (`bot.py`) or a **library** for your own
trading bot (`verdict()` / `check.py --json`).

> Reports are written in **Russian**. The code, config and this README are in English.

| Risk score | Category (as printed) | Meaning |
|---|---|---|
| 0–14 | 🟢 Почти нулевая вероятность скама | almost no signs of a scam |
| 15–34 | 🟢 Низкий риск | low risk — tradable |
| 35–54 | 🟡 Так себе | questionable — red flags, small size only |
| 55–74 | 🟠 Вероятный скам | likely scam |
| 75+ | 🔴 Точно скам | scam — don't touch |

---

## Requirements — API keys

The bot reads blockchain data through third-party APIs. **You need your own keys** (the free tiers are
enough). Keys go into a `.env` file, which is never committed (see `.gitignore`).

| Key | Needed for | Required? | Where to get it |
|---|---|---|---|
| `HELIUS_API_KEY` | **Solana** — parsed transaction history, full holder list, token metadata | **Yes, for Solana** | free at https://dashboard.helius.dev |
| `BLOCKSCOUT_API_KEY` | **Robinhood Chain** — holders, transfers, contracts, pool events | **Yes, for Robinhood** (the public site returns 403 to scripts) | free at https://dev.blockscout.com |
| `BOT_TOKEN` | Telegram bot | only if you run `bot.py` | Telegram → @BotFather → `/newbot` |
| `FEYN_API_KEY` | extra "second opinion" for pump.fun tokens | optional (paid) | https://feyn.fun |

DexScreener (pools/liquidity), RugCheck (second opinion) and the Robinhood public RPC need **no key**.

**Why not the free public Solana RPC?** It is rate-limited to ~100 requests / 10 s per IP and has no parsed
history or full holder lists. A full check makes hundreds of requests and relies on Helius-specific data, so
it would be slow, fail with HTTP 429 and lose most of the checks.

---

## Quick start

### 1. Install Python
Python **3.10+**: https://www.python.org/downloads/
On Windows tick **"Add Python to PATH"** during installation.

### 2. Install dependencies
Open a terminal in the project folder:
```
pip install -r requirements.txt
```

### 3. Add your keys
Copy `.env.example` to `.env` and fill in what you need:
```
HELIUS_API_KEY=your_helius_key
BLOCKSCOUT_API_KEY=your_blockscout_key
BOT_TOKEN=your_telegram_bot_token      # only for the Telegram bot
```

### 4. Check a token from the console (no Telegram needed)
```
python check.py
```
On Windows you can just double-click **check_windows.bat**.
Paste an address or link → Enter → full report. Check as many tokens as you like; an empty line or `q`
exits. Every report is saved to `reports/`.

One-off check: `python check.py <address>`

### 5. (Optional) Run the Telegram bot
```
python bot.py
```
Open your bot in Telegram, press Start and send addresses. It runs while the terminal is open.

### 6. (Optional) Run 24/7 on a VPS
Any small Ubuntu VPS (~$5/month) works:
```
sudo apt install python3-pip -y
pip3 install -r requirements.txt
```
Create `/etc/systemd/system/rugbot.service`:
```
[Unit]
Description=rugbot
After=network-online.target

[Service]
WorkingDirectory=/root/rugbot
ExecStart=/usr/bin/python3 bot.py
Restart=always

[Install]
WantedBy=multi-user.target
```
Then `sudo systemctl enable --now rugbot`. Logs: `journalctl -u rugbot -f`.

---

## Using it from a trading bot

A single risk number is not enough: a pool the creator can pull is a hard stop at any score.
Use the ready-made verdict:

```python
from bot import verdict

v = await verdict("solana", mint)      # or "robinhood"
if v["safe_to_trade"]:
    ...                                 # OK to buy
else:
    print(v["blockers"])                # why not
```

Or from any language: `python check.py --json <address>` → a single JSON line.

`safe_to_trade` is `true` only if the score is below 35 **and** the liquidity can't be pulled
(launchpad / burned / locked) **and** the "what is dangerous now" list is empty.

---

## How to read the result

Before the verdict the report prints two lines — they answer two different questions:

- **📜 Как запускали (how it was launched)** — history: bundle, supply redistribution, fake holders,
  creator's past rugs. A dirty launch stays in the history forever (this is usually what GMGN/Axiom
  show as "scam probability").
- **⚡ Что опасно сейчас (what is dangerous now)** — who holds the supply, who can dump it at once,
  who controls the liquidity.

The category answers the **second** question. If the launch was dirty but the team and the bundle have
verifiably exited and the token has been trading on its own for hours (hundreds of real buyers), it is
not flagged as a scam — it's a normal volatile memecoin. Until a token proves that, a dirty launch means
at least "questionable".

Dust sent to hundreds of wallets does not link them into one group: a transfer counts as a link only
from 0.01% of the supply.

## Full vs fast check

- Plain address → **full check** (default): ~130–150 wallets, more history, deeper creator analysis.
- `fast <address>` → **fast check**: ~70 wallets (snipers, top holders, most active traders, a sample of
  small holders).
- Wallets that show up again (bots, exchanges, snipers) are cached for 30 minutes in `.cache.json`.
- Speed is limited mainly by the free-tier rate limits of Helius and Blockscout.

## What is checked

| Check | Robinhood Chain | Solana |
|---|---|---|
| **Contract** | verified source, proxy, dangerous owner functions (mint, blacklist, tax, pause), renounced owner | mint authority, freeze authority, Token-2022 traps (permanent delegate, transfer hook, transfer fee, default-frozen accounts, pause) |
| **Liquidity** | who added liquidity and where the NFT position is: burned ✅ / locker ⚠️ / held by a wallet ❌ (can pull the pool) / already pulled ❌ | who created the pool: a launchpad (pump.fun etc.) ✅, or a wallet — then where its LP token/position is: burned ✅ / locker ⚠️ / held by a wallet ❌ / already pulled ❌; cross-checked with RugCheck |
| **Launch** | buys in the first 5 s under the 99% Pons anti-snipe tax (only tax-exempt wallets buy then) | buys in the creation slot (Jito bundle) — the pool-creation slot for manually launched tokens — plus wallets whose fees were paid by another wallet |
| **Snipers** | first 10 s: bought / sold / still holding / moved to other wallets | same |
| **Bundle fate** | where the launch wallets' tokens went: sold, or redistributed to many wallets (and whether those still hold) | same |
| **Linked wallets** | common funder (not an exchange), direct funding, token transfers, buys in the same block | same + common fee payer |
| **Who is traced** | snipers, top-60 holders, 30 most active traders and a sample of 40 small holders | same |
| **Fresh wallets** | wallets < 7 days old with almost no history, and wallets that never transacted themselves | same |
| **Holders** | airdrops/dust used to inflate the holder count | same |
| **Volume** | wash trading: round-trips, identical sizes, no small or whale trades, clock-like rhythm, volume from linked wallets | same |
| **Synchronized sells** | waves of many wallets selling in the same block | same |
| **Creator** | share, funders, wallet network, past tokens of the network (alive / dead / rugged) | same (pump.fun launches) |
| **Dump risk** | price impact if the team (linked + creator) sells, and with snipers | same |
| **Second opinion** | — | RugCheck (free) and Feyn (with a key) |

## Hard floors (the category can't be lower)

- Robinhood: unverified contract → at least "questionable".
- Liquidity held by a wallet (not burned/locked) → at least "likely scam".
- Liquidity already pulled → "scam".
- Solana: couldn't verify who holds liquidity of a non-launchpad token → at least "questionable".
- Solana: mint or freeze authority not revoked, transfer hook → at least "likely scam".
- Solana: permanent delegate, default-frozen accounts, non-transferable, pausable, RugCheck "rugged" → "scam".
- Bundle redistributed the supply and the recipients still hold it → at least "likely scam".
- Dirty launch and the token hasn't proven it trades on its own yet → at least "questionable".
- Bot-driven volume → at least "questionable".
- Creator or their network are serial ruggers → at least "likely scam".
- Not enough data → at least "low risk" (never "almost zero").

## Limitations

- It's a heuristic. "Clean" only means "nothing found".
- Tested on live tokens on both chains; the real cases it was tuned on (bundles, supply redistribution,
  pulled pools, wash trading, fake holders) are kept as offline regression scenarios in `tests/`.
- "Checked only X of Y wallets" in a report means part of the requests hit the API rate limit — links and
  fresh-wallet numbers are underestimated; run it again a minute later.
- Links through exchanges/bridges are ignored on purpose, so schemes funded via exchanges may be missed.
- Dump risk uses the x·y=k formula; with concentrated liquidity the real impact is worse.
- For tokens with a very long history the bot analyses the launch and the latest trades and skips the
  middle (the report says so), but still fetches the launch wallets' own history to see where their tokens went.

## Tests (offline, no API keys needed)
```
python tests/test_robinhood.py
python tests/test_solana.py
```

## Tuning

Scores and thresholds are in `scoring.py`; windows and limits in `config.py` (overridable via `.env`).
Best calibration: run 20–30 tokens whose outcome you already know and adjust the thresholds.

## Files

- `check.py` / `check_windows.bat` — console checker (no Telegram), `--json` mode for bots
- `bot.py` — Telegram bot + `report()` / `verdict()` API
- `analyzer.py` — detects the chain from an address or link
- `core.py` — shared logic (snipers, bubbles, fresh wallets, volume, dump risk)
- `evm.py`, `sources.py`, `deployer.py` — Robinhood Chain
- `solana.py`, `sol_sources.py`, `sol_liquidity.py` — Solana
- `scoring.py` — scores and categories, `report.py` — report text
- `cache.py` — on-disk cache of wallet lookups

## Disclaimer

This is a heuristic tool for checking tokens yourself — not financial advice and not a guarantee.
"Clean" only means nothing was found. Scam schemes change faster than any checker; every trade is your
own decision and your own risk.

Never commit your `.env` — it is already in `.gitignore`.
