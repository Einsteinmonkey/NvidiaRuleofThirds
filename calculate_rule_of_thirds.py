#!/usr/bin/env python3
"""Generate NVDA Rule of Thirds results for 15m, 30m, 1h, and 4h candles.

Data source: Yahoo Finance chart endpoint.
4H candles are built from regular-session 60m Yahoo candles:
  - 09:30–13:30 ET
  - 13:30–16:00 ET

This script writes:
  - index.html
  - results/latest.md
  - results/last_results.md
  - results/history.csv
  - results/data.json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

NY = ZoneInfo("America/New_York") if ZoneInfo else timezone.utc
UTC = timezone.utc

DEFAULT_SYMBOL = "NVDA"
DEFAULT_DAYS = 10
DEFAULT_RECENT_CANDLES = 10
# Optional. Paste your GoCharting NVDA shared chart URL here if you want a link button.
DEFAULT_GOCHARTING_URL = ""

@dataclass
class Candle:
    symbol: str
    timeframe: str
    open_time_utc: str
    close_time_utc: str
    open_time_et: str
    close_time_et: str
    trading_day: str
    open: float
    high: float
    low: float
    close: float
    volume: int

@dataclass
class RuleResult:
    symbol: str
    timeframe: str
    open_time_utc: str
    close_time_utc: str
    open_time_et: str
    close_time_et: str
    trading_day: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    range: float
    one_third: float
    level_1: float
    level_2_middle: float
    level_3_high_average: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--recent-candles", type=int, default=DEFAULT_RECENT_CANDLES)
    parser.add_argument("--gocharting-url", default=DEFAULT_GOCHARTING_URL)
    return parser.parse_args()


def request_json(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; rule-of-thirds-bot/1.0)",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Yahoo Finance HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Yahoo Finance request failed: {e}") from e


def fetch_yahoo_candles(symbol: str, interval: str, range_value: str = "30d") -> List[Candle]:
    params = urllib.parse.urlencode(
        {
            "range": range_value,
            "interval": interval,
            "includePrePost": "false",
            "events": "history",
        }
    )
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?{params}"
    data = request_json(url)

    chart = data.get("chart", {})
    error = chart.get("error")
    if error:
        raise RuntimeError(f"Yahoo Finance returned error: {error}")

    results = chart.get("result") or []
    if not results:
        raise RuntimeError("Yahoo Finance returned no chart result.")

    result = results[0]
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    interval_minutes = interval_to_minutes(interval)
    now_utc = datetime.now(UTC)
    candles: List[Candle] = []

    for i, ts in enumerate(timestamps):
        try:
            o = float(opens[i])
            h = float(highs[i])
            l = float(lows[i])
            c = float(closes[i])
            v_raw = volumes[i]
        except (IndexError, TypeError, ValueError):
            continue

        if any(math.isnan(x) for x in [o, h, l, c]):
            continue

        open_dt_utc = datetime.fromtimestamp(int(ts), UTC)
        close_dt_utc = open_dt_utc + timedelta(minutes=interval_minutes)
        # Skip candles that are not fully closed yet.
        if close_dt_utc > now_utc:
            continue

        open_dt_et = open_dt_utc.astimezone(NY)
        close_dt_et = close_dt_utc.astimezone(NY)

        # Keep only regular session bars. Yahoo includePrePost=false usually does this,
        # but this makes the output more predictable.
        if not is_regular_session_open_time(open_dt_et, interval_minutes):
            continue

        candles.append(
            Candle(
                symbol=symbol.upper(),
                timeframe=interval,
                open_time_utc=open_dt_utc.isoformat(),
                close_time_utc=close_dt_utc.isoformat(),
                open_time_et=open_dt_et.isoformat(),
                close_time_et=close_dt_et.isoformat(),
                trading_day=open_dt_et.date().isoformat(),
                open=o,
                high=h,
                low=l,
                close=c,
                volume=int(v_raw or 0),
            )
        )

    candles.sort(key=lambda x: x.open_time_utc)
    return candles


def interval_to_minutes(interval: str) -> int:
    mapping = {
        "15m": 15,
        "30m": 30,
        "60m": 60,
        "1h": 60,
    }
    if interval not in mapping:
        raise ValueError(f"Unsupported interval: {interval}")
    return mapping[interval]


def is_regular_session_open_time(dt_et: datetime, interval_minutes: int) -> bool:
    # U.S. regular equities session: 09:30 to 16:00 ET, Monday-Friday.
    if dt_et.weekday() >= 5:
        return False
    minutes = dt_et.hour * 60 + dt_et.minute
    session_open = 9 * 60 + 30
    session_close = 16 * 60
    return session_open <= minutes < session_close


def filter_last_trading_days(candles: List[Candle], days: int) -> List[Candle]:
    trading_days: List[str] = []
    for c in reversed(candles):
        if c.trading_day not in trading_days:
            trading_days.append(c.trading_day)
        if len(trading_days) >= days:
            break
    allowed = set(trading_days)
    return [c for c in candles if c.trading_day in allowed]


def aggregate_4h_from_60m(symbol: str, days: int) -> List[Candle]:
    hourly = fetch_yahoo_candles(symbol, "60m", range_value="60d")
    hourly = filter_last_trading_days(hourly, days + 3)

    buckets: Dict[tuple[str, str], List[Candle]] = {}
    for c in hourly:
        dt = datetime.fromisoformat(c.open_time_et)
        minutes = dt.hour * 60 + dt.minute
        day = dt.date().isoformat()
        if 9 * 60 + 30 <= minutes < 13 * 60 + 30:
            block = "AM"
        elif 13 * 60 + 30 <= minutes < 16 * 60:
            block = "PM"
        else:
            continue
        buckets.setdefault((day, block), []).append(c)

    out: List[Candle] = []
    for (day, block), parts in buckets.items():
        parts.sort(key=lambda x: x.open_time_utc)
        if not parts:
            continue
        first = parts[0]
        last = parts[-1]
        open_dt = datetime.fromisoformat(first.open_time_utc)
        if block == "AM":
            close_dt_et = datetime.fromisoformat(day + "T13:30:00").replace(tzinfo=NY)
        else:
            close_dt_et = datetime.fromisoformat(day + "T16:00:00").replace(tzinfo=NY)
        close_dt_utc = close_dt_et.astimezone(UTC)

        # Skip incomplete current 4H block.
        if close_dt_utc > datetime.now(UTC):
            continue

        out.append(
            Candle(
                symbol=symbol.upper(),
                timeframe="4h",
                open_time_utc=open_dt.isoformat(),
                close_time_utc=close_dt_utc.isoformat(),
                open_time_et=datetime.fromisoformat(first.open_time_et).isoformat(),
                close_time_et=close_dt_et.isoformat(),
                trading_day=day,
                open=first.open,
                high=max(p.high for p in parts),
                low=min(p.low for p in parts),
                close=last.close,
                volume=sum(p.volume for p in parts),
            )
        )

    out.sort(key=lambda x: x.open_time_utc)
    return filter_last_trading_days(out, days)


def calculate_rule(c: Candle) -> RuleResult:
    rng = c.high - c.low
    one_third = rng / 3
    level_1 = c.low + one_third
    level_2 = level_1 + one_third
    level_3 = level_2 + one_third
    return RuleResult(
        symbol=c.symbol,
        timeframe=display_timeframe(c.timeframe),
        open_time_utc=c.open_time_utc,
        close_time_utc=c.close_time_utc,
        open_time_et=c.open_time_et,
        close_time_et=c.close_time_et,
        trading_day=c.trading_day,
        open=c.open,
        high=c.high,
        low=c.low,
        close=c.close,
        volume=c.volume,
        range=rng,
        one_third=one_third,
        level_1=level_1,
        level_2_middle=level_2,
        level_3_high_average=level_3,
    )


def display_timeframe(tf: str) -> str:
    return {"15m": "15M", "30m": "30M", "60m": "1H", "1h": "1H", "4h": "4H"}.get(tf, tf.upper())


def fmt_price(value: float) -> str:
    return f"${value:,.2f}"


def fmt_precise(value: float) -> str:
    return f"${value:,.4f}"


def fmt_volume(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def fmt_et(iso_value: str) -> str:
    dt = datetime.fromisoformat(iso_value)
    return dt.strftime("%Y-%m-%d %H:%M ET")


def build_results(symbol: str, days: int, recent_candles: int) -> Dict[str, List[RuleResult]]:
    timeframes = [
        ("15m", lambda: fetch_yahoo_candles(symbol, "15m", range_value="30d")),
        ("30m", lambda: fetch_yahoo_candles(symbol, "30m", range_value="30d")),
        ("1h", lambda: fetch_yahoo_candles(symbol, "60m", range_value="60d")),
        ("4h", lambda: aggregate_4h_from_60m(symbol, days)),
    ]

    all_results: Dict[str, List[RuleResult]] = {}
    for key, fetcher in timeframes:
        candles = fetcher()
        candles = filter_last_trading_days(candles, days)
        if not candles:
            raise RuntimeError(f"No closed candles found for {symbol} {key}.")
        rules = [calculate_rule(c) for c in candles]
        # Show only the most recent N candles per timeframe for a clean page.
        all_results[display_timeframe(key)] = rules[-recent_candles:]
    return all_results


def write_outputs(all_results: Dict[str, List[RuleResult]], gocharting_url: str) -> None:
    Path("results").mkdir(exist_ok=True)
    generated_at = datetime.now(UTC).isoformat()
    payload = {
        "generated_at_utc": generated_at,
        "results": {k: [asdict(r) for r in v] for k, v in all_results.items()},
        "gocharting_url": gocharting_url,
    }
    Path("results/data.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    Path("index.html").write_text(render_html(all_results, generated_at, gocharting_url), encoding="utf-8")
    Path("results/latest.md").write_text(render_latest_markdown(all_results, generated_at, gocharting_url), encoding="utf-8")
    Path("results/last_results.md").write_text(render_full_markdown(all_results, generated_at), encoding="utf-8")
    write_csv(all_results)


def write_csv(all_results: Dict[str, List[RuleResult]]) -> None:
    rows: List[Dict[str, Any]] = []
    for timeframe, results in all_results.items():
        for r in results:
            rows.append(asdict(r))
    rows.sort(key=lambda r: (r["timeframe"], r["open_time_utc"]))
    if not rows:
        return
    with open("results/history.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def render_html(all_results: Dict[str, List[RuleResult]], generated_at: str, gocharting_url: str) -> str:
    symbol = "NVDA"
    latest_cards = []
    tables = []

    for timeframe in ["15M", "30M", "1H", "4H"]:
        results = all_results.get(timeframe, [])
        if not results:
            continue
        latest = results[-1]
        latest_cards.append(render_latest_card(timeframe, latest))
        tables.append(render_table(timeframe, list(reversed(results))))

    if not latest_cards:
        body = """
        <div class=\"notice\">No result yet. Run the GitHub Action once and this page will update automatically.</div>
        """
    else:
        body = f"""
        <section class=\"latest-grid\">
          {''.join(latest_cards)}
        </section>
        <section class=\"tables\">
          {''.join(tables)}
        </section>
        """

    link_html = ""
    if gocharting_url.strip():
        safe_url = escape(gocharting_url.strip(), quote=True)
        link_html = f"""
        <section class=\"chart-link-card\">
          <h2>GoCharting</h2>
          <p>No chart is embedded on this page. Use the button below to open your NVDA chart in GoCharting.</p>
          <a class=\"button\" href=\"{safe_url}\" target=\"_blank\" rel=\"noopener\">Open chart in GoCharting →</a>
        </section>
        """

    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{symbol} Multi-Timeframe Rule of Thirds</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0b1118;
      --card: #151c25;
      --card-2: #0f151d;
      --border: #2a3442;
      --text: #f4f7fb;
      --muted: #a9b6c8;
      --accent: #8fc7ff;
      --good: #7dffb2;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: radial-gradient(circle at top, #17202d 0, var(--bg) 38%, #080d13 100%);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 36px 22px 56px; }}
    h1 {{ font-size: clamp(2.3rem, 6vw, 5rem); line-height: .95; margin: 0 0 12px; letter-spacing: -0.06em; }}
    .subtitle {{ margin: 0 0 26px; color: var(--muted); font-size: 1.05rem; }}
    .latest-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin-bottom: 22px; }}
    .latest-card, .table-card, .chart-link-card, .notice {{
      background: rgba(21, 28, 37, .9);
      border: 1px solid var(--border);
      border-radius: 20px;
      box-shadow: 0 18px 60px rgba(0,0,0,.25);
    }}
    .latest-card {{ padding: 18px; }}
    .tf {{ color: var(--accent); font-weight: 800; font-size: .86rem; letter-spacing: .08em; text-transform: uppercase; }}
    .date {{ color: var(--muted); font-size: .86rem; margin-top: 5px; min-height: 38px; }}
    .big {{ font-size: 2rem; font-weight: 800; letter-spacing: -0.04em; margin: 14px 0 2px; }}
    .metric-row {{ display:flex; justify-content:space-between; gap:10px; border-top:1px solid var(--border); padding-top:10px; margin-top:10px; font-size:.9rem; }}
    .metric-row span:first-child {{ color: var(--muted); }}
    .metric-row strong {{ white-space: nowrap; }}
    .tables {{ display: grid; grid-template-columns: 1fr; gap: 18px; }}
    .table-card {{ overflow: hidden; }}
    .table-head {{ display: flex; justify-content: space-between; gap: 18px; align-items: baseline; padding: 20px 22px; border-bottom: 1px solid var(--border); }}
    .table-head h2 {{ margin: 0; font-size: 1.25rem; }}
    .table-head p {{ margin: 0; color: var(--muted); font-size: .9rem; }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 1060px; }}
    th, td {{ text-align: right; padding: 12px 14px; border-bottom: 1px solid rgba(42,52,66,.72); font-size: .92rem; }}
    th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) {{ text-align: left; }}
    th {{ color: var(--muted); font-weight: 700; background: rgba(15,21,29,.8); position: sticky; top: 0; }}
    tr:last-child td {{ border-bottom: 0; }}
    .highlight {{ color: var(--good); font-weight: 800; }}
    .chart-link-card {{ padding: 22px; margin-top: 22px; }}
    .chart-link-card h2 {{ margin: 0 0 8px; }}
    .chart-link-card p {{ color: var(--muted); margin: 0 0 16px; }}
    .button {{ display:inline-flex; align-items:center; justify-content:center; padding: 12px 16px; border-radius: 12px; color: #061019; background: var(--accent); font-weight: 800; text-decoration: none; }}
    .notice {{ padding: 22px; color: var(--muted); }}
    footer {{ color: var(--muted); margin-top: 28px; font-size: .9rem; }}
    @media (max-width: 960px) {{ .latest-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
    @media (max-width: 600px) {{ .latest-grid {{ grid-template-columns: 1fr; }} main {{ padding: 28px 14px 44px; }} }}
  </style>
</head>
<body>
  <main>
    <h1>{symbol} Rule of Thirds</h1>
    <p class=\"subtitle\">15M, 30M, 1H, and 4H candles. Results update automatically from closed regular-session candles.</p>
    {body}
    {link_html}
    <footer>
      Last generated UTC: {escape(generated_at)} · Data source: Yahoo Finance chart data · Not financial advice.
    </footer>
  </main>
</body>
</html>
"""


