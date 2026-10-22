"""Kronos multi-coin scanner -> limit-order-only long/short bot for KuCoin Futures.

Every candle close:
  1. (re)build the universe: SYMBOLS, or the most liquid USDT perpetuals with tight spreads
  2. forecast every coin in the universe plus every coin we hold, in batches
  3. manage open trades (exit on model flip / max hold)
  4. rank fresh signals (RANK_BY) and open the best ones up to MAX_POSITIONS

Per-coin trade lifecycle (side = long or short)
  flat ──signal──▶ entering   post-only limit, below market (long) / above market (short)
  entering ──fill──▶ holding  reduce-only post-only take-profit + exchange stop-LIMIT safety net
  entering ──TTL──▶ flat
  holding ──TP / exchange stop / manual close (position = 0)──▶ flat
  holding ──soft stop / max hold / model flips──▶ exiting  reduce-only limit re-pegged at the touch
  exiting ──position = 0──▶ flat
The exchange position is the source of truth. No market orders are ever sent.
"""
import datetime as dt
import json
import logging
import os
import signal
import sys
import time
import urllib.request

import config as C
from broker import LiveBroker, OrderRejected, PaperBroker, make_exchange
from fmt import fp

logging.basicConfig(level=getattr(logging, C.LOG_LEVEL.upper(), logging.INFO), stream=sys.stdout,
                    format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("bot")

ENTERING, HOLDING, EXITING = "entering", "holding", "exiting"
LONG, SHORT = 1, -1
NAME = {LONG: "LONG", SHORT: "SHORT"}


def notify(msg):
    log.info(msg)
    if not C.WEBHOOK_URL:
        return
    try:
        body = json.dumps({"content": msg, "text": msg}).encode()
        req = urllib.request.Request(C.WEBHOOK_URL, body, {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        log.warning("webhook failed: %s", e)


# ------------------------------------------------------------------ rules --
# Shared with backtest.py so the backtest trades exactly like the bot.
def signal_side(fc):
    """LONG / SHORT / None from a forecast, ignoring ALLOW_* switches."""
    if C.MAX_DISPERSION and fc.close_std > C.MAX_DISPERSION:
        return None
    if fc.exp_return >= C.MIN_EDGE_LONG and fc.up_share >= C.MIN_AGREEMENT:
        return LONG
    if fc.exp_return <= -C.MIN_EDGE_SHORT and fc.up_share <= 1 - C.MIN_AGREEMENT:
        return SHORT
    return None


def allowed(side):
    return (side == LONG and C.ALLOW_LONG) or (side == SHORT and C.ALLOW_SHORT)


def score(fc):
    edge = abs(fc.exp_return)
    if C.RANK_BY == "edge":
        return edge
    if C.RANK_BY == "agreement":
        return max(fc.up_share, 1 - fc.up_share) + edge          # edge breaks ties
    return edge / max(fc.close_std, edge * 0.05, 1e-9)            # sharpe-like


def plan_entry(fc, side, bid, ask, tick):
    """(entry, target) for a maker entry, or (None, None) if the TP can't clear fees."""
    last = fc.last_close
    target = last + C.TP_CAPTURE * (fc.close - last)
    cost = 2 * C.MAKER_FEE + C.MIN_NET_EDGE
    if side == LONG:
        entry = min(last - C.ENTRY_PULLBACK * max(last - fc.low, 0), bid, ask - tick)
        ok = target >= entry * (1 + cost)
    else:
        entry = max(last + C.ENTRY_PULLBACK * max(fc.high - last, 0), ask, bid + tick)
        ok = target <= entry * (1 - cost)
    return (entry, target) if ok else (None, None)


def stop_prices(side, entry):
    """Exchange stop-limit (trigger, limit) for a position."""
    trig = entry * (1 - side * C.HARD_STOP_PCT)
    return trig, trig * (1 - side * C.STOP_LIMIT_SLIPPAGE)


def is_blacklisted(sym):
    return sym.upper() in C.BLACK or sym.split("/")[0].upper() in C.BLACK


class Bot:
    def __init__(self, broker, forecaster, clock=time.time):
        self.b = broker
        self.f = forecaster
        self.clock = clock
        self.tf_s = C.TF_MINUTES * 60
        self.s = self._load()
        self.paper = isinstance(broker, PaperBroker)

    # ---------------------------------------------------------------- state --
    @staticmethod
    def _blank():
        return {"last_bar": None, "universe": [], "universe_bar": -10**9, "trades": {}, "cooldown": {},
                "day": {"date": "", "trades": 0, "pnl": 0.0, "losses_in_row": 0},
                "total": {"trades": 0, "wins": 0, "pnl": 0.0}}

    def _load(self):
        if os.path.exists(C.STATE_FILE):
            try:
                with open(C.STATE_FILE) as fh:
                    s = json.load(fh)
                if "trades" in s:
                    log.info("resumed %d trade(s): %s", len(s["trades"]), list(s["trades"]))
                    return s
                log.warning("state file is from the single-coin version; starting fresh")
            except Exception as e:
                log.warning("bad state file (%s), starting fresh", e)
        return self._blank()

    def save(self):
        tmp = C.STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.s, fh, indent=1)
        os.replace(tmp, C.STATE_FILE)

    def _now(self):
        return dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc)

    def _roll_day(self):
        today = self._now().strftime("%Y-%m-%d")
        if self.s["day"]["date"] != today:
            self.s["day"] = {"date": today, "trades": 0, "pnl": 0.0, "losses_in_row": 0}

    # ----------------------------------------------------------------- loop --
    def current_bar(self):
        return int((self.clock() - C.CANDLE_CLOSE_DELAY) // self.tf_s) - 1

    def step(self):
        self._roll_day()
        bar = self.current_bar()
        if bar != self.s.get("last_bar"):
            self.s["last_bar"] = bar
            self.on_new_bar(bar)
            self.save()
        self.manage()
        self.save()

    def run(self):
        notify(f"Kronos bot up | {C.MODE} | {C.EXCHANGE} {C.TIMEFRAME} x{C.LEVERAGE} {C.MARGIN_MODE} | "
               f"{'SYMBOLS=' + ','.join(C.SYMBOL_LIST) if C.SYMBOL_LIST else f'auto top {C.UNIVERSE_SIZE}'} | "
               f"max {C.MAX_POSITIONS} positions | long={C.ALLOW_LONG} short={C.ALLOW_SHORT}")
        stop = {"flag": False}
        signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
        while not stop["flag"]:
            try:
                self.step()
            except Exception as e:  # network blips etc. — never die, retry next poll
                log.exception("loop error: %s", e)
            time.sleep(C.POLL_SECONDS)
        log.info("SIGTERM: leaving resting orders (incl. exchange stops) in place; state saved")
        self.save()

    # ------------------------------------------------------------ universe --
    def spread_ok(self, sym):
        bid, ask = self.b.book(sym)
        return bid > 0 and (ask - bid) / bid <= C.MAX_SPREAD_PCT

    def refresh_universe(self, bar):
        if C.SYMBOL_LIST:
            uni = [s for s in C.SYMBOL_LIST if not is_blacklisted(s)]
        else:
            uni = []
            for sym, vol in self.b.universe():
                if len(uni) >= C.UNIVERSE_SIZE or vol < C.MIN_VOLUME_USDT:
                    break
                if is_blacklisted(sym):
                    continue
                try:
                    if self.spread_ok(sym):
                        uni.append(sym)
                except Exception as e:
                    log.info("book %s: %s", sym, e)
            uni += [s for s in C.ALWAYS if s not in uni and not is_blacklisted(s)]
        self.s["universe"], self.s["universe_bar"] = uni, bar
        log.info("universe (%d): %s", len(uni), ", ".join(s.split(":")[0] for s in uni))

    # ------------------------------------------------------------- per bar --
    def on_new_bar(self, bar):
        if bar - self.s["universe_bar"] >= C.UNIVERSE_REFRESH_BARS or not self.s["universe"]:
            self.refresh_universe(bar)
        trades = self.s["trades"]
        want = list(dict.fromkeys(self.s["universe"] + list(trades)))
        candles = {}
        for sym in want:
            try:
                c = self.b.candles(sym, C.TIMEFRAME, C.LOOKBACK)
                if len(c) == C.LOOKBACK:
                    candles[sym] = c
                else:
                    log.info("%s: only %d/%d candles, skipped", sym, len(c), C.LOOKBACK)
            except Exception as e:
                log.warning("candles %s: %s", sym, e)
        t0 = time.time()
        fcs = self.f.forecast_many(candles, C.TF_MINUTES, C.SAMPLE_PATHS, C.TEMPERATURE, C.TOP_P,
                                   C.HORIZON_BARS, C.TOP_K, C.FORECAST_BATCH) if candles else {}
        sigs = {s: signal_side(fc) for s, fc in fcs.items()}
        log.info("forecast %d coins in %.1fs | signals: %s", len(fcs), time.time() - t0,
                 ", ".join(f"{s.split('/')[0]} {NAME[v]}" for s, v in sigs.items() if v) or "none")
        for s, fc in fcs.items():
            log.debug("%s %s", s, fc)

        positions = self.b.positions()
        for sym, t in list(trades.items()):
            if t["phase"] == ENTERING and bar - t["entry_bar"] >= C.ENTRY_TTL_BARS:
                self.b.cancel(sym, t["entry_oid"])
                if self.b.positions().get(sym):
                    self._on_entry_filled(sym, t)
                else:
                    log.info("%s entry not filled within %d bar(s), cancelled", sym, C.ENTRY_TTL_BARS)
                    del trades[sym]
                continue
            if t["phase"] in (HOLDING, EXITING):
                t["bars_held"] += 1
                if t["phase"] == HOLDING:
                    if C.EXIT_ON_FLIP and sigs.get(sym) == -t["side"]:
                        self._start_exit(sym, t, "model flipped", cooldown=not C.REVERSE_ON_FLIP)
                    elif t["bars_held"] >= C.MAX_HOLD_BARS:
                        self._start_exit(sym, t, f"max hold {C.MAX_HOLD_BARS} bars")

        if C.HEARTBEAT_BARS and bar % C.HEARTBEAT_BARS == 0:
            d, tt = self.s["day"], self.s["total"]
            held = ", ".join(f"{s.split('/')[0]} {NAME[t['side']]} {t['phase']}" for s, t in trades.items()) or "none"
            notify(f"♥ equity {self.b.equity():.2f} | open: {held} | today {d['pnl']:+.2f} ({d['trades']}) | "
                   f"total {tt['pnl']:+.2f} ({tt['trades']})")

        self._open_best(bar, fcs, sigs, positions)

    def _gates(self):
        now, d = self._now(), self.s["day"]
        if C.PAUSE_ENTRIES:
            return "PAUSE_ENTRIES"
        if now.hour not in C.TRADE_HOURS or now.weekday() not in C.TRADE_DAYS:
            return "outside trading hours"
        if C.MAX_TRADES_PER_DAY and d["trades"] >= C.MAX_TRADES_PER_DAY:
            return "max trades today"
        if C.MAX_DAILY_LOSS_USDT and d["pnl"] <= -C.MAX_DAILY_LOSS_USDT:
            return "daily loss limit"
        if C.MAX_CONSECUTIVE_LOSSES and d["losses_in_row"] >= C.MAX_CONSECUTIVE_LOSSES:
            return "consecutive losses"
        if C.MIN_EQUITY_USDT and self.b.equity() < C.MIN_EQUITY_USDT:
            return "equity below MIN_EQUITY_USDT"
        return None

    def _open_best(self, bar, fcs, sigs, positions):
        trades = self.s["trades"]
        untracked = [s for s in positions if s not in trades]
        if untracked:
            log.warning("untracked positions (not touched, count toward MAX_POSITIONS): %s", untracked)
        slots = C.MAX_POSITIONS - len(trades) - len(untracked)
        cands = sorted(((score(fcs[s]), s, side) for s, side in sigs.items()
                        if side and allowed(side) and s not in trades and s not in positions
                        and bar >= self.s["cooldown"].get(s, -1) and s in self.s["universe"]), reverse=True)
        if slots <= 0 or not cands:
            return
        why = self._gates()
        if why:
            log.info("no entries: %s", why)
            return
        same = {LONG: 0, SHORT: 0}
        notional = 0.0
        for s, t in trades.items():
            same[t["side"]] += 1
            notional += self.b.notional(s, t["contracts"], t.get("entry_price") or t["entry_limit"])
        for sc, sym, side in cands:
            if slots <= 0:
                break
            if C.MAX_SAME_SIDE and same[side] >= C.MAX_SAME_SIDE:
                continue
            try:
                n_val = self._enter(sym, fcs[sym], side, bar, sc, notional)
            except Exception as e:
                log.warning("enter %s failed: %s", sym, e)
                continue
            if n_val:
                slots -= 1; same[side] += 1; notional += n_val

    def _size(self, sym, price, open_notional):
        if C.SIZE_MODE == "equity":
            notional = self.b.equity() * C.EQUITY_PCT * C.LEVERAGE
        else:
            notional = C.NOTIONAL_USDT
        if C.MAX_NOTIONAL_USDT:
            notional = min(notional, C.MAX_NOTIONAL_USDT)
        if C.MAX_TOTAL_NOTIONAL_USDT:
            notional = min(notional, C.MAX_TOTAL_NOTIONAL_USDT - open_notional)
        notional = min(notional, self.b.free() * 0.95 * C.LEVERAGE)
        n = self.b.contracts_for(sym, max(notional, 0), price)
        return min(n, C.MAX_CONTRACTS) if C.MAX_CONTRACTS else n

    def _enter(self, sym, fc, side, bar, sc, open_notional):
        """Place an entry. Returns its notional, or 0 if skipped."""
        if not self.spread_ok(sym):
            log.info("%s spread too wide right now, skip", sym)
            return 0
        bid, ask = self.b.book(sym)
        entry, target = plan_entry(fc, side, bid, ask, self.b.tick(sym))
        if entry is None:
            log.info("%s %s signal but TP too close after fees, skip", sym, NAME[side])
            return 0
        n = self._size(sym, entry, open_notional)
        if n < self.b.meta(sym)["min"]:
            log.info("%s size %d contracts < minimum (1 ct = %s %s), skip", sym, n,
                     fp(self.b.meta(sym)["cs"]), self.b.meta(sym)["base"])
            return 0
        self.b.setup(sym)
        try:
            oid = self.b.limit(sym, "buy" if side == LONG else "sell", n, entry)
        except OrderRejected as e:
            log.info("%s entry rejected (post-only): %s", sym, e)
            return 0
        self.s["trades"][sym] = dict(
            phase=ENTERING, side=side, entry_oid=oid, entry_bar=bar, contracts=n,
            entry_limit=self.b.px(sym, entry), target=self.b.px(sym, target),
            opened_ms=int(self.clock() * 1000),
            realized0=self.b.realized_total(sym) if self.paper else None)
        notify(f"{sym} {NAME[side]} limit {n} ct @ {fp(entry)} | TP {fp(target)} | x{C.LEVERAGE} | "
               f"fc {fc.exp_return:+.3%}, {fc.up_share:.0%} up, score {sc:.2f}")
        return self.b.notional(sym, n, entry)

    # ------------------------------------------------------- every poll --
    def manage(self):
        trades = self.s["trades"]
        if not trades:
            return
        positions = self.b.positions()
        for sym, t in list(trades.items()):
            try:
                self._manage_one(sym, t, positions)
            except Exception as e:
                log.exception("%s manage error: %s", sym, e)

    def _manage_one(self, sym, t, positions):
        if t["phase"] == ENTERING:
            st = self.b.order(sym, t["entry_oid"])
            if st["status"] == "closed":
                self._on_entry_filled(sym, t)
            elif st["status"] in ("canceled", "cancelled", "expired", "rejected"):
                if self.b.positions().get(sym):
                    self._on_entry_filled(sym, t)
                else:
                    log.info("%s entry order %s", sym, st["status"])
                    del self.s["trades"][sym]
            return

        side = t["side"]
        pos, entry = positions.get(sym, (0, 0.0))
        if pos == 0:
            return self._on_closed(sym, t)
        if (pos > 0) != (side == LONG):
            notify(f"⚠ {sym} position {pos} disagrees with state {NAME[side]}; leaving it to you")
            return
        qty = abs(pos)
        t["entry_price"] = entry or t.get("entry_price")
        t["max_qty"] = max(t.get("max_qty", 0), qty)
        bid, ask = self.b.book(sym)
        mid = (bid + ask) / 2
        t["last_mid"] = mid

        if C.EXCHANGE_STOP and t.get("stop_qty") != qty:
            self._place_exchange_stop(sym, t, qty)

        if t["phase"] == HOLDING:
            adverse = side * (t["entry_price"] - mid) / t["entry_price"]
            if adverse >= C.STOP_PCT:
                return self._start_exit(sym, t, f"soft stop {C.STOP_PCT:.2%} (mid {fp(mid)})", cooldown=True)
            if not t.get("tp_oid"):
                tp = max(t["target"], ask) if side == LONG else min(t["target"], bid)
                try:
                    t["tp_oid"] = self.b.limit(sym, "sell" if side == LONG else "buy", qty, tp, reduce_only=True)
                except OrderRejected as e:
                    log.info("%s TP rejected, retry next poll: %s", sym, e)
        else:
            self._repeg(sym, t, qty, bid, ask)

    def _on_entry_filled(self, sym, t):
        pos, entry = self.b.positions().get(sym, (0, 0.0))
        t.update(phase=HOLDING, bars_held=0, entry_price=entry, max_qty=abs(pos),
                 tp_oid=None, exit_oid=None, stop_oid=None, stop_qty=None)
        notify(f"{sym} {NAME[t['side']]} FILLED {abs(pos)} ct @ {fp(entry)}")

    def _place_exchange_stop(self, sym, t, qty):
        if t.get("stop_oid"):
            self.b.cancel(sym, t["stop_oid"])
        trig, lim = stop_prices(t["side"], t["entry_price"])
        try:
            t["stop_oid"] = self.b.stop_limit(sym, "sell" if t["side"] == LONG else "buy", qty, trig, lim)
            t["stop_qty"] = qty
        except Exception as e:
            log.error("%s could not place exchange stop: %s", sym, e)
            t["stop_oid"], t["stop_qty"] = None, None

    def _start_exit(self, sym, t, why, cooldown=False):
        notify(f"{sym} EXIT {NAME[t['side']]}: {why}")
        t.update(phase=EXITING, exit_cooldown=cooldown)
        if t.get("tp_oid"):
            self.b.cancel(sym, t["tp_oid"])
            t["tp_oid"] = None
        pos, _ = self.b.positions().get(sym, (0, 0.0))
        if pos == 0:
            return self._on_closed(sym, t)
        bid, ask = self.b.book(sym)
        self._repeg(sym, t, abs(pos), bid, ask)

    def _repeg(self, sym, t, qty, bid, ask):
        """Keep a reduce-only post-only exit at the touch (inside the spread when it is wide)."""
        tick, side = self.b.tick(sym), t["side"]
        if side == LONG:
            px = max(ask - C.REPEG_TICKS * tick, bid + tick)
        else:
            px = min(bid + C.REPEG_TICKS * tick, ask - tick)
        px = self.b.px(sym, px)
        if t.get("exit_oid"):
            if abs(t.get("exit_px", 0) - px) < tick / 2 and t.get("exit_qty") == qty:
                return
            self.b.cancel(sym, t["exit_oid"])
            t["exit_oid"] = None
            if not self.b.positions().get(sym):
                return
        try:
            t["exit_oid"] = self.b.limit(sym, "sell" if side == LONG else "buy", qty, px, reduce_only=True)
            t.update(exit_px=px, exit_qty=qty)
        except OrderRejected as e:
            log.info("%s exit rejected (post-only), retry next poll: %s", sym, e)

    def _on_closed(self, sym, t):
        for k in ("tp_oid", "exit_oid", "stop_oid"):
            if t.get(k):
                self.b.cancel(sym, t[k])
        self.b.cancel_stops(sym)
        if self.paper:
            pnl, est = self.b.realized_total(sym) - (t.get("realized0") or 0.0), False
        else:
            pnl, est = self.b.realized_pnl(sym, t["opened_ms"]), False
            if pnl is None:              # history not available yet: estimate from last mid
                cs = self.b.meta(sym)["cs"]
                px = t.get("exit_px") or t.get("last_mid") or t["entry_price"]
                pnl = t["side"] * (px - t["entry_price"]) * t.get("max_qty", 0) * cs \
                    - 2 * C.MAKER_FEE * t["entry_price"] * t.get("max_qty", 0) * cs
                est = True
        d, tt = self.s["day"], self.s["total"]
        d["trades"] += 1; d["pnl"] += pnl
        d["losses_in_row"] = 0 if pnl > 0 else d["losses_in_row"] + 1
        tt["trades"] += 1; tt["pnl"] += pnl; tt["wins"] += pnl > 0
        notify(f"{sym} CLOSED {NAME[t['side']]} | pnl {pnl:+.2f} USDT{' (est.)' if est else ''} | "
               f"today {d['pnl']:+.2f} | total {tt['pnl']:+.2f} over {tt['trades']} ({tt['wins']} wins)")
        if t.get("exit_cooldown") or (t["phase"] == HOLDING and pnl < 0):
            self.s["cooldown"][sym] = (self.s["last_bar"] or 0) + C.COOLDOWN_BARS
        del self.s["trades"][sym]


# ------------------------------------------------------------------- main --
def build_broker():
    if C.MODE not in ("paper", "live"):
        sys.exit("MODE must be paper or live (KuCoin Futures has no testnet)")
    if C.MODE == "live" and C.CONFIRM_LIVE.lower() != "yes":
        sys.exit("MODE=live requires CONFIRM_LIVE=yes. Refusing to start.")
    if C.MODE == "paper":
        return PaperBroker(make_exchange(C.EXCHANGE), C.PAPER_START_USDT, C.MAKER_FEE)
    if not (C.API_KEY and C.API_SECRET and C.API_PASSWORD):
        sys.exit("MODE=live needs API_KEY, API_SECRET and API_PASSWORD (KuCoin passphrase)")
    return LiveBroker(make_exchange(C.EXCHANGE, C.API_KEY, C.API_SECRET, C.API_PASSWORD, C.KUCOIN_UTA))


def main():
    log.info("config: %s", C.summary())
    from forecaster import Forecaster
    broker = build_broker()
    if C.MODE == "paper" and os.path.exists(C.STATE_FILE):
        os.remove(C.STATE_FILE)            # paper orders don't survive restarts
    elif C.MODE == "live" and not os.path.exists(C.STATE_FILE):
        broker.cancel_own_open_orders()      # orphans from a lost state file
    fc = Forecaster(C.MODEL_NAME, C.TOKENIZER_NAME, C.MAX_CONTEXT, C.TORCH_THREADS, C.SEED)
    Bot(broker, fc).run()


if __name__ == "__main__":
    main()
