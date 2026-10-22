# Kronos multi-coin limit-order bot — KuCoin Futures, long + short

A Render background worker. Every 15-minute close it forecasts the next candle for a basket of
KuCoin USDT perpetuals with [Kronos](https://github.com/shiyu-coder/Kronos), ranks the signals,
and opens the best ones, long or short, using **limit orders only**. No market orders exist in the code.

## How it picks coins

* `SYMBOLS` set → trade exactly that list.
* `SYMBOLS` empty → every `UNIVERSE_REFRESH_BARS` candles, take the most liquid USDT perpetuals
  (24h turnover ≥ `MIN_VOLUME_USDT`, spread ≤ `MAX_SPREAD_PCT`, not in `BLACKLIST`), up to `UNIVERSE_SIZE`,
  plus `ALWAYS_INCLUDE`.
* Each candle, every coin gets `SAMPLE_PATHS` forecasts. A coin signals LONG or SHORT when the mean
  forecast move ≥ `MIN_EDGE_LONG` / `MIN_EDGE_SHORT` and ≥ `MIN_AGREEMENT` of paths agree.
* Signals are ranked by `RANK_BY` (`sharpe` = edge ÷ path disagreement, `edge`, or `agreement`) and
  the top ones are opened up to `MAX_POSITIONS`, `MAX_SAME_SIDE` and `MAX_TOTAL_NOTIONAL_USDT`.

## How each trade runs

1. **Entry**: post-only limit below the market (long) or above it (short), `ENTRY_PULLBACK` of the way
   to the forecast low/high. Cancelled after `ENTRY_TTL_BARS` if unfilled.
2. **Take-profit**: reduce-only post-only limit at `TP_CAPTURE` of the forecast move.
   Trades that can't clear 2× `MAKER_FEE` + `MIN_NET_EDGE` are skipped.
3. **Exchange stop-limit** (`EXCHANGE_STOP`): reduce-only stop-*limit* resting on KuCoin at
   `HARD_STOP_PCT`, so a position is protected even if the bot is down.
4. **Early exit** at the touch with a re-pegged reduce-only limit, when:
   - price moves `STOP_PCT` against the entry (soft stop)
   - the trade has been held `MAX_HOLD_BARS` candles
   - the model flips to the opposite signal (`EXIT_ON_FLIP`)

> ⚠️ Limit-only means stops are not guaranteed: in a fast move the exit can fill late or far beyond
> `STOP_PCT`, and the exchange stop-limit can be skipped over entirely if price gaps past its limit.
> With leverage, keep `HARD_STOP_PCT` well inside your liquidation distance.

## Tiny-priced coins (PEPE, SHIB, …)

* Sizes are whole contracts using each market's own multiplier (e.g. 1 BTC contract = 0.001 BTC,
  1 PEPE contract = 1,000,000 PEPE), and prices are rounded to each market's tick (down to 1e-9).
* Prices are rescaled to about 1000 before they go into Kronos. Its normaliser divides by
  `std + 1e-5`, which would otherwise flatten a 0.000004 price series into a constant line.
* Logs and alerts print full decimals (`0.000003921`), never `0.00` or scientific notation.

## Settings

Every setting is an environment variable. All of them are listed, with explanations, in
`.env.example` and pre-filled in `render.yaml`, so they all appear in the Render dashboard.
They are grouped as:

| Group | Covers |
|-------|--------|
| Exchange | mode, KuCoin keys |
| Universe | coin selection, ranking, max positions |
| Futures | leverage, isolated/cross margin, stop trigger price |
| Direction | allow long / allow short |
| Model | Kronos size, lookback, horizon, sampling, batch size |
| Signal | edge thresholds, entry/TP placement, fees, exit on flip |
| Sizing | fixed USDT or % of equity, caps |
| Risk | stops, hold time, cooldown, daily loss / trade limits, kill switch, trading hours |
| Ops | polling, state file, alert webhook, heartbeat |

To add a setting, add a row to `VARS` in `config.py`, then run `python make_render.py` to regenerate
`render.yaml` and `.env.example`.

## Modes

| `MODE` | What happens | Needs |
|--------|--------------|-------|
| `paper` | Real KuCoin prices, simulated fills, 1000 USDT play money | nothing |
| `live`  | Real money | `API_KEY`, `API_SECRET`, `API_PASSWORD` (passphrase), `CONFIRM_LIVE=yes` |

KuCoin Futures has no testnet, so go paper → live with small `NOTIONAL_USDT`.
Create the API key with **Futures trading only, no withdrawals**, ideally IP-restricted.
Dedicate the futures account to the bot. It leaves positions it didn't open alone, but they count
toward `MAX_POSITIONS`, and it cancels its own stop orders on a coin when it closes a trade there.

## Deploy on Render

1. Push this folder to a GitHub repo.
2. Render → **New → Blueprint** → select the repo. You get a Frankfurt background worker
   (Standard, 2 GB) with a 1 GB disk for `state.json`.
3. Deploy in `paper` mode and watch the logs: one line per candle with the universe size,
   forecast time and signals.
4. For live, enter the secrets in the dashboard, set `MODE=live` and `CONFIRM_LIVE=yes`.

## Backtest first

```bash
bash build.sh
python backtest.py --bars 400                                   # auto universe (top 8)
python backtest.py --bars 400 --symbols BTC/USDT:USDT,PEPE/USDT:USDT
```

It replays the same ranking, slots and limit-order rules with a conservative fill model, then
prints per-coin forecast hit-rate and correlation, plus P&L by side, exit reason and coin. Coins
where the correlation is about zero have no edge: blacklist them.

Not financial advice. Kronos is a research model and leveraged futures can lose more than the
trade size. Run paper mode first and start small.
