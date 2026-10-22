"""Every setting is an environment variable (set them in the Render dashboard).

`python config.py`        prints the effective config (secrets masked)
`python config.py --env`  regenerates .env.example from the table below
"""
import os
import sys

# (name, default, type, group, help)
VARS = [
    # --- Exchange ----------------------------------------------------------
    ("MODE", "paper", str, "Exchange", "paper (simulated fills on live KuCoin prices) | live (real money)"),
    ("CONFIRM_LIVE", "", str, "Exchange", 'must be "yes" for MODE=live'),
    ("EXCHANGE", "kucoinfutures", str, "Exchange", "ccxt exchange id"),
    ("API_KEY", "", str, "Exchange", "KuCoin API key (Futures trade permission, NO withdraw)"),
    ("API_SECRET", "", str, "Exchange", "KuCoin API secret"),
    ("API_PASSWORD", "", str, "Exchange", "KuCoin API passphrase"),
    ("KUCOIN_UTA", "auto", str, "Exchange", "unified trading account: auto | true | false"),
    ("TIMEFRAME", "15m", str, "Exchange", "candle size the model forecasts"),

    # --- Coin selection ----------------------------------------------------
    ("SYMBOLS", "", str, "Universe", "fixed comma list, e.g. BTC/USDT:USDT,PEPE/USDT:USDT; empty = auto-pick by liquidity"),
    ("UNIVERSE_SIZE", 15, int, "Universe", "auto mode: forecast the N most liquid USDT perpetuals"),
    ("MIN_VOLUME_USDT", 20_000_000.0, float, "Universe", "auto mode: min 24h turnover"),
    ("MAX_SPREAD_PCT", 0.0008, float, "Universe", "skip coins whose bid/ask spread is wider than this"),
    ("BLACKLIST", "", str, "Universe", "comma list of symbols or base coins never traded, e.g. USDC,TRUMP"),
    ("ALWAYS_INCLUDE", "", str, "Universe", "comma list always added to the auto universe"),
    ("UNIVERSE_REFRESH_BARS", 16, int, "Universe", "re-rank the universe every N candles"),
    ("RANK_BY", "sharpe", str, "Universe", "rank signals by: edge (|forecast return|) | sharpe (edge / path dispersion) | agreement"),
    ("MAX_POSITIONS", 3, int, "Universe", "max coins held (or being entered) at once"),
    ("MAX_SAME_SIDE", 0, int, "Universe", "max positions in the same direction, 0 = no limit"),
    ("MAX_TOTAL_NOTIONAL_USDT", 0.0, float, "Universe", "cap on combined position value, 0 = none"),
    ("FORECAST_BATCH", 32, int, "Universe", "max sequences per Kronos batch (RAM vs speed)"),

    # --- Futures -----------------------------------------------------------
    ("LEVERAGE", 3, int, "Futures", "leverage sent with every opening order"),
    ("MARGIN_MODE", "isolated", str, "Futures", "isolated | cross"),
    ("SET_MARGIN_MODE_ON_START", True, bool, "Futures", "switch the symbol to MARGIN_MODE at startup (fails if a position is open)"),
    ("TRIGGER_PRICE_TYPE", "mark", str, "Futures", "price that triggers the exchange stop: mark | last | index"),

    # --- Direction ---------------------------------------------------------
    ("ALLOW_LONG", True, bool, "Direction", "open long positions"),
    ("ALLOW_SHORT", True, bool, "Direction", "open short positions"),

    # --- Model -------------------------------------------------------------
    ("MODEL_NAME", "NeoQuasar/Kronos-small", str, "Model", "Kronos-mini | Kronos-small | Kronos-base (HF repo)"),
    ("TOKENIZER_NAME", "NeoQuasar/Kronos-Tokenizer-base", str, "Model", "Kronos-Tokenizer-2k for mini, -base for small/base"),
    ("MAX_CONTEXT", 512, int, "Model", "model context window (512 small/base, 2048 mini)"),
    ("LOOKBACK", 400, int, "Model", "closed candles fed to the model"),
    ("HORIZON_BARS", 1, int, "Model", "forecast this many candles ahead; target uses the last one"),
    ("SAMPLE_PATHS", 8, int, "Model", "independent forecast paths per decision (direction vote)"),
    ("TEMPERATURE", 1.0, float, "Model", "sampling temperature"),
    ("TOP_P", 0.9, float, "Model", "nucleus sampling"),
    ("TOP_K", 0, int, "Model", "top-k sampling, 0 = off"),
    ("SEED", -1, int, "Model", "torch seed for reproducible forecasts, -1 = random"),
    ("TORCH_THREADS", 0, int, "Model", "CPU threads for torch, 0 = torch default"),

    # --- Signal ------------------------------------------------------------
    ("MIN_EDGE_LONG", 0.0015, float, "Signal", "min forecast return to go long (0.0015 = 0.15%)"),
    ("MIN_EDGE_SHORT", 0.0015, float, "Signal", "min forecast drop to go short"),
    ("MIN_AGREEMENT", 0.70, float, "Signal", "share of paths that must agree on direction"),
    ("MAX_DISPERSION", 0.0, float, "Signal", "skip if path std of close / price exceeds this, 0 = off"),
    ("ENTRY_PULLBACK", 0.35, float, "Signal", "entry between touch (0) and forecast low/high (1)"),
    ("TP_CAPTURE", 0.80, float, "Signal", "take profit at this share of the forecast move"),
    ("MAKER_FEE", 0.0002, float, "Signal", "maker fee per side (KuCoin futures base tier 0.02%)"),
    ("MIN_NET_EDGE", 0.0005, float, "Signal", "required TP distance beyond 2x maker fee"),
    ("EXIT_ON_FLIP", True, bool, "Signal", "exit when the model signals the opposite direction"),
    ("REVERSE_ON_FLIP", False, bool, "Signal", "after a flip exit, allow entering the other side next candle"),

    # --- Sizing ------------------------------------------------------------
    ("SIZE_MODE", "fixed", str, "Sizing", "fixed (NOTIONAL_USDT) | equity (EQUITY_PCT of equity as margin)"),
    ("NOTIONAL_USDT", 300.0, float, "Sizing", "position value per trade in fixed mode"),
    ("EQUITY_PCT", 0.10, float, "Sizing", "margin per trade as share of equity in equity mode"),
    ("MAX_CONTRACTS", 0, int, "Sizing", "hard cap on contracts per trade, 0 = none"),
    ("MAX_NOTIONAL_USDT", 0.0, float, "Sizing", "hard cap on position value, 0 = none"),

    # --- Risk --------------------------------------------------------------
    ("STOP_PCT", 0.008, float, "Risk", "soft stop: price move against entry that starts a limit exit"),
    ("EXCHANGE_STOP", True, bool, "Risk", "rest a reduce-only stop-LIMIT on KuCoin (survives bot crashes)"),
    ("HARD_STOP_PCT", 0.015, float, "Risk", "trigger distance of the exchange stop-limit from entry"),
    ("STOP_LIMIT_SLIPPAGE", 0.003, float, "Risk", "limit price offset beyond the stop trigger"),
    ("MAX_HOLD_BARS", 3, int, "Risk", "exit after this many candles"),
    ("ENTRY_TTL_BARS", 1, int, "Risk", "cancel an unfilled entry after this many candles"),
    ("COOLDOWN_BARS", 1, int, "Risk", "candles to wait after a stop-out"),
    ("MAX_TRADES_PER_DAY", 0, int, "Risk", "0 = unlimited (UTC day)"),
    ("MAX_DAILY_LOSS_USDT", 0.0, float, "Risk", "stop opening trades for the UTC day after this loss, 0 = off"),
    ("MAX_CONSECUTIVE_LOSSES", 0, int, "Risk", "pause until next UTC day after N losses in a row, 0 = off"),
    ("MIN_EQUITY_USDT", 0.0, float, "Risk", "never open trades below this account equity"),
    ("PAUSE_ENTRIES", False, bool, "Risk", "kill switch: manage open trade but open no new ones"),
    ("TRADE_HOURS_UTC", "", str, "Risk", 'allowed entry hours, e.g. "6-22" or "0-8,13-20"; empty = always'),
    ("TRADE_WEEKDAYS", "", str, "Risk", 'allowed entry days 0=Mon..6=Sun, e.g. "0-4"; empty = always'),

    # --- Loop / ops --------------------------------------------------------
    ("POLL_SECONDS", 10, int, "Ops", "order/position check interval"),
    ("CANDLE_CLOSE_DELAY", 5, int, "Ops", "seconds after candle close before fetching it"),
    ("REPEG_TICKS", 1, int, "Ops", "exit orders sit this many ticks inside the spread when possible"),
    ("STATE_FILE", "state.json", str, "Ops", "trade state (put on a persistent disk)"),
    ("LOG_LEVEL", "INFO", str, "Ops", "DEBUG | INFO | WARNING"),
    ("WEBHOOK_URL", "", str, "Ops", "Discord/Slack/Telegram-relay webhook for trade alerts"),
    ("HEARTBEAT_BARS", 16, int, "Ops", "post a status webhook every N candles, 0 = off"),
    ("PAPER_START_USDT", 1000.0, float, "Ops", "starting equity in paper mode"),
]

