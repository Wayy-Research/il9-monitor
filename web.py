"""
IL-9 Monitor — Mobile-friendly web dashboard.

Single-file FastAPI app that shows positions, market data, resting orders,
and P&L. Auto-refreshes every 10 seconds. Designed for phone screens.

Run locally:  uvicorn web:app --port 8080
Deploy:       Render web service pointing to this file
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)

app = FastAPI(title="IL-9 Monitor")

# Cache to avoid hammering APIs on every page load
_cache: dict[str, Any] = {}
_cache_ts: float = 0
CACHE_TTL = 8  # seconds


async def _fetch_data() -> dict[str, Any]:
    """Fetch all data from Kalshi + Polymarket, with caching."""
    global _cache, _cache_ts

    if time.time() - _cache_ts < CACHE_TTL and _cache:
        return _cache

    from broker import KalshiBroker

    data: dict[str, Any] = {
        "balance": 0,
        "positions": [],
        "resting_orders": [],
        "markets": [],
        "fills": [],
        "polymarket": {},
        "updated": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
        "error": None,
    }

    # Kalshi authenticated data
    try:
        async with KalshiBroker() as broker:
            bal = await broker.get_balance()
            data["balance"] = bal.get("balance", 0) / 100.0

            resp = await broker._trading_get("/portfolio/positions")
            data["positions"] = resp.get("market_positions", [])
            data["event_positions"] = resp.get("event_positions", [])

            orders = await broker.get_orders(status="resting")
            data["resting_orders"] = orders

            # Recent fills for IL-9
            fills = await broker.get_fills()
            data["fills"] = [f for f in fills if "KXIL9D" in f.get("ticker", "")][:20]
    except Exception as e:
        data["error"] = str(e)
        logger.error(f"Broker error: {e}")

    # Kalshi market data (public elections API, no auth)
    try:
        import requests as req
        resp = req.get(
            "https://api.elections.kalshi.com/trade-api/v2/markets",
            params={"series_ticker": "KXIL9D", "limit": 20},
            headers={"Accept": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        data["markets"] = resp.json().get("markets", [])
    except Exception as e:
        logger.error(f"Market data error: {e}")

    # Polymarket cross-reference
    try:
        import requests as req
        resp = req.get(
            "https://gamma-api.polymarket.com/events",
            params={"slug": "il-09-democratic-primary-winner", "limit": 1},
            timeout=10,
        )
        events = resp.json()
        if events:
            for m in events[0].get("markets", []):
                q = m.get("question", "")
                prices = m.get("outcomePrices", "[]")
                if isinstance(prices, str):
                    prices = json.loads(prices)
                yes_p = float(prices[0]) if prices else 0
                vol = float(m.get("volume", 0) or 0)
                # Extract candidate name from question
                name = q.replace("Will ", "").replace(" be the Democratic nominee for IL-09?", "")
                if yes_p > 0.001:
                    data["polymarket"][name] = {"price": yes_p, "volume": vol}
    except Exception as e:
        logger.error(f"Polymarket error: {e}")

    _cache = data
    _cache_ts = time.time()
    return data


# Candidate name mapping
NAMES = {
    "KXIL9D-26-MSIM": ("Mike Simmons", "MSIM"),
    "KXIL9D-26-DBIS": ("Daniel Biss", "DBIS"),
    "KXIL9D-26-KA": ("Kat Abughazaleh", "KA"),
    "KXIL9D-26-LFIN": ("Laura Fine", "LFIN"),
    "KXIL9D-26-JS": ("Jan Schakowsky", "JS"),
    "KXIL9D-26-PAND": ("Phil Andrew", "PAND"),
    "KXIL9D-26-BAMI": ("Bushra Amiwala", "BAMI"),
    "KXIL9D-26-HHUY": ("Hoan Huynh", "HHUY"),
}

PRIMARY = datetime(2026, 3, 17, tzinfo=timezone.utc)


def _countdown() -> str:
    delta = PRIMARY - datetime.now(timezone.utc)
    if delta.total_seconds() <= 0:
        return "PRIMARY DAY"
    h = int(delta.total_seconds() // 3600)
    m = int((delta.total_seconds() % 3600) // 60)
    return f"T-{h}h {m}m"


def _pos_row(p: dict) -> str:
    ticker = p.get("ticker", "")
    name, short = NAMES.get(ticker, (ticker, ticker[-4:]))
    pos = float(p.get("position_fp", 0))
    exposure = float(p.get("market_exposure_dollars", 0))
    resting = p.get("resting_orders_count", 0)

    side = "YES" if pos > 0 else "NO"
    side_class = "green" if pos > 0 else "red"
    qty = abs(int(pos))

    return f"""
    <tr>
        <td><b>{short}</b></td>
        <td>{name}</td>
        <td class="{side_class}"><b>{side}</b></td>
        <td>{qty}</td>
        <td>${exposure:.2f}</td>
        <td>{resting}</td>
    </tr>"""


def _order_row(o: dict) -> str:
    ticker = o.get("ticker", "")
    _, short = NAMES.get(ticker, (ticker, ticker[-4:]))
    action = o.get("action", "?")
    side = o.get("side", "?")
    count = o.get("remaining_count", o.get("count_fp", "?"))
    yes_p = o.get("yes_price", "")
    no_p = o.get("no_price", "")
    price_str = f"{yes_p}c" if yes_p else f"{no_p}c NO"

    action_class = "green" if action == "buy" else "red"
    return f"""
    <tr>
        <td>{short}</td>
        <td class="{action_class}">{action.upper()}</td>
        <td>{side.upper()}</td>
        <td>{count}</td>
        <td>{price_str}</td>
    </tr>"""


def _fill_row(f: dict) -> str:
    ticker = f.get("ticker", "")
    _, short = NAMES.get(ticker, (ticker, ticker[-4:]))
    action = f.get("action", "?")
    side = f.get("side", "?")
    count = f.get("count_fp", "?")
    yes_p = f.get("yes_price_dollars", f.get("yes_price_fixed", "?"))
    ts = f.get("created_time", "")[:19]

    action_class = "green" if action == "buy" else "red"
    return f"""
    <tr>
        <td class="mono">{ts}</td>
        <td>{short}</td>
        <td class="{action_class}">{action.upper()} {side.upper()}</td>
        <td>{count}</td>
        <td>{yes_p}</td>
    </tr>"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    data = await _fetch_data()

    positions_html = "".join(_pos_row(p) for p in data["positions"])
    orders_html = "".join(_order_row(o) for o in data["resting_orders"])
    fills_html = "".join(_fill_row(f) for f in data["fills"][:10])

    # Polymarket section
    pm_rows = ""
    for name, info in sorted(data.get("polymarket", {}).items(), key=lambda x: -x[1]["price"]):
        pm_rows += f"""
        <tr>
            <td>{name}</td>
            <td><b>{info['price']*100:.1f}%</b></td>
            <td>${info['volume']:,.0f}</td>
        </tr>"""

    # Event-level summary
    event = data.get("event_positions", [{}])[0] if data.get("event_positions") else {}
    total_cost = float(event.get("total_cost_dollars", 0))
    total_shares = float(event.get("total_cost_shares_fp", 0))
    fees = float(event.get("fees_paid_dollars", 0))

    error_html = ""
    if data.get("error"):
        error_html = f'<div class="error">Broker error: {data["error"]}</div>'

    html = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>IL-9 Monitor</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; background: #0a0a0a; color: #e0e0e0; padding: 12px; }}
  h1 {{ font-size: 18px; margin-bottom: 4px; }}
  h2 {{ font-size: 14px; color: #888; margin: 12px 0 6px; text-transform: uppercase; letter-spacing: 1px; }}
  .header {{ display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid #333; margin-bottom: 8px; }}
  .countdown {{ font-size: 20px; font-weight: bold; color: #ff4444; }}
  .balance {{ font-size: 16px; color: #4CAF50; }}
  .summary {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; margin: 8px 0; }}
  .stat {{ background: #1a1a1a; padding: 8px; border-radius: 6px; text-align: center; }}
  .stat .label {{ font-size: 10px; color: #888; text-transform: uppercase; }}
  .stat .value {{ font-size: 18px; font-weight: bold; margin-top: 2px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; padding: 4px 6px; color: #888; border-bottom: 1px solid #333; font-size: 11px; text-transform: uppercase; }}
  td {{ padding: 4px 6px; border-bottom: 1px solid #1a1a1a; }}
  .green {{ color: #4CAF50; }}
  .red {{ color: #ff4444; }}
  .mono {{ font-family: monospace; font-size: 11px; }}
  .error {{ background: #441111; color: #ff6666; padding: 8px; border-radius: 4px; margin: 8px 0; }}
  .updated {{ font-size: 11px; color: #555; text-align: center; margin-top: 12px; }}
  .section {{ background: #111; border-radius: 8px; padding: 10px; margin-bottom: 10px; }}
</style>
</head><body>

<div class="header">
  <div>
    <h1>IL-9 Primary</h1>
    <span class="balance">${data['balance']:.2f} cash</span>
  </div>
  <div class="countdown">{_countdown()}</div>
</div>

{error_html}

<div class="summary">
  <div class="stat">
    <div class="label">Invested</div>
    <div class="value">${total_cost:.2f}</div>
  </div>
  <div class="stat">
    <div class="label">Contracts</div>
    <div class="value">{int(total_shares)}</div>
  </div>
  <div class="stat">
    <div class="label">Fees</div>
    <div class="value">${fees:.2f}</div>
  </div>
</div>

<div class="section">
<h2>Positions</h2>
<table>
  <tr><th>Tick</th><th>Candidate</th><th>Side</th><th>Qty</th><th>Exp</th><th>Rest</th></tr>
  {positions_html}
</table>
</div>

<div class="section">
<h2>Resting Orders ({len(data['resting_orders'])})</h2>
<table>
  <tr><th>Tick</th><th>Action</th><th>Side</th><th>Qty</th><th>Price</th></tr>
  {orders_html}
</table>
</div>

<div class="section">
<h2>Polymarket</h2>
<table>
  <tr><th>Candidate</th><th>Price</th><th>Volume</th></tr>
  {pm_rows}
</table>
</div>

<div class="section">
<h2>Recent Fills</h2>
<table>
  <tr><th>Time</th><th>Tick</th><th>Action</th><th>Qty</th><th>Price</th></tr>
  {fills_html}
</table>
</div>

<div class="updated">Updated: {data['updated']} · Auto-refresh 10s</div>

</body></html>"""

    return HTMLResponse(content=html)


@app.get("/api/data")
async def api_data():
    """JSON endpoint for programmatic access."""
    return await _fetch_data()


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