def render_latest_card(timeframe: str, r: RuleResult) -> str:
    return f"""
    <article class=\"latest-card\">
      <div class=\"tf\">{escape(timeframe)}</div>
      <div class=\"date\">{escape(fmt_et(r.open_time_et))}<br>to {escape(fmt_et(r.close_time_et))}</div>
      <div class=\"big\">{fmt_price(r.close)}</div>
      <div class=\"metric-row\"><span>Low</span><strong>{fmt_price(r.low)}</strong></div>
      <div class=\"metric-row\"><span>High</span><strong>{fmt_price(r.high)}</strong></div>
      <div class=\"metric-row\"><span>One third</span><strong>{fmt_precise(r.one_third)}</strong></div>
      <div class=\"metric-row\"><span>Middle</span><strong class=\"highlight\">{fmt_precise(r.level_2_middle)}</strong></div>
    </article>
    """


def render_table(timeframe: str, results: List[RuleResult]) -> str:
    rows = []
    for r in results:
        rows.append(
            f"""
            <tr>
              <td>{escape(fmt_et(r.open_time_et))}</td>
              <td>{escape(fmt_et(r.close_time_et))}</td>
              <td>{fmt_price(r.low)}</td>
              <td>{fmt_price(r.high)}</td>
              <td>{fmt_precise(r.range)}</td>
              <td>{fmt_precise(r.one_third)}</td>
              <td>{fmt_precise(r.level_1)}</td>
              <td class=\"highlight\">{fmt_precise(r.level_2_middle)}</td>
              <td>{fmt_precise(r.level_3_high_average)}</td>
              <td>{fmt_price(r.close)}</td>
              <td>{fmt_volume(r.volume)}</td>
            </tr>
            """
        )
    return f"""
    <section class=\"table-card\">
      <div class=\"table-head\">
        <h2>{escape(timeframe)} Rule of Thirds</h2>
        <p>Most recent {len(results)} fully closed candles</p>
      </div>
      <div class=\"table-wrap\">
        <table>
          <thead>
            <tr>
              <th>Open ET</th>
              <th>Close ET</th>
              <th>Low</th>
              <th>High</th>
              <th>Range</th>
              <th>One third</th>
              <th>Level 1</th>
              <th>Level 2 / Middle</th>
              <th>Level 3 / High avg</th>
              <th>Close</th>
              <th>Volume</th>
            </tr>
          </thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
      </div>
    </section>
    """


