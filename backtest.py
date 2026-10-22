"""Walk-forward backtest of the bot's exact rules across several coins.

    python backtest.py --bars 300                              # auto universe (same filters as the bot)
    python backtest.py --bars 300 --symbols BTC/USDT:USDT,PEPE/USDT:USDT

Uses the same signal, ranking (RANK_BY), MAX_POSITIONS / MAX_SAME_SIDE slots, entry, TP and stop
settings as bot.py. Fill model (deliberately conservative):
  * an entry limit fills only if a later candle trades strictly THROUGH it
  * a take-profit fills only if a later candle trades strictly through it
  * if stop and TP are both touched in one candle, the stop is assumed first
  * stop / max-hold / flip exits fill at that candle's close plus one spread against you
Sizing is NOTIONAL_USDT per trade (fixed) so results are in USDT.
"""
import argparse

import numpy as np
import pandas as pd

import config as C
from bot import LONG, NAME, allowed, plan_entry, score, signal_side
from broker import PaperBroker, make_exchange


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=300)
    ap.add_argument("--symbols", default=",".join(C.SYMBOL_LIST))
    ap.add_argument("--top", type=int, default=min(C.UNIVERSE_SIZE, 8), help="auto universe size")
    ap.add_argument("--spread", type=float, default=0.0002)
    a = ap.parse_args()

    from forecaster import Forecaster
    br = PaperBroker(make_exchange(C.EXCHANGE), 0, C.MAKER_FEE)
    syms = [s for s in a.symbols.split(",") if s] or [
        s for s, v in br.universe() if v >= C.MIN_VOLUME_USDT][:a.top]
    need = C.LOOKBACK + a.bars + C.MAX_HOLD_BARS + 2
    data = {}
    for s in syms:
        df = br.candles(s, C.TIMEFRAME, need)
        if len(df) >= C.LOOKBACK + 50:
            data[s] = df.set_index("ts")
    common = sorted(set.intersection(*(set(d.index) for d in data.values())))
    data = {s: d.loc[common].reset_index() for s, d in data.items()}
    n = len(common)
    start = max(n - a.bars - C.MAX_HOLD_BARS - 1, C.LOOKBACK - 1)
    print(f"{len(data)} coins: {', '.join(s.split('/')[0] for s in data)} | trading bars {start}..{n - 2}")

    model = Forecaster(C.MODEL_NAME, C.TOKENIZER_NAME, C.MAX_CONTEXT, C.TORCH_THREADS, C.SEED)
    preds, trades = [], []
    pos, pend, cool = {}, {}, {}
    for i in range(start, n - 1):
        fcs = model.forecast_many({s: d.iloc[i - C.LOOKBACK + 1:i + 1] for s, d in data.items()},
                                  C.TF_MINUTES, C.SAMPLE_PATHS, C.TEMPERATURE, C.TOP_P, C.HORIZON_BARS,
                                  C.TOP_K, C.FORECAST_BATCH)
        sigs = {s: signal_side(fc) for s, fc in fcs.items()}
        for s, fc in fcs.items():
            preds.append((s, fc.exp_return, data[s].close[i + 1] / fc.last_close - 1, fc.up_share))

        for s, p in pos.items():
            p["bars"] += 1
            if (C.EXIT_ON_FLIP and sigs.get(s) == -p["side"]) or p["bars"] >= C.MAX_HOLD_BARS:
                p["force"] = True
        slots = C.MAX_POSITIONS - len(pos) - len(pend)
        same = {1: 0, -1: 0}
        for p in list(pos.values()) + list(pend.values()):
            same[p["side"]] += 1
        cands = sorted(((score(fcs[s]), s, v) for s, v in sigs.items()
                        if v and allowed(v) and s not in pos and s not in pend and i >= cool.get(s, -1)),
                       reverse=True)
        for sc, s, side in cands:
            if slots <= 0:
                break
            if C.MAX_SAME_SIDE and same[side] >= C.MAX_SAME_SIDE:
                continue
            last = fcs[s].last_close
            e, t = plan_entry(fcs[s], side, last * (1 - a.spread / 2), last * (1 + a.spread / 2), last * 1e-7)
            if e:
                pend[s] = dict(side=side, entry=e, target=t, placed=i)
                slots -= 1; same[side] += 1

        for s in list(pend):                                     # candle i+1: entries
            c, p = data[s].iloc[i + 1], pend[s]
            if (p["side"] == LONG and c.low < p["entry"]) or (p["side"] != LONG and c.high > p["entry"]):
                pos[s] = dict(side=p["side"], entry=p["entry"], target=p["target"], bars=0, force=False,
                              opened=c.timestamp)
                del pend[s]
            elif i + 1 - p["placed"] >= C.ENTRY_TTL_BARS:
                del pend[s]
        for s in list(pos):                                      # candle i+1: exits
            c, p = data[s].iloc[i + 1], pos[s]
            if p["opened"] == c.timestamp:
                continue
            sd = p["side"]
            soft = p["entry"] * (1 - sd * C.STOP_PCT)
            hit_stop = c.low <= soft if sd == LONG else c.high >= soft
            hit_tp = c.high > p["target"] if sd == LONG else c.low < p["target"]
            px = why = None
            if hit_stop:
                px, why = (min(c.close, soft) if sd == LONG else max(c.close, soft)) * (1 - sd * a.spread), "stop"
            elif hit_tp:
                px, why = p["target"], "tp"
            elif p["force"]:
                px, why = c.close * (1 - sd * a.spread), "time/flip"
            if px:
                r = sd * (px - p["entry"]) / p["entry"] - 2 * C.MAKER_FEE
                trades.append(dict(symbol=s, side=NAME[sd], open=p["opened"], close=c.timestamp, ret=r, why=why,
                                   pnl=r * C.NOTIONAL_USDT))
                if why == "stop":
                    cool[s] = i + 1 + C.COOLDOWN_BARS
                del pos[s]
        if (i - start) % 20 == 0:
            print(f"  bar {i - start}/{a.bars}: {len(trades)} trades, open {list(pos)}")

    p = pd.DataFrame(preds, columns=["symbol", "fc", "real", "up"])
    t = pd.DataFrame(trades)
    print("\n=== forecast quality (next close) ===")
    for s, g in p.groupby("symbol"):
        print(f"{s:22s} hit {(np.sign(g.fc) == np.sign(g.real)).mean():.1%}  corr {g.fc.corr(g.real):+.3f}")
    print(f"{'ALL':22s} hit {(np.sign(p.fc) == np.sign(p.real)).mean():.1%}  corr {p.fc.corr(p.real):+.3f}")
    print("\n=== strategy ===")
    if t.empty:
        print("no trades — loosen MIN_EDGE_* / MIN_AGREEMENT or ENTRY_PULLBACK")
        return
    eq = t.pnl.cumsum()
    print(f"trades {len(t)} | win {(t.ret > 0).mean():.0%} | avg {t.ret.mean():+.3%} | "
          f"pnl {t.pnl.sum():+.2f} USDT at {C.NOTIONAL_USDT:.0f}/trade | max DD {(eq - eq.cummax()).min():.2f}")
    print(t.groupby(["side", "why"]).ret.agg(["count", "mean"]).to_string())
    print(t.groupby("symbol").pnl.agg(["count", "sum"]).sort_values("sum").to_string())
    t.to_csv("backtest_trades.csv", index=False)
    print("trades -> backtest_trades.csv")


if __name__ == "__main__":
    main()
