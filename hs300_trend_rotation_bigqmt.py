#coding:gbk
"""HS300 trend rotation strategy for big-QMT built-in Python.

This is the big-QMT version of the Backtrader prototype:

1. Universe: current HS300 sector constituents from QMT.
2. Filter: close > MA250, MA250 rising over 20 bars, MA20 > MA60 > MA250.
3. Rank: 60-day return, descending.
4. Rebalance: once every REBALANCE_DAYS trading days, hold top MAX_POSITIONS.
5. Exit: daily close below MA60, or rank/trend exit on rebalance day.

Safety:
- LIVE_TRADING is False by default. It logs intended orders only.
- State is persisted per account to avoid duplicate daily rebalances after a
  QMT restart.
- It is intended to run on a 1-minute or daily strategy chart. The logic gates
  by time and date, so repeated handlebar calls on the same day are harmless.
"""

import json
import math
import os
from datetime import datetime


# ========================= USER CONFIG =========================

STRATEGY_NAME = "HS300_TREND_ROTATION_BIGQMT"

# Leave empty to use QMT's injected global variable named `account`.
ACCOUNT_ID = ""

# False = signal log only. True = send real passorder orders.
LIVE_TRADING = False

SECTOR_NAME = "沪深300"
SECTOR_ALIASES = ["沪深300", "沪深300成份股", "沪深300成分股"]
FALLBACK_CONSTITUENTS_FILE = "hs300_constituents.csv"
BENCHMARK_CODE = "000300.SH"

MAX_POSITIONS = 5
ENTRY_RANK = 5
EXIT_RANK = 10
REBALANCE_DAYS = 5

MA_FAST = 20
MA_MID = 60
MA_LONG = 250
LONG_SLOPE_LOOKBACK = 20
MOMENTUM_PERIOD = 60

# Load enough daily bars for all indicators.
DAILY_BAR_COUNT = max(MA_LONG + LONG_SLOPE_LOOKBACK, MOMENTUM_PERIOD + 1) + 10

# Only start trading signals on or after this date.
TRADE_START = "20220701"

# Intraday execution gate. Avoid the A-share closing call auction after 14:57.
EXECUTE_AFTER = "145000"
EXECUTE_BEFORE = "145650"
CLOSING_AUCTION_START = "145700"

# Order settings. QMT passorder: 23=buy, 24=sell, 1101=share amount, 11=limit.
BUY_PRICE_BUFFER = 0.002
SELL_PRICE_BUFFER = 0.002
PRICE_TICK = 0.01
PRICE_DECIMALS = 2
LOT_SIZE = 100
CASH_BUFFER = 0.01

# Optional hard guard. 0 means no cap other than account cash.
MAX_BUY_VALUE_PER_STOCK = 0.0

# Execution safety filters borrowed from the reviewed quant-trade project.
MARKET_FILTER_ENABLED = True
MARKET_OK_STREAK_REQUIRED = 2
MARKET_MA_PERIOD = 20
MARKET_DAILY_DROP_BLOCK = 0.03
FILTER_STAR_MARKET = True
FILTER_ST_STOCKS = True
FILTER_SUSPENDED_BUYS = True
FILTER_LIMIT_UP_BUYS = True
FILTER_LIMIT_DOWN_BUYS = True
LIMIT_UP_THRESHOLD = 0.09
LIMIT_DOWN_THRESHOLD = 0.09
AMOUNT_LOOKBACK = 20
MIN_AVG_AMOUNT = 20000000.0

STATE_DIR_NAME = "QMTStrategyState"
STATE_FILE = ""


# ========================= RUNTIME STATE =========================

S = {
    "schema_version": 1,
    "account": "",
    "live": None,
    "last_processed_date": "",
    "last_rebalance_date": "",
    "last_no_constituents_date": "",
    "trade_dates": [],
    "market_ok_streak": 0,
    "market_weak_streak": 0,
    "attempts": {},        # {yyyymmdd: [remark, ...]}
}


# ========================= SMALL UTILITIES =========================

def log(message):
    print("{} [{}] {}".format(
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), STRATEGY_NAME, message))


def current_account_id():
    if ACCOUNT_ID:
        return str(ACCOUNT_ID)
    value = globals().get("account", "")
    return str(value or "")


