# HS300 Trend Rotation Strategy

Big-QMT built-in Python version of an HS300 trend rotation strategy.

## Files

- `hs300_trend_rotation_bigqmt.py`: QMT strategy source with `init(C)` and `handlebar(C)` entry points.
- `hs300_constituents.csv`: fallback HS300 constituent list used when the QMT sector query returns empty.
- `backtest_hs300_trend_rotation.py`: reproducible public-data backtest script.
- `backtest_summary.json`: latest backtest summary.
- `backtest_equity_curve.csv`: latest backtest equity curve.
- `backtest_trades.csv`: latest backtest trade log.
- `backtest_data_failures.json`: symbols excluded from the latest run and the reason.
- `parameter_scan_hs300.py`: small parameter sensitivity scan for rebalance/rank/MA settings.
- `tests/test_strategy_logic.py`: focused regression tests for execution safeguards.
- `requirements.txt`: dependencies for the public-data research scripts.

## Strategy Summary

- Universe: point-in-time HS300 constituents; live QMT uses its current sector query.
- Trend filter: close above MA250, rising MA250, and MA20 > MA60 > MA250.
- Ranking: 60-day return among eligible stocks.
- Rebalance: every 5 trading days, strictly capped at 5 positions.
- Exit: daily close below MA60, or exit buffer failure on rebalance day.
- Borrowed safety components: HS300 index market filter, ST/STAR-board buy filter, suspended/limit-up/limit-down buy avoidance, liquidity filter, state persistence, active-order checks, and account/position query failure protection.
- Rebalance timing: based on recorded trading dates instead of a simple counter.
- Execution: two-phase sell/confirm/buy flow; available cash is refreshed after sells fill.
- Position ownership: stocks bought by the strategy remain managed even after index removal.
- QMT compatibility: compact order remarks, odd-lot liquidation, retryable rejected/cancelled orders, and strategy-scoped order queries.
- Logging: prints candidate counts, blocked-buy reasons, top rankings, targets, and position drift.
- Sector fallback: if QMT does not return the HS300 sector list near the close, the strategy loads `hs300_constituents.csv` from the strategy directory.

## Safety

`LIVE_TRADING = False` by default. In this mode, the strategy only logs intended orders and does not call live trading orders.

`QMT_BACKTEST_MODE = False` by default. Set it to `True` only for QMT historical replay: the strategy then uses bar timestamps, in-memory state, and `quickTrade=2`. Keep it `False` for simulation and live execution.

The default execution window is 14:50:00 to 14:56:50. The strategy exits before 14:57:00 to avoid the A-share closing call auction.

Before enabling live trading:

1. Run it in big-QMT simulation/log-only mode.
2. Confirm HS300 sector data, daily bars, account, positions, and order queries work in your broker's QMT build.
3. Change `LIVE_TRADING = True` only after validation.

For the public-data research backtest:

```powershell
pip install -r requirements.txt
python backtest_hs300_trend_rotation.py
```

This project is for strategy research and execution testing only. It is not investment advice or a promise of returns.

## Latest Backtest

Public-data backtest window: 2021-09-02 to 2026-09-02, initial capital 1,000,000 CNY.

- Strategy total return: -15.75%; annual return: -3.37%.
- Final equity: 842,527.15 CNY; max drawdown: -52.48%; Sharpe: 0.02.
- Trade count: 472; maximum simultaneous positions: 5.
- HS300 benchmark total return: -6.60%; annual return: -1.36%; max drawdown: -37.86%.
- Historical constituent union: 439 symbols; 438 usable symbols.

## Backtest Limitation

The backtest queries BaoStock point-in-time HS300 snapshots every 20 trading days and forward-fills each snapshot without look-ahead. This removes the previous current-constituent survivorship bias, but index additions and removals can be reflected up to 20 trading days late. Prices use Tencent forward-adjusted daily bars with Eastmoney as fallback. One symbol (`001280`) was excluded because 183 available rows were below the 270-row indicator warm-up requirement.

This is still an approximate daily-bar research backtest. It does not reproduce broker matching, queue priority, intraday limit-state changes, slippage, or tick-level execution, and the negative result shows that the current parameter set should not be used for live trading without further validation.
