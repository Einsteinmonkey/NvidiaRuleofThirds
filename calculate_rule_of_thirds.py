#!/usr/bin/env python3
"""
NVDA Rule of Thirds 4-hour candle calculator.

Fetches 60-minute OHLC candles for NVIDIA (NVDA), aggregates regular-session
bars into 4-hour candles, uses only fully closed candles, calculates Rule of
Thirds levels, and writes:
- index.html
- results/latest.md
- results/last_10.md
- results/history.csv
- results/chart_data.json

No API key required. Yahoo Finance chart data is used for the calculated table.
The TradingView embed in index.html provides the live 4-hour chart.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
RESULTS_DIR = ROOT / "results"
INDEX_PATH = ROOT / "index.html"
NY_TZ = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
FOUR_HOURS_MINUTES = 240
CLOSE_BUFFER_MINUTES = 5


@dataclass(frozen=True)
class RawCandle:
    start_et: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


@dataclass(frozen=True)
class Candle:
    label: str
    date: str
    start_et: datetime
    end_et: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    source: str = ""


@dataclass(frozen=True)
class RuleRow:
    candle: Candle
    range_value: float
    one_third: float
    level_1: float
    level_2_middle: float
    level_3_high_average: float


def http_get(url: str, timeout: int = 30) -> bytes:
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; NVDA-4H-Rule-of-Thirds/1.0)",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urlopen(req, timeout=timeout) as response:  # nosec B310 - public market-data fetch
        status = getattr(response, "status", 200)
        if status >= 400:
            raise RuntimeError(f"HTTP {status} fetching {url}")
        return response.read()


def clean_number(value) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def fetch_yahoo_hourly(symbol: str, range_value: str = "730d") -> List[RawCandle]:
    """Fetch 60-minute candles from Yahoo Finance chart endpoint."""
    params = urlencode(
        {
            "range": range_value,
            "interval": "60m",
            "includePrePost": "false",
            "includeAdjustedClose": "true",
            "events": "div,splits",
        }
    )
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{params}"
    payload = json.loads(http_get(url).decode("utf-8"))

    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        error = payload.get("chart", {}).get("error")
        raise RuntimeError(f"Yahoo returned no result for {symbol}: {error}")

    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [None])[0]
    if not timestamps or not quote:
        raise RuntimeError(f"Yahoo returned incomplete intraday data for {symbol}")

    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    candles: List[RawCandle] = []
    for i, ts in enumerate(timestamps):
        o = clean_number(opens[i] if i < len(opens) else None)
        h = clean_number(highs[i] if i < len(highs) else None)
        l = clean_number(lows[i] if i < len(lows) else None)
        c = clean_number(closes[i] if i < len(closes) else None)
        if None in (o, h, l, c):
            continue
        start_et = datetime.fromtimestamp(int(ts), timezone.utc).astimezone(NY_TZ)
        volume = int(clean_number(volumes[i] if i < len(volumes) else 0) or 0)
        candles.append(RawCandle(start_et=start_et, open=o, high=h, low=l, close=c, volume=volume))

    if not candles:
        raise RuntimeError(f"Yahoo returned no usable hourly candles for {symbol}")

    return sorted(candles, key=lambda x: x.start_et)


def market_session_bounds(dt: datetime) -> Tuple[datetime, datetime]:
    open_dt = dt.replace(hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute, second=0, microsecond=0)
    close_dt = dt.replace(hour=SESSION_CLOSE.hour, minute=SESSION_CLOSE.minute, second=0, microsecond=0)
    return open_dt, close_dt


def regular_session_bucket(raw: RawCandle) -> Optional[Tuple[datetime, datetime]]:
    """Return the 4-hour regular-session bucket for a raw candle, or None."""
    start = raw.start_et
    session_open, session_close = market_session_bounds(start)
    if start < session_open or start >= session_close:
        return None

    minutes_since_open = int((start - session_open).total_seconds() // 60)
    bucket_index = minutes_since_open // FOUR_HOURS_MINUTES
    bucket_start = session_open + timedelta(minutes=FOUR_HOURS_MINUTES * bucket_index)
    bucket_end = min(bucket_start + timedelta(minutes=FOUR_HOURS_MINUTES), session_close)
    return bucket_start, bucket_end


def aggregate_to_four_hour(raw_candles: Iterable[RawCandle]) -> List[Candle]:
    grouped: Dict[Tuple[str, str], List[RawCandle]] = {}
    bounds: Dict[Tuple[str, str], Tuple[datetime, datetime]] = {}

    for raw in raw_candles:
        bucket = regular_session_bucket(raw)
        if bucket is None:
            continue
        bucket_start, bucket_end = bucket
        key = (bucket_start.date().isoformat(), bucket_start.isoformat())
        grouped.setdefault(key, []).append(raw)
        bounds[key] = (bucket_start, bucket_end)

    candles: List[Candle] = []
    for key in sorted(grouped.keys(), key=lambda k: bounds[k][0]):
        bucket_rows = sorted(grouped[key], key=lambda x: x.start_et)
        bucket_start, bucket_end = bounds[key]
        label = f"{bucket_start.date().isoformat()} {bucket_start:%H:%M}-{bucket_end:%H:%M} ET"
        candles.append(
            Candle(
                label=label,
                date=bucket_start.date().isoformat(),
                start_et=bucket_start,
                end_et=bucket_end,
                open=bucket_rows[0].open,
                high=max(row.high for row in bucket_rows),
                low=min(row.low for row in bucket_rows),
                close=bucket_rows[-1].close,
                volume=sum(row.volume for row in bucket_rows),
                source="Yahoo Finance 60m candles aggregated to 4h regular-session candles",
            )
        )

    if not candles:
        raise RuntimeError("Could not aggregate any regular-session 4-hour candles.")
    return candles


def closed_candles(candles: Iterable[Candle]) -> List[Candle]:
    """Only include candles whose 4-hour bucket has fully closed."""
    now_et = datetime.now(NY_TZ)
    cutoff = now_et - timedelta(minutes=CLOSE_BUFFER_MINUTES)
    return [c for c in candles if c.end_et <= cutoff]


def calculate_row(candle: Candle) -> RuleRow:
    range_value = candle.high - candle.low
    one_third = range_value / 3
    level_1 = candle.low + one_third
    level_2 = candle.low + (one_third * 2)
    level_3 = candle.low + (one_third * 3)
    return RuleRow(
        candle=candle,
        range_value=range_value,
        one_third=one_third,
        level_1=level_1,
        level_2_middle=level_2,
        level_3_high_average=level_3,
    )


def fmt_price(value: float) -> str:
    return f"${value:,.2f}"


def csv_escape(value: str) -> str:
    return value.replace('"', '""')


def ema(values: List[float], period: int) -> List[Optional[float]]:
    if not values or period <= 0:
        return [None] * len(values)
    multiplier = 2 / (period + 1)
    result: List[Optional[float]] = []
    prev: Optional[float] = None
    for idx, value in enumerate(values):
        if idx < period - 1:
            result.append(None)
            continue
        if idx == period - 1:
            prev = sum(values[:period]) / period
        else:
            assert prev is not None
            prev = (value - prev) * multiplier + prev
        result.append(prev)
    return result


def build_chart_data(candles: List[Candle]) -> List[dict]:
    closes = [c.close for c in candles]
    ema_9 = ema(closes, 9)
    ema_20 = ema(closes, 20)
    ema_50 = ema(closes, 50)
    ema_200 = ema(closes, 200)
    ema_12 = ema(closes, 12)
    ema_26 = ema(closes, 26)

    macd_line: List[Optional[float]] = []
    for fast, slow in zip(ema_12, ema_26):
        macd_line.append(None if fast is None or slow is None else fast - slow)

    macd_signal: List[Optional[float]] = [None] * len(macd_line)
    valid_indices = [i for i, value in enumerate(macd_line) if value is not None]
    valid_values = [macd_line[i] for i in valid_indices if macd_line[i] is not None]
    valid_signal = ema([float(v) for v in valid_values], 9)
    for idx, signal_value in zip(valid_indices, valid_signal):
        macd_signal[idx] = signal_value

    rows = []
    for i, candle in enumerate(candles):
        signal_value = macd_signal[i]
        macd_value = macd_line[i]
        histogram = None if macd_value is None or signal_value is None else macd_value - signal_value
        rows.append(
            {
                "label": candle.label,
                "date": candle.date,
                "startEt": candle.start_et.isoformat(),
                "endEt": candle.end_et.isoformat(),
                "open": round(candle.open, 4),
                "high": round(candle.high, 4),
                "low": round(candle.low, 4),
                "close": round(candle.close, 4),
                "volume": candle.volume,
                "ema9": None if ema_9[i] is None else round(float(ema_9[i]), 4),
                "ema20": None if ema_20[i] is None else round(float(ema_20[i]), 4),
                "ema50": None if ema_50[i] is None else round(float(ema_50[i]), 4),
                "ema200": None if ema_200[i] is None else round(float(ema_200[i]), 4),
                "macd": None if macd_value is None else round(float(macd_value), 4),
                "macdSignal": None if signal_value is None else round(float(signal_value), 4),
                "macdHist": None if histogram is None else round(float(histogram), 4),
            }
        )
    return rows


def render_latest_md(symbol: str, latest: RuleRow, generated_at: str) -> str:
    c = latest.candle
    return f"""# {symbol} 4H Rule of Thirds

