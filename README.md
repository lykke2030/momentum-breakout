# Momentum → Base → Breakout: an intraday backtest

A static website that presents an intraday backtest of a "VTG Setup"-style pattern:

1. **Prior momentum**: a gap up, a thrust bar or orderly persistent buying
2. **Base and contraction**: a controlled pullback, then tight bars
3. **Range expansion**: a breakout that closes near its high

The test uses Alpaca SIP data, 5-minute bars, signals confirmed on the bar close, entries at the next open, 5 bps of slippage per side and an out-of-sample hold-out.

**Result:** no tradable edge on intraday bars. The full setup averages about −0.10R per trade after costs, both in-sample and out of sample.

Live site: https://lykke2030.github.io/momentum-breakout/

## Pages and files

| Path | What it is |
|---|---|
| `index.html` | The site. Single page: verdict, how the setup works, cumulative R chart, ablation (which part matters), breakdowns, costs vs stop width, a sortable list of every trade, method and downloads. A toggle switches between the two datasets. |
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
