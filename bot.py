"""
IL-9 Democratic Primary — Trading Bot

Main entry point. Ties together:
- Kalshi broker (positions, orders)
- Market data (wrdata Kalshi + Polymarket providers)
- Strategy engine (Kelly-based signals)
- Impact analysis (slippage estimation)
- Terminal monitor (Rich live display)

Usage:
    python bot.py              # Full monitor + strategy mode
    python bot.py --monitor    # Monitor only (no trading)
    python bot.py --balance    # Just check balance and exit
    python bot.py --markets    # Just show markets and exit
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

from rich.console import Console
from rich.live import Live
from rich.prompt import Confirm, IntPrompt

from config import (
    CANDIDATE_NAMES,
    MONITOR_REFRESH_INTERVAL,
    POLYMARKET_EVENT_SLUG,
    PRIMARY_TICKERS,
    SHORT_NAMES,
    STRATEGY_EVAL_INTERVAL,
    TICKERS,
    POLYMARKET_TOKENS,
    StrategyConfig,
)
from monitor import Monitor

logger = logging.getLogger(__name__)
console = Console()


# ============================================================================
# Data fetching helpers
# ============================================================================

async def fetch_kalshi_markets() -> list[dict]:
    """Fetch all IL-9 markets from Kalshi elections API (no auth)."""
    from wrdata.providers.kalshi_provider import KalshiProvider
    provider = KalshiProvider()
    loop = asyncio.get_event_loop()
    try:
        markets = await loop.run_in_executor(
            None, lambda: provider.fetch_markets(series_ticker="KXIL9D", limit=50)
        )
        return markets
    except Exception as e:
        logger.error(f"Failed to fetch Kalshi markets: {e}")
        return []


async def fetch_kalshi_orderbooks(tickers: list[str]) -> dict[str, dict]:
    """Fetch orderbooks for specified tickers from Kalshi elections API."""
    from wrdata.providers.kalshi_provider import KalshiProvider
    provider = KalshiProvider()
    loop = asyncio.get_event_loop()
    orderbooks = {}
    for ticker in tickers:
        try:
            ob = await loop.run_in_executor(
                None, lambda t=ticker: provider.fetch_orderbook(t)
            )
            orderbooks[ticker] = ob
        except Exception as e:
            logger.warning(f"Failed to fetch orderbook for {ticker}: {e}")
            orderbooks[ticker] = {"yes": [], "no": []}
    return orderbooks


async def fetch_polymarket_data() -> dict[str, dict]:
    """Fetch Polymarket cross-reference data via event slug."""
    import requests
    import json as _json

    result: dict[str, dict] = {}
    session = requests.Session()
    session.headers.update({"User-Agent": "wrdata/1.0", "Accept": "application/json"})

    try:
        resp = session.get(
            "https://gamma-api.polymarket.com/events",
            params={"slug": POLYMARKET_EVENT_SLUG, "limit": 1},
            timeout=15,
        )
        resp.raise_for_status()
        events = resp.json()
        if not events:
            return result

        event = events[0]
        # Map condition_id -> candidate name
        cid_to_name = {}
        for name, tokens in POLYMARKET_TOKENS.items():
            cid_to_name[tokens["condition_id"]] = name

        for m in event.get("markets", []):
            cid = m.get("conditionId", "")
            name = cid_to_name.get(cid)
            if not name:
                continue

            outcomes = m.get("outcomePrices", "[]")
            if isinstance(outcomes, str):
                outcomes = _json.loads(outcomes)
            yes_price = float(outcomes[0]) if outcomes else 0

            vol = float(m.get("volume", m.get("volumeNum", 0)) or 0)
            liq = float(m.get("liquidity", 0) or 0)

            # Fetch orderbook for bid/ask
            best_bid = 0.0
            best_ask = 0.0
            try:
                from wrdata.providers.polymarket_provider import PolymarketProvider
                pm = PolymarketProvider()
                yes_token = POLYMARKET_TOKENS[name]["yes"]
                ob = pm.fetch_orderbook(yes_token)
                bids = ob.get("bids", [])
                asks = ob.get("asks", [])
                best_bid = max((float(b["price"]) for b in bids), default=0)
                best_ask = min((float(a["price"]) for a in asks), default=0)
            except Exception:
                pass

            result[name] = {
                "price": yes_price,
                "volume": vol,
                "liquidity": liq,
                "best_bid": best_bid,
                "best_ask": best_ask,
            }
    except Exception as e:
        logger.warning(f"Failed to fetch Polymarket event data: {e}")

    return result


def _translate_position(raw: dict) -> dict:
    """Translate Kalshi API position dict to strategy/monitor format."""
    qty = raw.get("position", 0)
    side = "yes" if qty >= 0 else "no"
    total_cost = raw.get("total_cost", 0)
    abs_qty = abs(qty)
    avg_price = (total_cost / abs_qty / 100.0) if abs_qty > 0 else 0  # cents -> dollars
    return {
        "ticker": raw.get("ticker", ""),
        "side": side,
        "count": abs_qty,
        "avg_price": avg_price,
        "market_price": 0.0,  # filled in by monitor
        "raw": raw,
    }


async def fetch_broker_data(broker) -> tuple[float, list[dict]]:
    """Fetch balance and positions from Kalshi broker."""
    try:
        bal = await broker.get_balance()
        balance = bal.get("balance", bal.get("cash", 0)) / 100.0  # cents -> dollars
    except Exception as e:
        logger.error(f"Failed to fetch balance: {e}")
        balance = 0.0

    try:
        raw_positions = await broker.get_positions()
        positions = [_translate_position(p) for p in raw_positions]
    except Exception as e:
        logger.error(f"Failed to fetch positions: {e}")
        positions = []

    return balance, positions


# ============================================================================
# Main loops
# ============================================================================

async def monitor_loop(monitor: Monitor, broker=None) -> None:
    """Continuously refresh market data and update monitor."""
    while True:
        try:
            # Fetch market data (elections API, no auth)
            markets = await fetch_kalshi_markets()
            orderbooks = await fetch_kalshi_orderbooks(PRIMARY_TICKERS)

            # Fetch Polymarket data
            pm_data = await fetch_polymarket_data()

            # Fetch broker data if available
            balance = 0.0
            positions = []
            if broker:
                balance, positions = await fetch_broker_data(broker)

            monitor.update(
                markets=markets,
                orderbooks=orderbooks,
                pm_data=pm_data,
                balance=balance,
                positions=positions,
            )

        except Exception as e:
            logger.error(f"Monitor loop error: {e}")

        await asyncio.sleep(MONITOR_REFRESH_INTERVAL)


async def strategy_loop(monitor: Monitor, broker=None) -> None:
    """Evaluate strategy and generate signals periodically."""
    from strategy import IL9Strategy
    from impact import estimate_impact

    strategy = IL9Strategy()

    while True:
        await asyncio.sleep(STRATEGY_EVAL_INTERVAL)

        try:
            # Build market_prices dict from monitor state
            market_prices = {}
            for m in monitor.markets:
                ticker = m.get("ticker", "")
                yes_p = m.get("yes_price", 0)
                no_p = m.get("no_price", 0)
                if isinstance(yes_p, (int, float)) and yes_p > 1:
                    yes_p = yes_p / 100
                    no_p = no_p / 100 if no_p else 1 - yes_p
                market_prices[ticker] = {
                    "yes_price": yes_p or 0,
                    "no_price": no_p or (1 - yes_p if yes_p else 0),
                    "volume": m.get("volume", 0),
                    "open_interest": m.get("open_interest", 0),
                }

            # Update strategy with current positions
            strategy.update_positions(monitor.positions)

            # Generate signals
            signals = strategy.evaluate(market_prices)

            # Estimate impact for each signal
            impact_estimates = []
            for sig in signals:
                ob = monitor.orderbooks.get(sig.ticker, {"yes": [], "no": []})
                mp = market_prices.get(sig.ticker, {})
                est = estimate_impact(
                    ticker=sig.ticker,
                    side=f"{sig.action}_{sig.side}",
                    count=sig.count,
                    current_yes_price=mp.get("yes_price", 0),
                    ob_yes=ob.get("yes", ob.get("yes_bids", [])),
                    ob_no=ob.get("no", ob.get("no_bids", [])),
                    volume=mp.get("volume", 0),
                    open_interest=mp.get("open_interest", 0),
                )
                impact_estimates.append(est)

            monitor.update(signals=signals, impact_estimates=impact_estimates)

        except Exception as e:
            logger.error(f"Strategy loop error: {e}")


async def order_executor(monitor: Monitor, broker) -> None:
    """
    Interactive order execution. When signals appear, prompt user for confirmation.

    Runs in a separate thread to avoid blocking the Rich Live display.
    """
    last_signal_count = 0

    while True:
        await asyncio.sleep(5)

        if not monitor.signals or len(monitor.signals) == last_signal_count:
            continue

        last_signal_count = len(monitor.signals)

        # Log signals to console (will appear below the live display)
        for sig in monitor.signals:
            short = SHORT_NAMES.get(sig.ticker, sig.ticker[-4:])
            logger.info(
                f"SIGNAL: {sig.action.upper()} {sig.side.upper()} "
                f"{short} x{sig.count} @ {sig.limit_price}c "
                f"(edge={sig.edge*100:.1f}c, kelly={sig.kelly_fraction:.1%})"
            )


# ============================================================================
# CLI Commands
# ============================================================================

async def cmd_balance() -> None:
    """Check balance and print."""
    from broker import KalshiBroker
    async with KalshiBroker() as broker:
        try:
            bal = await broker.get_balance()
            cents = bal.get("balance", bal.get("cash", 0))
            console.print(f"[bold green]Balance:[/] ${cents / 100.0:.2f} ({cents}c)")
        except Exception as e:
            console.print(f"[bold red]Error:[/] {e}")


async def cmd_markets() -> None:
    """Show all IL-9 markets."""
    markets = await fetch_kalshi_markets()
    if not markets:
        console.print("[red]No markets found or API error[/]")
        return

    from rich.table import Table
    table = Table(title="Kalshi IL-9 Markets")
    table.add_column("Ticker", style="cyan")
    table.add_column("Candidate")
    table.add_column("YES", justify="right")
    table.add_column("NO", justify="right")
    table.add_column("Volume", justify="right")
    table.add_column("OI", justify="right")

    for m in markets:
        ticker = m.get("ticker", "")
        name = CANDIDATE_NAMES.get(ticker, m.get("title", "?"))
        yes_p = m.get("yes_price")
        no_p = m.get("no_price")
        vol = m.get("volume", 0)
        oi = m.get("open_interest", 0)

        table.add_row(
            ticker,
            name[:25],
            f"{yes_p}c" if yes_p else "-",
            f"{no_p}c" if no_p else "-",
            str(vol) if vol else "-",
            str(oi) if oi else "-",
        )

    console.print(table)


async def cmd_positions() -> None:
    """Show current positions."""
    from broker import KalshiBroker
    async with KalshiBroker() as broker:
        try:
            raw = await broker.get_positions()
            positions = [_translate_position(p) for p in raw]
            if not positions:
                console.print("[dim]No open positions[/]")
                return
            for pos in positions:
                console.print(pos)
        except Exception as e:
            console.print(f"[bold red]Error:[/] {e}")


async def cmd_monitor(trading: bool = False) -> None:
    """Run the live monitor."""
    broker = None
    if trading:
        try:
            from broker import KalshiBroker
            broker = KalshiBroker()
            bal = await broker.get_balance()
            cents = bal.get("balance", bal.get("cash", 0))
            console.print(f"[green]Broker connected. Balance: ${cents / 100.0:.2f}[/]")
        except Exception as e:
            console.print(f"[yellow]Broker auth failed ({e}), running in monitor-only mode[/]")
            broker = None

    monitor = Monitor()

    # Initial data fetch
    console.print("[dim]Fetching initial data...[/]")
    markets = await fetch_kalshi_markets()
    orderbooks = await fetch_kalshi_orderbooks(PRIMARY_TICKERS)

    console.print("[dim]Fetching Polymarket data...[/]")
    pm_data = await fetch_polymarket_data()

    balance = 0.0
    positions = []
    if broker:
        balance, positions = await fetch_broker_data(broker)

    monitor.update(
        markets=markets,
        orderbooks=orderbooks,
        pm_data=pm_data,
        balance=balance,
        positions=positions,
    )

    console.print("[green]Starting live monitor... Press Ctrl+C to exit[/]")

    def _task_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"Background task {task.get_name()} died: {exc}")

    # Start background tasks
    tasks = [
        asyncio.create_task(monitor_loop(monitor, broker), name="monitor"),
    ]
    if trading:
        tasks.append(asyncio.create_task(strategy_loop(monitor, broker), name="strategy"))
        if broker:
            tasks.append(asyncio.create_task(order_executor(monitor, broker), name="executor"))

    for t in tasks:
        t.add_done_callback(_task_done)

    try:
        with Live(monitor.render(), console=console, refresh_per_second=1) as live:
            while True:
                await asyncio.sleep(1)
                live.update(monitor.render())
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down...[/]")
        for task in tasks:
            task.cancel()


async def cmd_buy(ticker: str, side: str, count: int, price: int) -> None:
    """Place a buy order."""
    if side not in ("yes", "no"):
        console.print(f"[bold red]Invalid side: {side!r}. Must be 'yes' or 'no'[/]")
        return

    from broker import KalshiBroker
    name = CANDIDATE_NAMES.get(ticker, ticker)
    console.print(f"[bold]Order: BUY {side.upper()} {name} x{count} @ {price}c[/]")

    if not Confirm.ask("Confirm?"):
        console.print("[dim]Cancelled[/]")
        return

    async with KalshiBroker() as broker:
        try:
            if side == "yes":
                result = await broker.buy_yes(ticker, count, price)
            else:
                result = await broker.buy_no(ticker, count, price)
            console.print(f"[green]Order placed:[/] {result}")
        except Exception as e:
            console.print(f"[bold red]Order failed:[/] {e}")


# ============================================================================
# Entry point
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="IL-9 Primary Monitor & Bot")
    parser.add_argument("--monitor", action="store_true", help="Monitor only (no trading)")
    parser.add_argument("--trade", action="store_true", help="Monitor + strategy + trading")
    parser.add_argument("--balance", action="store_true", help="Check balance")
    parser.add_argument("--markets", action="store_true", help="Show markets")
    parser.add_argument("--positions", action="store_true", help="Show positions")
    parser.add_argument("--buy", nargs=4, metavar=("TICKER", "SIDE", "COUNT", "PRICE"),
                        help="Buy: TICKER yes|no COUNT PRICE_CENTS")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.balance:
        asyncio.run(cmd_balance())
    elif args.markets:
        asyncio.run(cmd_markets())
    elif args.positions:
        asyncio.run(cmd_positions())
    elif args.buy:
        ticker, side, count, price = args.buy
        asyncio.run(cmd_buy(ticker, side, int(count), int(price)))
    elif args.trade:
        asyncio.run(cmd_monitor(trading=True))
    else:
        # Default: monitor mode
        asyncio.run(cmd_monitor(trading=False))


if __name__ == "__main__":
    main()
