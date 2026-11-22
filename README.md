# Momentum → Base → Breakout: an intraday backtest

A static website that presents an intraday backtest of a "VTG Setup"-style pattern:

1. **Prior momentum**: a gap up, a thrust bar or orderly persistent buying
2. **Base and contraction**: a controlled pullback, then tight bars
3. **Range expansion**: a breakout that closes near its high

The test uses Alpaca SIP data, 5-minute bars, signals confirmed on the bar close, entries at the next open, 5 bps of slippage per side and an out-of-sample hold-out.

**Result:** no tradable edge on intraday bars. The full setup averages about −0.10R per trade after costs, both in-sample and out of sample.

Live site: https://lykke2030.github.io/momentum-breakout/

## Pages and files

### Signal gallery: `signals/index.html`

Live: https://lykke2030.github.io/momentum-breakout/signals/

One card per trade, each showing the full 09:30–16:00 ET session. The page re-runs the backtest in the browser from the bundled 5-minute bars, a JavaScript port of `vtg_edge.py` that matches the Python results trade for trade (276 trades for the 116-stock set, 81 for the mega caps).

- **Cards:** entry ▲, exit ✕, stop (dashed red), the trade window shaded green or red, and the strategy's own levels in dashed gold: momentum high, contraction box and breakout trigger. Also EMA 9/20/50, session VWAP, AVWAP from the prior day's open, prior day high/low/close, volume, the 30-minute opening range, and a daily chart (SMA 20/50/200) with the 10 sessions after the trade.
- **Filters:** dataset, symbol, era, year, gap, day type and result. Sort orders include a reshuffle. Jump to a date, change the candle size, and switch each indicator on or off. 48 cards per page; charts draw as they scroll into view.
- **Full view:** trade details, then the strategy levels and the day's numbers, a large chart with a hover readout and a 90/20-session daily chart. Step through trades with ← → and close with Esc.
- **Review:** mark trades ✓ / ✗ with the buttons, the Y / N keys or the tag on a card. Marks are saved in your browser's localStorage under `momentum_breakout_signals_review_v1`.
- **Data:** `data/gallery/*_bars.bin.gz` and `*_meta.json`. These are packed regular-session bars: 5-minute bars for both datasets, plus 1-minute bars for the mega-cap trade days, which enable 1m/2m candles there. The 116-stock set only has 5-minute bars from Sep 2025, so its 1m/2m buttons are off and its daily SMAs start late.

### Results page and files


| Path | What it is |
|---|---|
| `signals/index.html` | The signal gallery (above). |
| `data/gallery/` | Packed bar data for the gallery |
| `index.html` | The results page. Single page: verdict, how the setup works, cumulative R chart, ablation (which part matters), breakdowns, costs vs stop width, a sortable list of every trade, method and downloads. A toggle switches between the two datasets. |
| `data/summary.json` | Parameters, test periods, out-of-sample start dates and random-entry baselines |
| `data/trades_broad.csv` | Every trade, 116 momentum and growth stocks, Oct 2025 – Nov 2026 |
| `data/trades_megacap.csv` | Every trade, 10 mega caps and index ETFs, 2021 – 2026 |
| `data/ablation_*.csv` | Results with each part of the setup switched off in turn |
| `data/vtg_edge.py` | The backtester (Python: alpaca-py, pandas, numpy) |
| `data/run_vtg.bat` | Windows one-click launcher; asks for your Alpaca keys |
| `data/tickers.txt` | The 116-stock universe |

The site is plain HTML, CSS and JavaScript with no build step and no external libraries. It loads its data with relative paths, so it works on GitHub Pages or any static server. Opening `index.html` straight from disk won't load the data. Use `python -m http.server` in this folder instead.

## Rerun the backtest

```bash
pip install alpaca-py pandas numpy
export APCA_API_KEY_ID=...  APCA_API_SECRET_KEY=...
python data/vtg_edge.py --universe-file data/tickers.txt --days 365 --ablate
```

For research and education only, not financial advice.