def configure_state_file(account_id):
    global STATE_FILE
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(root, STATE_DIR_NAME)
    if not os.path.isdir(path):
        try:
            os.makedirs(path)
        except Exception:
            pass
    safe_account = "".join(ch if ch.isalnum() else "_" for ch in account_id or "NO_ACCOUNT")
    mode = "live" if LIVE_TRADING else "simulation"
    STATE_FILE = os.path.join(path, "{}_{}_{}.json".format(
        STRATEGY_NAME, safe_account, mode))


def load_state():
    global S
    if not STATE_FILE or not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            S.update(data)
    except Exception as exc:
        log("WARNING: state load failed: {}".format(exc))


def save_state():
    if not STATE_FILE:
        return False
    try:
        tmp_path = STATE_FILE + ".tmp"
        with open(tmp_path, "w") as handle:
            json.dump(S, handle, sort_keys=True, indent=2)
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        os.rename(tmp_path, STATE_FILE)
        return True
    except Exception as exc:
        log("WARNING: state save failed: {}".format(exc))
        return False


def attr_number(obj, names, default=0.0):
    for name in names:
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if value is None:
            continue
        try:
            return float(value)
        except Exception:
            continue
    return float(default)


def full_code_from_record(record):
    for name in ["m_strStockCode", "m_strSecurityID", "m_strInstrumentID"]:
        try:
            code = str(getattr(record, name) or "")
        except Exception:
            code = ""
        if code:
            break
    else:
        code = ""
    market = ""
    for name in ["m_strExchangeID", "m_strMarket"]:
        try:
            market = str(getattr(record, name) or "").upper()
        except Exception:
            market = ""
        if market:
            break
    if "." in code:
        return code.upper().replace(".SS", ".SH")
    if market in ["SH", "SZ"]:
        return "{}.{}".format(code, market)
    if code.startswith("6") or code.startswith("5") or code.startswith("9"):
        return "{}.SH".format(code)
    if code:
        return "{}.SZ".format(code)
    return ""


def same_code(left, right):
    left = str(left or "").upper().replace(".SS", ".SH")
    right = str(right or "").upper().replace(".SS", ".SH")
    return left == right or left.split(".")[0] == right.split(".")[0]


def round_price(price):
    if price <= 0:
        return 0.0
    return round(math.floor(price / PRICE_TICK + 0.5) * PRICE_TICK, PRICE_DECIMALS)


def buy_lot(value):
    return max(0, int(math.floor(float(value) / LOT_SIZE)) * LOT_SIZE)


def sell_lot(value):
    return max(0, int(math.floor(float(value) / LOT_SIZE)) * LOT_SIZE)


def is_valid_number(value):
    try:
        return value is not None and math.isfinite(float(value))
    except Exception:
        return False


def today_text():
    return datetime.now().strftime("%Y%m%d")


def now_hhmmss():
    return datetime.now().strftime("%H%M%S")


# ========================= QMT DATA ADAPTERS =========================

def call_first(methods, args):
    for method in methods:
        if not callable(method):
            continue
        try:
            value = method(*args)
            if value:
                return value
        except Exception:
            continue
    return None


def get_hs300_codes(C):
    methods = [
        getattr(C, "get_stock_list_in_sector", None),
        globals().get("get_stock_list_in_sector"),
        getattr(C, "get_sector", None),
        globals().get("get_sector"),
    ]
    codes = None
    for sector_name in SECTOR_ALIASES:
        codes = call_first(methods, (sector_name,))
        if codes:
            break
    result = []
    for code in codes or []:
        value = normalize_stock_code(str(code))
        if value and value not in result:
            result.append(value)
    if not result:
        result = load_fallback_constituents()
    return result


def load_fallback_constituents():
    candidates = [
        os.path.join(os.getcwd(), FALLBACK_CONSTITUENTS_FILE),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), FALLBACK_CONSTITUENTS_FILE),
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        result = []
        try:
            with open(path, "r") as handle:
                for line in handle:
                    value = line.strip()
                    if not value or value.lower() == "code":
                        continue
                    code = normalize_stock_code(value)
                    if code and code not in result:
                        result.append(code)
            if result:
                log("QMT sector query returned empty; loaded {} HS300 codes from {}".format(
                    len(result), path))
                return result
        except Exception as exc:
            log("fallback constituents load failed: {} {}".format(path, exc))
    return []


