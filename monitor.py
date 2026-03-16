"""
IL-9 Democratic Primary — Live Position Monitor

Terminal UI using Rich for real-time monitoring of prediction market positions
across Kalshi and Polymarket.
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config import (
    CANDIDATE_NAMES,
    MONITOR_REFRESH_INTERVAL,
    PRIMARY_TICKERS,
    SHORT_NAMES,
    TICKERS,
    POLYMARKET_TOKENS,
)


console = Console()

# Primary date
PRIMARY_DATE = datetime(2026, 3, 17, tzinfo=timezone.utc)


def time_to_primary() -> str:
    """Human-readable time until primary."""
    now = datetime.now(timezone.utc)
    delta = PRIMARY_DATE - now
    if delta.total_seconds() <= 0:
        return "PRIMARY DAY"
    hours = int(delta.total_seconds() // 3600)
    minutes = int((delta.total_seconds() % 3600) // 60)
    return f"T-{hours}h {minutes}m"


def build_header() -> Panel:
    """Build the header panel."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    countdown = time_to_primary()
    header_text = Text()
    header_text.append("IL-9 DEMOCRATIC PRIMARY MONITOR", style="bold white")
    header_text.append(f"  |  {now}  |  ", style="dim")
    header_text.append(countdown, style="bold red" if "T-" in countdown else "bold green")
    return Panel(header_text, style="blue")


def build_positions_table(positions: list[dict], balance: float) -> Panel:
    """Build the positions & P&L panel."""
    table = Table(title="Positions & P&L", expand=True, show_lines=True)
    table.add_column("Ticker", style="cyan", width=8)
    table.add_column("Candidate", width=18)
    table.add_column("Side", width=6)
    table.add_column("Qty", justify="right", width=5)
    table.add_column("Avg Price", justify="right", width=9)
    table.add_column("Mkt Price", justify="right", width=9)
    table.add_column("P&L", justify="right", width=10)
    table.add_column("P&L %", justify="right", width=8)

    total_pnl = 0.0
    total_cost = 0.0

    for pos in positions:
        ticker = pos.get("ticker", pos.get("symbol", ""))
        short = SHORT_NAMES.get(ticker, ticker[-4:])
        name = CANDIDATE_NAMES.get(ticker, "Unknown")
        side = pos.get("side", "?")
        qty = pos.get("count", pos.get("quantity", 0))
        avg = pos.get("avg_price", 0)
        mkt = pos.get("market_price", 0)

        # P&L calculation
        # For YES positions: profit when YES price goes up (mkt > avg)
        # For NO positions: market_price should be the NO price;
        #   profit when NO price goes up (mkt > avg)
        # Both sides: pnl = (current - entry) * qty
        pnl = (mkt - avg) * qty
        cost = avg * qty
        pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0
        total_pnl += pnl
        total_cost += cost

        pnl_style = "green" if pnl >= 0 else "red"
        side_style = "green" if side in ("yes", "long") else "red"

        table.add_row(
            short,
            name[:18],
            Text(side.upper(), style=side_style),
            str(qty),
            f"{avg*100:.1f}c",
            f"{mkt*100:.1f}c",
            Text(f"${pnl:.2f}", style=pnl_style),
            Text(f"{pnl_pct:+.1f}%", style=pnl_style),
        )

    # Summary row
    total_pnl_style = "green" if total_pnl >= 0 else "red"
    table.add_row(
        "", "", "", "", "", "TOTAL",
        Text(f"${total_pnl:.2f}", style=f"bold {total_pnl_style}"),
        "",
    )

    footer = f"Balance: ${balance:.2f} | Invested: ${total_cost:.2f} | Net P&L: ${total_pnl:.2f}"
    return Panel(table, subtitle=footer)


def build_market_table(markets: list[dict]) -> Panel:
    """Build the market prices panel for all candidates."""
    table = Table(title="Kalshi Markets — KXIL9D Series", expand=True, show_lines=True)
    table.add_column("Ticker", style="cyan", width=8)
    table.add_column("Candidate", width=18)
    table.add_column("YES", justify="right", width=7)
    table.add_column("NO", justify="right", width=7)
    table.add_column("Vol", justify="right", width=7)
    table.add_column("OI", justify="right", width=7)
    table.add_column("Poll", justify="right", width=6)
    table.add_column("Edge", justify="right", width=7)

    # Poll estimates for edge calculation
    from config import StrategyConfig
    cfg = StrategyConfig()

    for m in markets:
        ticker = m.get("ticker", "")
        short = SHORT_NAMES.get(ticker, ticker[-4:])
        name = CANDIDATE_NAMES.get(ticker, "Unknown")
        yes_price = m.get("yes_price") or 0
        no_price = m.get("no_price") or 0
        vol = m.get("volume") or 0
        oi = m.get("open_interest") or 0

        # Convert from cents if needed
        if isinstance(yes_price, (int, float)) and yes_price > 1:
            yes_price = yes_price / 100
            no_price = no_price / 100 if no_price else 1 - yes_price

        poll = cfg.prob_estimates.get(ticker, 0)
        edge = poll - yes_price if yes_price > 0 else 0

        edge_style = "green" if edge > 0 else "red" if edge < 0 else "dim"
        vol_str = f"{vol:,}" if vol else "-"
        oi_str = f"{oi:,}" if oi else "-"

        # Highlight primary tickers
        row_style = "bold" if ticker in PRIMARY_TICKERS else ""

        table.add_row(
            Text(short, style=row_style),
            name[:18],
            f"{yes_price*100:.1f}c" if yes_price else "-",
            f"{no_price*100:.1f}c" if no_price else "-",
            vol_str,
            oi_str,
            f"{poll*100:.0f}%" if poll else "-",
            Text(f"{edge*100:+.1f}c", style=edge_style) if yes_price else Text("-"),
        )

    return Panel(table)