def render_latest_markdown(all_results: Dict[str, List[RuleResult]], generated_at: str, gocharting_url: str) -> str:
    lines = ["# NVDA Multi-Timeframe Rule of Thirds", "", f"Generated UTC: {generated_at}", ""]
    for timeframe in ["15M", "30M", "1H", "4H"]:
        results = all_results.get(timeframe, [])
        if not results:
            continue
        r = results[-1]
        lines.extend(
            [
                f"## {timeframe}",
                f"Open ET: {fmt_et(r.open_time_et)}",
                f"Close ET: {fmt_et(r.close_time_et)}",
                f"Low: {fmt_price(r.low)}",
                f"High: {fmt_price(r.high)}",
                f"Range: {fmt_precise(r.range)}",
                f"One third: {fmt_precise(r.one_third)}",
                f"Level 1: {fmt_precise(r.level_1)}",
                f"Level 2 / Middle: {fmt_precise(r.level_2_middle)}",
                f"Level 3 / High average: {fmt_precise(r.level_3_high_average)}",
                "",
            ]
        )
    if gocharting_url.strip():
        lines.extend([f"GoCharting: {gocharting_url.strip()}", ""])
    return "\n".join(lines)


def render_full_markdown(all_results: Dict[str, List[RuleResult]], generated_at: str) -> str:
    lines = ["# NVDA Recent Rule of Thirds Results", "", f"Generated UTC: {generated_at}", ""]
    for timeframe in ["15M", "30M", "1H", "4H"]:
        results = list(reversed(all_results.get(timeframe, [])))
        if not results:
            continue
        lines.extend([f"## {timeframe}", "", "| Open ET | Close ET | Low | High | Range | One Third | L1 | L2 / Middle | L3 / High Avg | Close |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"])
        for r in results:
            lines.append(
                f"| {fmt_et(r.open_time_et)} | {fmt_et(r.close_time_et)} | {fmt_price(r.low)} | {fmt_price(r.high)} | {fmt_precise(r.range)} | {fmt_precise(r.one_third)} | {fmt_precise(r.level_1)} | {fmt_precise(r.level_2_middle)} | {fmt_precise(r.level_3_high_average)} | {fmt_price(r.close)} |"
            )
        lines.append("")
    return "\n".join(lines)


def write_placeholder_index() -> None:
    Path("index.html").write_text(render_html({}, datetime.now(UTC).isoformat(), DEFAULT_GOCHARTING_URL), encoding="utf-8")


def main() -> int:
    args = parse_args()
    symbol = args.symbol.upper()
    if symbol != "NVDA":
        print(f"Warning: this page is designed for NVDA, but received symbol {symbol}.")
    try:
        all_results = build_results(symbol, args.days, args.recent_candles)
        write_outputs(all_results, args.gocharting_url)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print("Updated NVDA multi-timeframe Rule of Thirds results.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