Latest fully closed 4-hour candle: **{c.label}**

| Field | Value |
|---|---:|
| Open | {fmt_price(c.open)} |
| Low | {fmt_price(c.low)} |
| High | {fmt_price(c.high)} |
| Close | {fmt_price(c.close)} |
| Range | {fmt_price(latest.range_value)} |
| One Third | {fmt_price(latest.one_third)} |
| Level 1 | {fmt_price(latest.level_1)} |
| Level 2 / Middle | {fmt_price(latest.level_2_middle)} |
| Level 3 / High Average | {fmt_price(latest.level_3_high_average)} |

Candle close ET: {c.end_et.isoformat()}  
Source: {c.source}  
Updated UTC: {generated_at}
"""


def render_last_n_md(symbol: str, rows: List[RuleRow]) -> str:
    lines = [
        f"# {symbol} 4H Rule of Thirds - Last {len(rows)} Closed Candles",
        "",
        "| Candle | Low | High | Range | One Third | Level 1 | Level 2 / Middle | Level 3 / High Avg |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        c = row.candle
        lines.append(
            f"| {c.label} | {fmt_price(c.low)} | {fmt_price(c.high)} | {fmt_price(row.range_value)} | "
            f"{fmt_price(row.one_third)} | {fmt_price(row.level_1)} | {fmt_price(row.level_2_middle)} | {fmt_price(row.level_3_high_average)} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_history_csv(rows: List[RuleRow], generated_at: str) -> str:
    header = [
        "generated_at_utc",
        "candle_label",
        "date",
        "start_et",
        "end_et",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "range",
        "one_third",
        "level_1",
        "level_2_middle",
        "level_3_high_average",
        "source",
    ]
    out = [",".join(header)]
    for row in rows:
        c = row.candle
        values = [
            generated_at,
            c.label,
            c.date,
            c.start_et.isoformat(),
            c.end_et.isoformat(),
            f"{c.open:.6f}",
            f"{c.high:.6f}",
            f"{c.low:.6f}",
            f"{c.close:.6f}",
            str(c.volume),
            f"{row.range_value:.6f}",
            f"{row.one_third:.6f}",
            f"{row.level_1:.6f}",
            f"{row.level_2_middle:.6f}",
            f"{row.level_3_high_average:.6f}",
            c.source,
        ]
        out.append(",".join(f'"{csv_escape(v)}"' if "," in v or " " in v else v for v in values))
    out.append("")
    return "\n".join(out)


def render_html(symbol: str, display_symbol: str, rows: List[RuleRow], generated_at: str) -> str:
    latest = rows[-1]
    c = latest.candle

    table_rows = "\n".join(
        f"""
            <tr>
              <td>{html.escape(row.candle.label)}</td>
              <td>{fmt_price(row.candle.low)}</td>
              <td>{fmt_price(row.candle.high)}</td>
              <td>{fmt_price(row.range_value)}</td>
              <td>{fmt_price(row.one_third)}</td>
              <td>{fmt_price(row.level_1)}</td>
              <td>{fmt_price(row.level_2_middle)}</td>
              <td>{fmt_price(row.level_3_high_average)}</td>
            </tr>"""
        for row in reversed(rows)
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{display_symbol} 4H Rule of Thirds</title>
  <style>
    :root {{
      --bg: #0d1117;
      --card: #161b22;
      --card-2: #0f141b;
      --border: #30363d;
      --text: #f0f6fc;
      --muted: #9fb0c3;
      --accent: #58a6ff;
      --good: #3fb950;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: radial-gradient(circle at top, #172133 0, #0d1117 42%, #090c10 100%);
      color: var(--text);
      font-family: Arial, Helvetica, sans-serif;
      padding: 40px 18px;
    }}
    .page {{ max-width: 1120px; margin: 0 auto; }}
    .card {{
      background: rgba(22, 27, 34, 0.94);
      border: 1px solid var(--border);
      border-radius: 22px;
      padding: 28px;
      box-shadow: 0 24px 90px rgba(0, 0, 0, 0.35);
      margin-bottom: 24px;
    }}
    h1 {{ font-size: clamp(34px, 5vw, 56px); line-height: 1; margin: 0 0 10px; }}
    h2 {{ margin: 0 0 18px; font-size: 24px; }}
    .sub {{ color: var(--muted); font-size: 16px; margin-bottom: 26px; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin-bottom: 24px; }}
    .metric {{ background: var(--card-2); border: 1px solid var(--border); border-radius: 16px; padding: 18px; }}
    .metric .label {{ color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; font-size: 13px; }}
    .metric .value {{ font-size: clamp(26px, 4vw, 42px); font-weight: 800; margin-top: 8px; }}
    .latest-table, .history-table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid var(--border); padding: 12px 12px; text-align: right; white-space: nowrap; }}
    th:first-child, td:first-child {{ text-align: left; }}
    th {{ color: var(--muted); font-weight: 700; }}
    .table-wrap {{ overflow-x: auto; }}
    .footer {{ color: var(--muted); font-size: 14px; line-height: 1.45; margin-top: 20px; }}
    .chart-wrap {{ height: 720px; min-height: 520px; border: 1px solid var(--border); border-radius: 18px; overflow: hidden; background: #0b0f14; }}
    .note {{ color: var(--muted); font-size: 14px; margin-top: 12px; }}
    .pill {{ display: inline-block; color: var(--good); background: rgba(63,185,80,.12); border: 1px solid rgba(63,185,80,.35); padding: 5px 10px; border-radius: 999px; font-size: 13px; margin-left: 8px; vertical-align: middle; }}
    @media (max-width: 900px) {{ .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} .card {{ padding: 20px; }} }}
    @media (max-width: 520px) {{ .grid {{ grid-template-columns: 1fr; }} body {{ padding: 22px 12px; }} }}
  </style>
</head>
<body>
  <main class="page">
    <section class="card">
      <h1>{display_symbol} 4H Rule of Thirds</h1>
      <div class="sub">Latest fully closed 4-hour candle: <strong>{html.escape(c.label)}</strong><span class="pill">Auto-updated</span></div>

      <div class="grid">
        <div class="metric"><div class="label">Low</div><div class="value">{fmt_price(c.low)}</div></div>
        <div class="metric"><div class="label">High</div><div class="value">{fmt_price(c.high)}</div></div>
        <div class="metric"><div class="label">Open</div><div class="value">{fmt_price(c.open)}</div></div>
        <div class="metric"><div class="label">Close</div><div class="value">{fmt_price(c.close)}</div></div>
      </div>

      <table class="latest-table">
        <thead><tr><th>Result</th><th>Price</th></tr></thead>
        <tbody>
          <tr><td>Range</td><td>{fmt_price(latest.range_value)}</td></tr>
          <tr><td>One Third</td><td>{fmt_price(latest.one_third)}</td></tr>
          <tr><td>Level 1</td><td>{fmt_price(latest.level_1)}</td></tr>
          <tr><td>Level 2 / Middle</td><td>{fmt_price(latest.level_2_middle)}</td></tr>
          <tr><td>Level 3 / High Average</td><td>{fmt_price(latest.level_3_high_average)}</td></tr>
        </tbody>
      </table>

      <div class="footer">
        Candle close ET: {html.escape(c.end_et.isoformat())}<br />
        Source: {html.escape(c.source)}<br />
        Last updated UTC: {html.escape(generated_at)}
      </div>
    </section>

    <section class="card">
      <h2>Most recent {len(rows)} closed 4-hour candles</h2>
      <div class="table-wrap">
        <table class="history-table">
          <thead>
            <tr>
              <th>Candle</th><th>Low</th><th>High</th><th>Range</th><th>One Third</th><th>Level 1</th><th>Level 2 / Middle</th><th>Level 3 / High Avg</th>
            </tr>
          </thead>
          <tbody>
            {table_rows}
          </tbody>
        </table>
      </div>
    </section>

    <section class="card">
      <h2>Live {display_symbol} 4H chart with EMA 9 / 20 / 50 / 200 and MACD</h2>
      <div class="chart-wrap">
        <div class="tradingview-widget-container" style="height:100%;width:100%">
          <div class="tradingview-widget-container__widget" style="height:calc(100% - 32px);width:100%"></div>
          <div class="tradingview-widget-copyright"><a href="https://www.tradingview.com/symbols/NASDAQ-NVDA/" rel="noopener nofollow" target="_blank"><span class="blue-text">NVDA chart</span></a><span class="trademark"> by TradingView</span></div>
          <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js" async>
          {{
            "autosize": true,
            "symbol": "NASDAQ:NVDA",
            "interval": "240",
            "timezone": "America/New_York",
            "theme": "dark",
            "style": "1",
            "locale": "en",
            "backgroundColor": "rgba(13, 17, 23, 1)",
            "gridColor": "rgba(48, 54, 61, 0.45)",
            "withdateranges": true,
            "allow_symbol_change": false,
            "save_image": true,
            "hide_side_toolbar": false,
            "hide_top_toolbar": false,
            "hide_legend": false,
            "hide_volume": false,
            "calendar": false,
            "details": true,
            "studies": [
              {{ "id": "MAExp@tv-basicstudies", "inputs": {{ "length": 9 }} }},
              {{ "id": "MAExp@tv-basicstudies", "inputs": {{ "length": 20 }} }},
              {{ "id": "MAExp@tv-basicstudies", "inputs": {{ "length": 50 }} }},
              {{ "id": "MAExp@tv-basicstudies", "inputs": {{ "length": 200 }} }},
              {{ "id": "MACD@tv-basicstudies" }}
            ]
          }}
          </script>
        </div>
      </div>
      <div class="note">The Rule of Thirds table uses closed 4-hour regular-session candles aggregated from Yahoo Finance 60-minute data. The live chart is provided by TradingView and may move during market hours.</div>
    </section>
  </main>
</body>
</html>
"""