def build_orderbook_panel(orderbooks: dict[str, dict]) -> Panel:
    """Build orderbook depth display for primary tickers."""
    parts = []
    for ticker, ob in orderbooks.items():
        short = SHORT_NAMES.get(ticker, ticker[-4:])
        yes_bids = ob.get("yes", ob.get("yes_bids", []))
        no_bids = ob.get("no", ob.get("no_bids", []))

        text = Text()
        text.append(f"  {short}\n", style="bold cyan")

        # YES side
        text.append("    YES: ", style="green")
        if yes_bids:
            for level in yes_bids[:3]:
                if isinstance(level, dict):
                    p, q = level.get("price", 0), level.get("quantity", 0)
                elif isinstance(level, (list, tuple)):
                    p, q = level[0], level[1]
                else:
                    continue
                text.append(f"{p}c x {q}  ")
        else:
            text.append("EMPTY", style="dim red")
        text.append("\n")

        # NO side
        text.append("    NO:  ", style="red")
        if no_bids:
            for level in no_bids[:3]:
                if isinstance(level, dict):
                    p, q = level.get("price", 0), level.get("quantity", 0)
                elif isinstance(level, (list, tuple)):
                    p, q = level[0], level[1]
                else:
                    continue
                text.append(f"{p}c x {q}  ")
        else:
            text.append("EMPTY", style="dim red")
        text.append("\n")

        parts.append(text)

    combined = Text()
    for p in parts:
        combined.append_text(p)

    return Panel(combined, title="Orderbook Depth (Primary Tickers)")


def build_polymarket_panel(pm_data: dict[str, dict]) -> Panel:
    """Build Polymarket cross-reference panel."""
    table = Table(title="Polymarket — IL-09", expand=True)
    table.add_column("Candidate", width=15)
    table.add_column("Price", justify="right", width=8)
    table.add_column("Volume", justify="right", width=10)
    table.add_column("Liquidity", justify="right", width=10)
    table.add_column("Bid", justify="right", width=8)
    table.add_column("Ask", justify="right", width=8)

    for name, data in pm_data.items():
        price = data.get("price", 0)
        vol = data.get("volume", 0)
        liq = data.get("liquidity", 0)
        bid = data.get("best_bid", 0)
        ask = data.get("best_ask", 0)

        table.add_row(
            name,
            f"{price*100:.1f}%" if price else "-",
            f"${vol:,.0f}" if vol else "-",
            f"${liq:,.0f}" if liq else "-",
            f"{bid*100:.1f}c" if bid else "-",
            f"{ask*100:.1f}c" if ask else "-",
        )

    return Panel(table)


def build_signals_panel(signals: list, impact_estimates: list | None = None) -> Panel:
    """Build strategy signals and impact panel."""
    text = Text()

    if not signals:
        text.append("  No signals — positions at target\n", style="dim")
    else:
        for sig in signals:
            short = SHORT_NAMES.get(sig.ticker, sig.ticker[-4:])
            action_style = "green" if sig.action == "buy" else "red"
            text.append(f"  {sig.action.upper()} ", style=action_style)
            text.append(f"{sig.side.upper()} ", style="bold")
            text.append(f"{short} x{sig.count} @ {sig.limit_price}c  ")
            text.append(f"edge={sig.edge*100:.1f}c  kelly={sig.kelly_fraction:.1%}\n")
            text.append(f"    {sig.reason}\n", style="dim")

    if impact_estimates:
        text.append("\n  IMPACT ANALYSIS\n", style="bold yellow")
        for est in impact_estimates:
            short = SHORT_NAMES.get(est.ticker, est.ticker[-4:])
            warn_style = "red" if est.is_price_setter else "yellow"
            text.append(f"  {short}: ", style="cyan")
            text.append(f"fill={est.expected_fill*100:.1f}c  slip={est.slippage_cents:.1f}c  ")
            if est.is_price_setter:
                text.append("PRICE-SETTER", style="bold red")
            if est.warning:
                text.append(f"\n    {est.warning}", style=warn_style)
            text.append("\n")

    return Panel(text, title="Strategy Signals & Impact")