def no_constituents_logged(current_date):
    return S.get("last_no_constituents_date") == current_date


def mark_no_constituents_logged(current_date):
    S["last_no_constituents_date"] = current_date
    save_state()


def normalize_stock_code(code):
    value = str(code or "").strip().upper().replace(".SS", ".SH")
    if not value:
        return ""
    if "." in value:
        raw, market = value.rsplit(".", 1)
        if market in ["SH", "SZ"]:
            return "{}.{}".format(raw, market)
    raw = value
    if raw.startswith(("6", "5", "9")):
        return "{}.SH".format(raw)
    if raw.startswith(("0", "1", "2", "3")):
        return "{}.SZ".format(raw)
    return value


def get_market_data(C, fields, codes, count):
    method = getattr(C, "get_market_data_ex", None)
    if not callable(method):
        method = globals().get("get_market_data_ex")
    if not callable(method):
        raise RuntimeError("get_market_data_ex is unavailable")
    return method(
        fields, codes, period="1d", count=count,
        dividend_type="front_ratio", fill_data=False, subscribe=True)


def frame_values(frame, field):
    if frame is None:
        return []

    # pandas DataFrame path: column named `field`.
    try:
        if hasattr(frame, "columns") and field in list(frame.columns):
            return [float(x) for x in list(frame[field]) if is_valid_number(x)]
    except Exception:
        pass

    # pandas Series path.
    try:
        if hasattr(frame, "tolist"):
            return [float(x) for x in frame.tolist() if is_valid_number(x)]
    except Exception:
        pass

    # dict path: {"close": [...]}.
    try:
        if isinstance(frame, dict) and field in frame:
            return [float(x) for x in list(frame[field]) if is_valid_number(x)]
    except Exception:
        pass

    # list-of-records path.
    values = []
    try:
        for row in frame:
            if isinstance(row, dict) and field in row and is_valid_number(row[field]):
                values.append(float(row[field]))
        return values
    except Exception:
        return []


def stock_frame(data, code):
    if isinstance(data, dict):
        if code in data:
            return data.get(code)
        bare = code.split(".")[0]
        for key, value in data.items():
            if same_code(str(key), code) or str(key).split(".")[0] == bare:
                return value
    return None


def latest_close_map(C, codes):
    data = get_market_data(C, ["close"], codes, DAILY_BAR_COUNT)
    result = {}
    for code in codes:
        closes = frame_values(stock_frame(data, code), "close")
        if closes:
            result[code] = closes
    return result


def latest_market_map(C, codes):
    data = get_market_data(
        C, ["open", "high", "low", "close", "volume", "amount"], codes, DAILY_BAR_COUNT)
    result = {}
    for code in codes:
        frame = stock_frame(data, code)
        closes = frame_values(frame, "close")
        if closes:
            result[code] = {
                "open": frame_values(frame, "open"),
                "high": frame_values(frame, "high"),
                "low": frame_values(frame, "low"),
                "close": closes,
                "volume": frame_values(frame, "volume"),
                "amount": frame_values(frame, "amount"),
            }
    return result


# ========================= ACCOUNT AND ORDER QUERIES =========================

def qmt_detail(account_id, detail_type, strategy_name=None):
    method = globals().get("get_trade_detail_data")
    if not callable(method):
        raise RuntimeError("get_trade_detail_data is unavailable")
    if strategy_name is None:
        return method(account_id, "stock", detail_type)
    try:
        return method(account_id, "stock", detail_type, strategy_name)
    except TypeError:
        return method(account_id, "stock", detail_type)


def account_available_cash(account_id):
    values = qmt_detail(account_id, "account")
    if not values:
        return 0.0
    return attr_number(values[0], ["m_dAvailable", "m_dEnableBalance"], 0.0)


def account_total_asset(account_id):
    values = qmt_detail(account_id, "account")
    if not values:
        return 0.0
    return attr_number(values[0], ["m_dBalance", "m_dAssureAsset", "m_dTotalAsset"], 0.0)


def position_map(account_id):
    result = {}
    for position in qmt_detail(account_id, "position") or []:
        code = full_code_from_record(position)
        if not code:
            continue
        total = int(attr_number(position, ["m_nVolume"], 0))
        available = int(attr_number(position, ["m_nCanUseVolume"], total))
        market_value = attr_number(position, ["m_dInstrumentValue", "m_dMarketValue"], 0.0)
        result[code] = {
            "total": max(0, total),
            "available": max(0, available),
            "market_value": max(0.0, market_value),
        }
    return result