SECRETS = {"API_KEY", "API_SECRET", "API_PASSWORD", "WEBHOOK_URL"}


def _cast(raw, typ):
    if typ is bool:
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return typ(raw)


for _name, _default, _typ, _grp, _help in VARS:
    _raw = os.getenv(_name)
    globals()[_name] = _cast(_raw, _typ) if _raw not in (None, "") else _default

MODE = MODE.lower()
MARGIN_MODE = MARGIN_MODE.lower()
SIZE_MODE = SIZE_MODE.lower()
TF_MINUTES = {"m": 1, "h": 60, "d": 1440}[TIMEFRAME[-1]] * int(TIMEFRAME[:-1])


def parse_ranges(spec, hi):
    """'6-22' -> {6..21}; '0-8,13-20' -> ...; '' -> all."""
    if not spec.strip():
        return set(range(hi))
    out = set()
    for part in spec.split(","):
        a, _, b = part.strip().partition("-")
        out |= set(range(int(a), int(b) if b else int(a) + 1))
    return out


def _csv(v):
    return [x.strip() for x in v.split(",") if x.strip()]


SYMBOL_LIST = _csv(SYMBOLS)
BLACK = {x.upper() for x in _csv(BLACKLIST)}
ALWAYS = _csv(ALWAYS_INCLUDE)
RANK_BY = RANK_BY.lower()

TRADE_HOURS = parse_ranges(TRADE_HOURS_UTC, 24)
TRADE_DAYS = parse_ranges(TRADE_WEEKDAYS, 7)


def summary():
    rows = []
    for name, *_ in VARS:
        v = globals()[name]
        rows.append(f"{name}={'***' if name in SECRETS and v else v}")
    return " ".join(rows)


def env_example():
    lines = ["# Copy to .env for local runs: set -a; source .env; set +a",
             "# On Render, set these in the dashboard. Generated by: python config.py --env", ""]
    grp = None
    for name, default, typ, g, help_ in VARS:
        if g != grp:
            lines += ["", f"# ===== {g} ====="]; grp = g
        d = str(default).lower() if typ is bool else default
        lines += [f"# {help_}", f"{name}={d}"]
    return "\n".join(lines).replace("\n\n\n", "\n\n").lstrip() + "\n"


if __name__ == "__main__":
    if "--env" in sys.argv:
        open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env.example"), "w").write(env_example())
        print("wrote .env.example")
    else:
        print(summary().replace(" ", "\n"))