def build_layout(
    positions: list[dict],
    balance: float,
    markets: list[dict],
    orderbooks: dict[str, dict],
    pm_data: dict[str, dict],
    signals: list,
    impact_estimates: list | None = None,
) -> Layout:
    """Compose the full terminal layout."""
    layout = Layout()

    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=12),
    )

    layout["body"].split_row(
        Layout(name="left", ratio=1),
        Layout(name="right", ratio=1),
    )

    layout["left"].split_column(
        Layout(name="positions", ratio=1),
        Layout(name="polymarket", ratio=1),
    )

    layout["right"].split_column(
        Layout(name="markets", ratio=2),
        Layout(name="orderbook", ratio=1),
    )

    layout["header"].update(build_header())
    layout["positions"].update(build_positions_table(positions, balance))
    layout["markets"].update(build_market_table(markets))
    layout["orderbook"].update(build_orderbook_panel(orderbooks))
    layout["polymarket"].update(build_polymarket_panel(pm_data))
    layout["footer"].update(build_signals_panel(signals, impact_estimates))

    return layout


class Monitor:
    """
    Live terminal monitor that aggregates data from multiple sources.

    Pulls from:
    - Kalshi broker (positions, balance)
    - Kalshi elections API (market prices, orderbooks)
    - Polymarket (cross-reference prices)
    - Strategy engine (signals)
    - Impact analyzer (slippage estimates)
    """

    def __init__(self) -> None:
        self.positions: list[dict] = []
        self.balance: float = 0.0
        self.markets: list[dict] = []
        self.orderbooks: dict[str, dict] = {}
        self.pm_data: dict[str, dict] = {}
        self.signals: list = []
        self.impact_estimates: list = []
        self._running = False

    def update(
        self,
        positions: list[dict] | None = None,
        balance: float | None = None,
        markets: list[dict] | None = None,
        orderbooks: dict[str, dict] | None = None,
        pm_data: dict[str, dict] | None = None,
        signals: list | None = None,
        impact_estimates: list | None = None,
    ) -> None:
        """Update monitor state."""
        if positions is not None:
            self.positions = positions
        if balance is not None:
            self.balance = balance
        if markets is not None:
            self.markets = markets
        if orderbooks is not None:
            self.orderbooks = orderbooks
        if pm_data is not None:
            self.pm_data = pm_data
        if signals is not None:
            self.signals = signals
        if impact_estimates is not None:
            self.impact_estimates = impact_estimates

    def render(self) -> Layout:
        """Render the current state."""
        return build_layout(
            self.positions,
            self.balance,
            self.markets,
            self.orderbooks,
            self.pm_data,
            self.signals,
            self.impact_estimates,
        )


if __name__ == "__main__":
    # Demo with sample data
    monitor = Monitor()
    monitor.update(
        balance=97.50,
        positions=[
            {"ticker": "KXIL9D-26-MSIM", "side": "yes", "count": 50,
             "avg_price": 0.01, "market_price": 0.01},
            {"ticker": "KXIL9D-26-DBIS", "side": "no", "count": 10,
             "avg_price": 0.32, "market_price": 0.31},
        ],
        markets=[
            {"ticker": "KXIL9D-26-MSIM", "yes_price": 0.01, "no_price": 0.99, "volume": 0, "open_interest": 0},
            {"ticker": "KXIL9D-26-DBIS", "yes_price": 0.685, "no_price": 0.315, "volume": 0, "open_interest": 0},
            {"ticker": "KXIL9D-26-KA", "yes_price": 0.305, "no_price": 0.695, "volume": 0, "open_interest": 0},
            {"ticker": "KXIL9D-26-LFIN", "yes_price": 0.004, "no_price": 0.996, "volume": 0, "open_interest": 0},
        ],
        orderbooks={
            "KXIL9D-26-MSIM": {"yes": [], "no": []},
            "KXIL9D-26-DBIS": {"yes": [], "no": []},
            "KXIL9D-26-KA": {"yes": [], "no": []},
        },
        pm_data={
            "Biss": {"price": 0.685, "volume": 23004, "liquidity": 11565, "best_bid": 0.68, "best_ask": 0.70},
            "Abughazaleh": {"price": 0.305, "volume": 31619, "liquidity": 12447, "best_bid": 0.30, "best_ask": 0.31},
            "Fine": {"price": 0.004, "volume": 16377, "liquidity": 7704, "best_bid": 0.001, "best_ask": 0.999},
        },
        signals=[],
    )

    console.print(monitor.render())