def safe_account_total_asset(account_id):
    try:
        return account_total_asset(account_id), True
    except Exception as exc:
        log("account asset query failed: {}".format(exc))
        return 0.0, False


def safe_account_available_cash(account_id):
    try:
        return account_available_cash(account_id), True
    except Exception as exc:
        log("account cash query failed: {}".format(exc))
        return 0.0, False


def safe_position_map(account_id):
    try:
        return position_map(account_id), True
    except Exception as exc:
        log("position query failed: {}".format(exc))
        return {}, False


def has_active_order(account_id, stock_code):
    try:
        orders = qmt_detail(account_id, "order") or []
    except Exception as exc:
        log("order query failed for {}; skip to avoid duplicate: {}".format(stock_code, exc))
        return True
    for order in orders:
        code = full_code_from_record(order)
        if not same_code(code, stock_code):
            continue
        status = int(attr_number(order, ["m_nOrderStatus"], -1))
        # QMT terminal statuses seen in practice:
        # 53=part-canceled, 54=canceled, 56=filled, 57=rejected/junk.
        if status in [53, 54, 56, 57]:
            continue
        requested = int(attr_number(
            order, ["m_nVolumeTotalOriginal", "m_nVolume", "m_nOrderVolume"], 0))
        traded = int(attr_number(order, ["m_nVolumeTraded", "m_nTradedVolume"], 0))
        left = int(attr_number(order, ["m_nVolumeLeft", "m_nVolumeTotal"], 0))
        if max(left, requested - traded) > 0:
            return True
    return False


# ========================= SIGNAL LOGIC =========================

def ema(values, period):
    if not values:
        return []
    alpha = 2.0 / float(period + 1)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1.0 - alpha) * result[-1])
    return result


def macd_hist(values, fast=12, slow=26, signal=9):
    if len(values) < slow + signal:
        return []
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    dif = [f - s for f, s in zip(fast_ema, slow_ema)]
    dea = ema(dif, signal)
    return [d - e for d, e in zip(dif, dea)]


def check_market_trend(index_closes):
    if not MARKET_FILTER_ENABLED:
        return True, "disabled"
    if len(index_closes) < max(MARKET_MA_PERIOD, 35):
        return False, "index_history_insufficient"
    last = float(index_closes[-1])
    prev = float(index_closes[-2])
    if prev > 0 and (last - prev) / prev <= -MARKET_DAILY_DROP_BLOCK:
        return False, "index_daily_drop"
    ma = moving_average(index_closes, MARKET_MA_PERIOD)
    hist = macd_hist(index_closes)
    if ma is None or not hist:
        return False, "index_indicator_unavailable"
    if last > ma and hist[-1] > 0:
        return True, "ok"
    return False, "index_trend_weak"


def update_market_streak(market_ok):
    if market_ok:
        S["market_ok_streak"] = int(S.get("market_ok_streak", 0) or 0) + 1
        S["market_weak_streak"] = 0
    else:
        S["market_ok_streak"] = 0
        S["market_weak_streak"] = int(S.get("market_weak_streak", 0) or 0) + 1


def can_open_new_positions():
    if not MARKET_FILTER_ENABLED:
        return True
    return int(S.get("market_ok_streak", 0) or 0) >= MARKET_OK_STREAK_REQUIRED


def is_star_market(code):
    value = normalize_stock_code(code)
    return value.startswith("688")


def instrument_name(C, code):
    methods = [getattr(C, "get_instrumentdetail", None), globals().get("get_instrumentdetail")]
    for method in methods:
        if not callable(method):
            continue
        try:
            detail = method(code)
            if isinstance(detail, dict):
                return str(
                    detail.get("m_strInstrumentName")
                    or detail.get("InstrumentName")
                    or detail.get("name")
                    or ""
                )
        except Exception:
            continue
    return ""


