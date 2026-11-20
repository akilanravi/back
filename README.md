# ONE Strategy — 5-minute CE + PE underlying backtest

This project **replaces the 27-strategy/5-exit project** with one 7-condition setup mirrored for bullish CE and bearish PE signals. **It does not buy or backtest option contracts**; returns are from the underlying stock/index with hypothetical cash-equity-style sizing. No order placement.

## Entry — completed 5-minute candle

All conditions must be true simultaneously. Dhan candles are timestamped at **candle opening**. The scanner evaluates the completed candle, then enters at the **next 5-minute candle open**.

| Filter | PE (from provided Streak screenshot) | CE (mirror proposed) |
|---|---|---|
| 1 | Close < session VWAP | Close > session VWAP |
| 2 | EMA(close,9) < EMA(close,21) | EMA9 > EMA21 |
| 3 | Close < Supertrend(10,2) | Close > Supertrend(10,2) |
| 4 | RSI(14) < 45 | RSI(14) > 55 |
| 5 | Close < previous candle Low | Close > previous candle High |
| 6 | Volume > 1.5 × SMA(volume,20) | Same |
| 7 | Close < Open | Close > Open |

Additional consistency: ST(10,2) trend direction must agree with price position. Volume average is the **previous 20 completed bars** to avoid self-inclusion; Streak `SMA(volume,20,0)` usually includes the current bar, so Streak and this project may differ on marginal signals. To match Streak literally, change `v.shift(1).rolling(20)` to `v.rolling(20)` in `prepare()`.

## Exit

- Exit when Supertrend(7,2) direction is **opposite** the held CE/PE position, evaluated at the close of a 5-minute candle and executed at the **next candle open**.
- If no reversal occurs, exit at **15:10 IST at the opening price of the 15:10 candle**. This means the position is flat at 15:10, rather than holding until the close of that candle.
- If the 15:10 candle is missing, report `LATE_MISSING_1510` or `MISSING_1510_LAST_CLOSE` to flag imperfect data. Do not treat these as exact-time exits.
- No fixed stop, target, or maximum holding bars. **An unprotected position can suffer large losses or gaps** before the Supertrend reverses.
- Entry signals must be on a bar timestamped 09:30 or later (the 09:30 bar closes at 09:35); entry must occur before 15:10. One open position per symbol at a time; CE and PE can each trigger later the same day.

## Universe and five-year data

- Pulls the **current** NSE F&O stock underlying list by identifying `FUTSTK` contracts in Dhan's public scrip master and matching them to NSE cash equities. It does not trade futures contracts.
- Attempts indices NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50, SENSEX when the master exposes a matching index security ID. Any unresolved index is **reported as skipped**, not silently assigned an invented ID. If needed, create `index_override.json` with confirmed Dhan `security_id`, `exchange_segment`, `instrument` for each index, e.g. `{"NIFTY":{"security_id":"YOUR_VERIFIED_ID","exchange_segment":"IDX_I","instrument":"INDEX"}}`.
- Five-year request: 2021-11-20 through 2026-11-20 in 85-day Dhan API windows. Actual availability depends on Dhan's retention, entitlements and data quality; inspect `coverage.csv` for every symbol.
- **Survivorship bias:** current F&O membership is not historical membership; delisted or formerly eligible stocks are not automatically included. This is not a historical point-in-time F&O universe.
- NSE index candles may have zero or non-tradeable volume; the volume condition will reject them. Index options signals require a **separate volume proxy rule** (such as futures volume) and cannot be claimed to satisfy the screenshot's index-volume filter without one. No fake volume is used.

## GitHub setup

1. Upload the **contents** of this folder to your repo root, replacing the old files. Delete obsolete `lab.py`, `engine.py`, `advanced.py`, `combined.py`, and old tests/workflows to prevent confusion.
2. In repository Settings → Secrets and variables → Actions, retain `DHAN_ACCESS_TOKEN`.
3. Actions → **Single CE PE Strategy - 5 Year 5min** → Run workflow. Leave `symbols` blank and `max_symbols=0` for all currently listed F&O stocks plus resolved indices.
4. For a fast validation run, set `symbols=RELIANCE,SBIN,NIFTY` or `max_symbols=3`.
5. Download `backtest-results` artifact: `universe.csv`, `coverage.csv`, `trades.csv`, `ranking.csv`, `monthly.csv`, `symbol_ranking.csv`.

For a large universe, Dhan request limits or the GitHub job time limit may require splitting symbols into batches. Reports are checkpointed every 10 instruments. This project does not use Yahoo, broker auto-orders, or option premium pricing.

## Research caveats

- `round_trip_cost_bps=12` is a **simple assumed total turnover cost** for comparison, not verified Zerodha/Dhan options brokerage/tax/slippage. No portfolio-wide simultaneous-position cap is enforced: reported P&L sums independent symbol-level trades, **not** a funded ₹1 lakh portfolio.
- No lookahead in the signals: all indicators use the completed bar; order at next open. OHLC-only data cannot reconstruct actual fills/slippage.
- Confirm the Supertrend implementation matches the specific Streak/Dhan platform before using alerts.