def render_placeholder_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>NVDA 4H Rule of Thirds</title>
  <style>
    :root { --bg:#0d1117; --card:#161b22; --card-2:#0f141b; --border:#30363d; --text:#f0f6fc; --muted:#9fb0c3; }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; background:radial-gradient(circle at top,#172133 0,#0d1117 42%,#090c10 100%); color:var(--text); font-family:Arial,Helvetica,sans-serif; padding:40px 18px; }
    .page { max-width:1120px; margin:0 auto; }
    .card { background:rgba(22,27,34,.94); border:1px solid var(--border); border-radius:22px; padding:28px; box-shadow:0 24px 90px rgba(0,0,0,.35); margin-bottom:24px; }
    h1 { font-size:clamp(34px,5vw,56px); line-height:1; margin:0 0 10px; }
    h2 { margin:0 0 18px; font-size:24px; }
    .sub { color:var(--muted); font-size:16px; margin-bottom:20px; }
    .empty { background:var(--card-2); border:1px solid var(--border); border-radius:16px; padding:22px; color:var(--muted); line-height:1.5; }
    .chart-wrap { height:720px; min-height:520px; border:1px solid var(--border); border-radius:18px; overflow:hidden; background:#0b0f14; }
    .note { color:var(--muted); font-size:14px; margin-top:12px; }
  </style>
</head>
<body>
  <main class="page">
    <section class="card">
      <h1>NVDA 4H Rule of Thirds</h1>
      <div class="sub">NVIDIA stock 4-hour candles</div>
      <div class="empty">No result yet. Run the GitHub Action once and this page will update automatically with the latest 4-hour result and the most recent 10 closed 4-hour candles.</div>
    </section>
    <section class="card">
      <h2>Live NVDA 4H chart with EMA 9 / 20 / 50 / 200 and MACD</h2>
      <div class="chart-wrap">
        <div class="tradingview-widget-container" style="height:100%;width:100%">
          <div class="tradingview-widget-container__widget" style="height:calc(100% - 32px);width:100%"></div>
          <div class="tradingview-widget-copyright"><a href="https://www.tradingview.com/symbols/NASDAQ-NVDA/" rel="noopener nofollow" target="_blank"><span class="blue-text">NVDA chart</span></a><span class="trademark"> by TradingView</span></div>
          <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js" async>
          {
            "autosize": true,
            "symbol": "NASDAQ:NVDA",
            "interval": "240",
            "timezone": "America/New_York",
            "theme": "dark",
            "style": "1",
            "locale": "en",
            "backgroundColor": "rgba(13, 17, 23, 1)",
            "gridColor": "rgba(48, 54, 61, 0.45)",
            "withdateranges": true,
            "allow_symbol_change": false,
            "save_image": true,
            "hide_side_toolbar": false,
            "hide_top_toolbar": false,
            "hide_legend": false,
            "hide_volume": false,
            "calendar": false,
            "details": true,
            "studies": [
              { "id": "MAExp@tv-basicstudies", "inputs": { "length": 9 } },
              { "id": "MAExp@tv-basicstudies", "inputs": { "length": 20 } },
              { "id": "MAExp@tv-basicstudies", "inputs": { "length": 50 } },
              { "id": "MAExp@tv-basicstudies", "inputs": { "length": 200 } },
              { "id": "MACD@tv-basicstudies" }
            ]
          }
          </script>
        </div>
      </div>
      <div class="note">The Rule of Thirds table updates after the automation runs. The live chart is provided by TradingView.</div>
    </section>
  </main>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Calculate NVDA 4-hour Rule of Thirds levels.")
    parser.add_argument("--symbol", default="NVDA", help="Stock ticker symbol, default NVDA")
    parser.add_argument("--days", type=int, default=10, help="Number of closed 4-hour candles to display")
    parser.add_argument("--chart-days", type=int, default=320, help="Number of 4-hour candles to export for chart_data.json")
    args = parser.parse_args()

    symbol = args.symbol.upper().strip()
    display_symbol = symbol

    raw = fetch_yahoo_hourly(symbol)
    candles = aggregate_to_four_hour(raw)
    closed = closed_candles(candles)
    if len(closed) < args.days:
        raise RuntimeError(f"Only found {len(closed)} closed 4-hour candles for {symbol}; need {args.days}.")

    last_n_candles = closed[-args.days :]
    rows = [calculate_row(c) for c in last_n_candles]
    chart_candles = closed[-args.chart_days :]

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    RESULTS_DIR.mkdir(exist_ok=True)

    (RESULTS_DIR / "latest.md").write_text(render_latest_md(display_symbol, rows[-1], generated_at), encoding="utf-8")
    (RESULTS_DIR / "last_10.md").write_text(render_last_n_md(display_symbol, rows), encoding="utf-8")
    (RESULTS_DIR / "history.csv").write_text(render_history_csv(rows, generated_at), encoding="utf-8")
    (RESULTS_DIR / "chart_data.json").write_text(json.dumps(build_chart_data(chart_candles), indent=2), encoding="utf-8")
    INDEX_PATH.write_text(render_html(symbol, display_symbol, rows, generated_at), encoding="utf-8")

    print(f"Updated {INDEX_PATH.name} and results for {display_symbol} 4H using {rows[-1].candle.source}.")
    print(f"Latest closed 4H candle: {rows[-1].candle.label}")
    print(f"Low: {fmt_price(rows[-1].candle.low)} | High: {fmt_price(rows[-1].candle.high)}")
    print(f"Level 1: {fmt_price(rows[-1].level_1)}")
    print(f"Level 2 / Middle: {fmt_price(rows[-1].level_2_middle)}")
    print(f"Level 3 / High Average: {fmt_price(rows[-1].level_3_high_average)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