def filter_buyable_codes(C, codes):
    result = []
    st_failures = 0
    for code in codes:
        norm = normalize_stock_code(code)
        if FILTER_STAR_MARKET and is_star_market(norm):
            continue
        if FILTER_ST_STOCKS:
            name = instrument_name(C, norm)
            if "ST" in name.upper():
                continue
            if not name:
                st_failures += 1
        result.append(norm)
    if st_failures > 0:
        log("instrument detail unavailable for {} symbols; kept them".format(st_failures))
    return result


def moving_average(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / float(period)


def tail_value(values, default=0.0):
    if values:
        return float(values[-1])
    return default


def average_tail(values, period):
    tail = [float(value) for value in values[-period:] if is_valid_number(value)]
    if not tail:
        return None
    return sum(tail) / float(len(tail))


def candidate_snapshot(code, history):
    if isinstance(history, dict):
        closes = history.get("close") or []
        opens = history.get("open") or []
        highs = history.get("high") or []
        lows = history.get("low") or []
        volumes = history.get("volume") or []
        amounts = history.get("amount") or []
    else:
        closes = history or []
        opens, highs, lows, volumes, amounts = [], [], [], [], []
    required = max(MA_LONG + LONG_SLOPE_LOOKBACK, MOMENTUM_PERIOD + 1)
    if len(closes) < required:
        return None
    close = float(closes[-1])
    prev_close = float(closes[-2]) if len(closes) >= 2 else 0.0
    ma20 = moving_average(closes, MA_FAST)
    ma60 = moving_average(closes, MA_MID)
    ma250 = moving_average(closes, MA_LONG)
    ma250_old = sum(closes[-MA_LONG - LONG_SLOPE_LOOKBACK:-LONG_SLOPE_LOOKBACK]) / float(MA_LONG)
    old_close = float(closes[-MOMENTUM_PERIOD - 1])
    values = [close, ma20, ma60, ma250, ma250_old, old_close]
    if not all(is_valid_number(value) for value in values) or old_close <= 0:
        return None
    eligible = close > ma250 and ma250 > ma250_old and ma20 > ma60 > ma250
    momentum = close / old_close - 1.0
    return {
        "code": code,
        "close": close,
        "closes": closes,
        "prev_close": prev_close,
        "open": tail_value(opens),
        "high": tail_value(highs),
        "low": tail_value(lows),
        "volume": tail_value(volumes),
        "amount": tail_value(amounts),
        "avg_amount": average_tail(amounts, AMOUNT_LOOKBACK),
        "ma60": ma60,
        "eligible": bool(eligible),
        "momentum": momentum,
    }


def build_rankings(close_history):
    rows = []
    all_rows = {}
    for code, closes in close_history.items():
        row = candidate_snapshot(code, closes)
        if not row:
            continue
        all_rows[code] = row
        if row["eligible"]:
            rows.append(row)
    rows.sort(key=lambda row: row["momentum"], reverse=True)
    return rows, all_rows


def buy_block_reason(row):
    if FILTER_SUSPENDED_BUYS and row.get("volume", 0.0) <= 0:
        return "suspended_or_zero_volume"
    prev = float(row.get("prev_close", 0.0) or 0.0)
    close = float(row.get("close", 0.0) or 0.0)
    if prev > 0 and FILTER_LIMIT_UP_BUYS and close >= prev * (1.0 + LIMIT_UP_THRESHOLD):
        return "limit_up"
    if prev > 0 and FILTER_LIMIT_DOWN_BUYS and close <= prev * (1.0 - LIMIT_DOWN_THRESHOLD):
        return "limit_down"
    avg_amount = row.get("avg_amount")
    if MIN_AVG_AMOUNT > 0 and avg_amount is not None and avg_amount < MIN_AVG_AMOUNT:
        return "low_liquidity"
    return ""


def filter_ranked_for_buy(rows):
    result = []
    blocked = {}
    for row in rows:
        reason = buy_block_reason(row)
        if reason:
            blocked[reason] = blocked.get(reason, 0) + 1
            continue
        result.append(row)
    return result, blocked


def format_ranked(rows, limit=10):
    values = []
    for idx, row in enumerate(rows[:limit], 1):
        values.append("{}:{}:{:.2%}".format(idx, row["code"], row["momentum"]))
    return ",".join(values) if values else "NONE"


def format_blocked_counts(blocked):
    if not blocked:
        return "NONE"
    return ",".join("{}={}".format(key, blocked[key]) for key in sorted(blocked.keys()))


def remember_trade_date(current_date):
    dates = S.setdefault("trade_dates", [])
    if current_date not in dates:
        dates.append(current_date)
        dates.sort()
    if len(dates) > 250:
        del dates[:-250]


def trading_days_since(last_date, current_date):
    dates = S.get("trade_dates", [])
    if last_date not in dates or current_date not in dates:
        return REBALANCE_DAYS
    return dates.index(current_date) - dates.index(last_date)


def value_by_code(mapping, code, default=None):
    if code in mapping:
        return mapping.get(code)
    for key, value in mapping.items():
        if same_code(key, code):
            return value
    return default


def rebalance_due(current_date):
    if S.get("last_rebalance_date") == current_date:
        return False
    last_date = S.get("last_rebalance_date", "")
    if not last_date:
        return True
    return trading_days_since(last_date, current_date) >= REBALANCE_DAYS


def daily_already_processed(current_date):
    return S.get("last_processed_date") == current_date


def mark_daily_processed(current_date):
    S["last_processed_date"] = current_date
    save_state()


def mark_rebalanced(current_date):
    S["last_rebalance_date"] = current_date
    save_state()


# ========================= EXECUTION =========================

def make_remark(current_date, code, side, volume):
    return "{}_{}_{}_{}".format(
        STRATEGY_NAME, side, code.split(".")[0], current_date)[-64:]


def attempt_recorded(current_date, remark):
    attempts = S.setdefault("attempts", {})
    return remark in attempts.get(current_date, [])


def record_attempt(current_date, remark):
    attempts = S.setdefault("attempts", {})
    values = attempts.setdefault(current_date, [])
    if remark not in values:
        values.append(remark)
    # Keep the file small.
    for key in list(attempts.keys()):
        if key < current_date:
            attempts.pop(key, None)
    return save_state()


def submit_order(C, account_id, code, side, volume, reference_price, current_date):
    volume = int(volume)
    if volume < LOT_SIZE:
        return False
    if has_active_order(account_id, code):
        log("SKIP {} {}: active order exists".format(side, code))
        return False
    if side == "BUY":
        op_type = 23
        price = round_price(reference_price * (1.0 + BUY_PRICE_BUFFER))
    else:
        op_type = 24
        price = round_price(reference_price * (1.0 - SELL_PRICE_BUFFER))
    if price <= 0:
        log("SKIP {} {}: invalid price".format(side, code))
        return False
    remark = make_remark(current_date, code, side, volume)
    if attempt_recorded(current_date, remark):
        log("SKIP {} {}: attempt already recorded today".format(side, code))
        return False
    if not record_attempt(current_date, remark):
        log("BLOCKED {} {}: state file is not writable".format(side, code))
        return False

    if not LIVE_TRADING:
        log("DRY RUN: {} {} shares of {} at {:.2f}; remark={}".format(
            side, volume, code, price, remark))
        return True

    try:
        passorder(op_type, 1101, account_id, code, 11, price,
                  int(volume), STRATEGY_NAME, 1, remark, C)
        log("ORDER SENT: {} {} shares of {} at {:.2f}; remark={}".format(
            side, volume, code, price, remark))
        return True
    except Exception as exc:
        log("ORDER ERROR: {} {} failed: {}".format(side, code, exc))
        return False


def execute_strategy(C, account_id, current_date):
    remember_trade_date(current_date)
    all_codes = get_hs300_codes(C)
    if not all_codes:
        if not no_constituents_logged(current_date):
            log("BLOCKED: no HS300 constituents returned by QMT sector query and fallback file {}; copy this file into the strategy directory or confirm QMT sector name".format(
                FALLBACK_CONSTITUENTS_FILE))
            mark_no_constituents_logged(current_date)
        return

    try:
        C.set_universe([BENCHMARK_CODE] + all_codes)
    except Exception:
        pass

    market_history = latest_market_map(C, [BENCHMARK_CODE] + all_codes)
    index_history = value_by_code(market_history, BENCHMARK_CODE, {})
    index_closes = index_history.get("close", [])
    market_ok, market_reason = check_market_trend(index_closes)
    update_market_streak(market_ok)

    buy_codes = filter_buyable_codes(C, all_codes)
    all_history = dict((code, value_by_code(market_history, code, {})) for code in all_codes)
    buy_history = dict((code, value_by_code(market_history, code, {})) for code in buy_codes)
    ranked_raw, _ = build_rankings(buy_history)
    ranked, blocked_counts = filter_ranked_for_buy(ranked_raw)
    _, all_rows = build_rankings(all_history)

    positions, positions_ok = safe_position_map(account_id)
    if not positions_ok:
        log("BLOCKED: position query failed; skip trading to avoid duplicate or blind orders")
        mark_daily_processed(current_date)
        return

    hs300_position_codes = [
        code for code in positions.keys()
        if any(same_code(code, hs_code) for hs_code in all_codes)
    ]

    # Daily risk exit: close below MA60.
    sell_orders_sent = 0
    for code in hs300_position_codes:
        row = value_by_code(all_rows, code)
        if not row:
            continue
        pos = positions.get(code, {})
        available = sell_lot(pos.get("available", 0))
        if available >= LOT_SIZE and row["close"] < row["ma60"]:
            if submit_order(C, account_id, code, "SELL", available, row["close"], current_date):
                sell_orders_sent += 1

    do_rebalance = rebalance_due(current_date)
    if not do_rebalance:
        days_since = trading_days_since(S.get("last_rebalance_date", ""), current_date)
        log("daily check done: ranked_candidates={} market={} rebalance_due=False days_since_rebalance={} top={}".format(
            len(ranked), market_reason, days_since, format_ranked(ranked, 5)))
        mark_daily_processed(current_date)
        return

    exit_buffer = set(row["code"] for row in ranked[:EXIT_RANK])
    target_codes = [row["code"] for row in ranked[:ENTRY_RANK]][:MAX_POSITIONS]

    # Rebalance exits: trend failed or fell out of exit buffer.
    for code in hs300_position_codes:
        if code in exit_buffer:
            continue
        row = value_by_code(all_rows, code)
        close = row["close"] if row else 0.0
        if close <= 0:
            history = value_by_code(market_history, code, {})
            closes = history.get("close", []) if isinstance(history, dict) else []
            close = closes[-1] if closes else 0.0
        pos = positions.get(code, {})
        available = sell_lot(pos.get("available", 0))
        if available >= LOT_SIZE and close > 0:
            if submit_order(C, account_id, code, "SELL", available, close, current_date):
                sell_orders_sent += 1

    if not target_codes:
        log("REBALANCE: no eligible candidates; blocked={} raw_top={}".format(
            format_blocked_counts(blocked_counts), format_ranked(ranked_raw, 5)))
        mark_rebalanced(current_date)
        mark_daily_processed(current_date)
        return

    if LIVE_TRADING and sell_orders_sent > 0:
        log("REBALANCE: {} sell orders sent; skip buys until cash/positions refresh".format(
            sell_orders_sent))
        mark_rebalanced(current_date)
        mark_daily_processed(current_date)
        return

    total_asset, asset_ok = safe_account_total_asset(account_id)
    cash, cash_ok = safe_account_available_cash(account_id)
    if total_asset <= 0:
        # Fallback when QMT account detail does not expose total assets.
        total_asset = cash + sum(pos.get("market_value", 0.0) for pos in positions.values())
    if total_asset <= 0:
        log("BLOCKED: account asset is unavailable; skip new buys")
        mark_rebalanced(current_date)
        mark_daily_processed(current_date)
        return
    market_allows_buys = can_open_new_positions()
    buy_enabled = market_allows_buys and cash_ok
    if not buy_enabled:
        reason = "market filter" if not market_allows_buys else "cash query failed"
        log("REBALANCE: sells done, new buys blocked by {}; market={}".format(
            reason, market_reason))
        mark_rebalanced(current_date)
        mark_daily_processed(current_date)
        return

    target_value = total_asset / float(MAX_POSITIONS)
    available_cash = max(0.0, cash * (1.0 - CASH_BUFFER))

    rank_map = dict((row["code"], idx + 1) for idx, row in enumerate(ranked))
    row_map = dict((row["code"], row) for row in ranked)
    drift_values = []
    for code in target_codes:
        row = row_map.get(code)
        if not row:
            continue
        current_value = int(positions.get(code, {}).get("total", 0)) * row["close"]
        drift_values.append("{}:{:.2%}".format(code, current_value / total_asset - 1.0 / MAX_POSITIONS))
    log("REBALANCE: candidates={} blocked={} targets={} total_asset={:.2f} cash={:.2f} market={} asset_ok={}".format(
        len(ranked), format_blocked_counts(blocked_counts), ",".join(target_codes),
        total_asset, cash, market_reason, asset_ok))
    log("RANK TOP10: {}".format(format_ranked(ranked, 10)))
    log("POSITION DRIFT: {}".format(",".join(drift_values) if drift_values else "NONE"))

    for code in target_codes:
        row = row_map.get(code)
        if not row:
            continue
        close = row["close"]
        current_shares = int(positions.get(code, {}).get("total", 0))
        current_value = current_shares * close
        desired_value = target_value
        if MAX_BUY_VALUE_PER_STOCK > 0:
            desired_value = min(desired_value, MAX_BUY_VALUE_PER_STOCK)
        delta_value = desired_value - current_value
        if delta_value <= close * LOT_SIZE:
            log("TARGET {} rank={} no buy needed; current_value={:.2f}".format(
                code, rank_map.get(code, 0), current_value))
            continue
        buy_value = min(delta_value, available_cash)
        volume = buy_lot(buy_value / close)
        if volume < LOT_SIZE:
            log("TARGET {} rank={} insufficient cash; cash_left={:.2f}".format(
                code, rank_map.get(code, 0), available_cash))
            continue
        if submit_order(C, account_id, code, "BUY", volume, close, current_date):
            available_cash -= volume * close * (1.0 + BUY_PRICE_BUFFER)
        log("TARGET {} rank={} momentum60={:.2%}".format(
            code, rank_map.get(code, 0), row["momentum"]))

    mark_rebalanced(current_date)
    mark_daily_processed(current_date)


# ========================= QMT ENTRY POINTS =========================

def init(C):
    account_id = current_account_id()
    configure_state_file(account_id)
    load_state()
    S["account"] = account_id
    S["live"] = bool(LIVE_TRADING)
    save_state()

    if account_id:
        try:
            C.set_account(account_id)
        except Exception as exc:
            log("WARNING: set_account failed: {}".format(exc))
    try:
        C.set_universe([BENCHMARK_CODE])
    except Exception as exc:
        log("WARNING: set_universe failed: {}".format(exc))

    log("initialized: live={} account={} sector={} window={}-{} state={}".format(
        LIVE_TRADING, account_id or "NOT_SET", SECTOR_NAME,
        EXECUTE_AFTER, EXECUTE_BEFORE, STATE_FILE))
    if not LIVE_TRADING:
        log("SAFE MODE: LIVE_TRADING=False; no real order will be sent")


def handlebar(C):
    try:
        if not C.is_last_bar():
            return
    except Exception:
        pass

    current_date = today_text()
    if current_date < TRADE_START:
        return
    if datetime.now().weekday() > 4:
        return
    hhmmss = now_hhmmss()
    if hhmmss >= CLOSING_AUCTION_START:
        return
    if hhmmss < EXECUTE_AFTER or hhmmss > EXECUTE_BEFORE:
        return
    if daily_already_processed(current_date):
        return

    account_id = current_account_id()
    if not account_id:
        log("BLOCKED: set ACCOUNT_ID or provide QMT global variable `account`")
        return

    try:
        execute_strategy(C, account_id, current_date)
    except Exception as exc:
        log("UNEXPECTED ERROR: {}".format(exc))


def order_callback(C, order_info):
    try:
        log("order callback: code={} status={} traded={} price={}".format(
            full_code_from_record(order_info),
            int(attr_number(order_info, ["m_nOrderStatus"], 0)),
            int(attr_number(order_info, ["m_nVolumeTraded", "m_nTradedVolume"], 0)),
            attr_number(order_info, ["m_dPrice", "m_dTradedPrice"], 0.0)))
    except Exception as exc:
        log("order callback parse failed: {}".format(exc))


def deal_callback(C, deal_info):
    try:
        log("deal callback: code={} volume={} price={}".format(
            full_code_from_record(deal_info),
            int(attr_number(deal_info, ["m_nVolume", "m_nTradedVolume"], 0)),
            attr_number(deal_info, ["m_dPrice", "m_dTradedPrice"], 0.0)))
    except Exception as exc:
        log("deal callback parse failed: {}".format(exc))
