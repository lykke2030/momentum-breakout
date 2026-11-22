@echo off
title VTG intraday breakout backtest
cd /d "%~dp0"
set PY=python
where python >nul 2>nul || set PY=py

echo Installing packages (alpaca-py, pandas, numpy)...
%PY% -m pip install --quiet --upgrade alpaca-py pandas numpy

if not defined APCA_API_KEY_ID set /p APCA_API_KEY_ID=Paste your Alpaca API key ID and press Enter: 
if not defined APCA_API_SECRET_KEY set /p APCA_API_SECRET_KEY=Paste your Alpaca secret key and press Enter: 

echo.
echo Downloading 5-min bars for the universe in tickers.txt and running the backtest.
echo This takes roughly 10-20 minutes. Progress is written to run_log.txt.
%PY% -u vtg_edge.py --universe-file tickers.txt --days 365 --ablate > run_log.txt 2>&1
type run_log.txt
echo.
echo ===== Done. Results are in vtg_edge\results\summary.md =====
pause
