"""
IL-9 Market Maker — Provide liquidity on KXIL9D-26-MSIM with a Simmons edge.

Strategy:
  - Post a two-sided book (YES bids + YES asks) around our fair value
  - Skew the book bullish: tighter asks (easy to buy YES), wider bids
  - Ladder orders across multiple price levels for depth
  - Keep net long exposure — we believe Simmons is underpriced

Mechanics:
  - SELL YES at various prices above our avg cost (profit-taking ladder)
  - BUY YES at low prices to catch panic sellers (accumulation)
  - With $3.88 cash: can post ~3-4 buy orders at 1c
  - With 1,176 contracts: can create substantial sell ladder

The goal: make MSIM look like a real market, attract volume, and
earn spread while maintaining our long thesis.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field

from config import CANDIDATE_NAMES, SHORT_NAMES, TICKERS

logger = logging.getLogger(__name__)


@dataclass
class LadderOrder:
    """A single order in the market-making ladder."""
    ticker: str
    action: str       # "buy" or "sell"
    side: str         # "yes" or "no"
    count: int        # contracts
    price: int        # cents (1-99)
    purpose: str      # human-readable description

    def __str__(self) -> str:
        name = SHORT_NAMES.get(self.ticker, self.ticker[-4:])
        return (
            f"{self.action.upper():4s} {self.count:>4d}x {self.side.upper():3s} "
            f"{name} @ {self.price:>2d}c  ({self.purpose})"
        )


@dataclass
class MarketMakerConfig:
    """Configuration for the Simmons market maker."""

    # Our fair value estimate for MSIM YES (10% from polls)
    fair_value: float = 0.10

    # Spread parameters
    # Bullish skew: asks are tighter to fair value than bids
    ask_spread: float = 0.01   # Ask at fair_value - ask_spread (tight, inviting buys)
    bid_spread: float = 0.04   # Bid at fair_value - bid_spread (wide, don't want to buy cheap)

    # Ladder parameters
    ask_levels: int = 5        # Number of ask price levels
    ask_step: int = 2          # Cents between ask levels
    bid_levels: int = 3        # Number of bid price levels
    bid_step: int = 1          # Cents between bid levels

    # Position limits
    max_sell_contracts: int = 400   # Max contracts to sell (keep 776+ long)
    min_long_position: int = 700    # Never sell below this many contracts
    max_buy_dollars: float = 3.50   # Max cash to deploy on bids

    # Contract sizes per level
    ask_size_per_level: int = 50    # Contracts per ask level
    bid_size_per_level: int = 50    # Contracts per bid level


class SimmonsMaker:
    """
    Market maker for KXIL9D-26-MSIM.

    Posts a skewed two-sided book to provide liquidity while
    maintaining a bullish Simmons position.
    """

    def __init__(
        self,
        config: MarketMakerConfig | None = None,
        current_position: int = 1176,
        available_cash: float = 3.88,
    ) -> None:
        self.config = config or MarketMakerConfig()
        self.position = current_position  # YES contracts held
        self.cash = available_cash
        self.active_orders: list[dict] = []

    def generate_ladder(self) -> list[LadderOrder]:
        """
        Generate the full order ladder.

        ASK SIDE (selling YES contracts we own):
          - Start near fair value, ladder up
          - Tighter spread = invites buyers (bullish signal)
          - Smaller sizes near fair value, larger further out

        BID SIDE (buying more YES contracts):
          - Start well below fair value
          - Wider spread = don't overpay, but catch sellers
          - Limited by available cash
        """
        orders: list[LadderOrder] = []
        cfg = self.config
        ticker = TICKERS["MSIM"]

        # ── ASK LADDER (sell YES) ──────────────────────────────
        # We want to sell some contracts at profit while keeping core long
        sellable = self.position - cfg.min_long_position
        if sellable <= 0:
            logger.info("No contracts available to sell (below min_long_position)")
        else:
            sellable = min(sellable, cfg.max_sell_contracts)
            sold_so_far = 0

            # Start asks near our fair value
            base_ask = max(1, round(cfg.fair_value * 100))  # 10c

            for i in range(cfg.ask_levels):
                price = base_ask + (i * cfg.ask_step)
                if price > 99:
                    break

                # Smaller sizes at lower prices, larger at higher (inverted pyramid)
                # We WANT people to buy at lower prices (bullish)
                if i == 0:
                    size = min(cfg.ask_size_per_level * 2, sellable - sold_so_far)
                else:
                    size = min(cfg.ask_size_per_level, sellable - sold_so_far)

                if size <= 0:
                    break

                purpose = "tight ask — invite buyers" if i < 2 else "profit target"
                orders.append(LadderOrder(
                    ticker=ticker,
                    action="sell",
                    side="yes",
                    count=size,
                    price=price,
                    purpose=purpose,
                ))
                sold_so_far += size

        # ── BID LADDER (buy YES) ──────────────────────────────
        # Deploy remaining cash at low prices to accumulate
        remaining_cash = cfg.max_buy_dollars
        base_bid = max(1, round((cfg.fair_value - cfg.bid_spread) * 100))  # 6c

        for i in range(cfg.bid_levels):
            price = max(1, base_bid - (i * cfg.bid_step))
            cost_per = price / 100.0
            max_contracts = math.floor(remaining_cash / cost_per) if cost_per > 0 else 0
            size = min(cfg.bid_size_per_level, max_contracts)

            if size <= 0:
                break

            cost = size * cost_per
            remaining_cash -= cost

            purpose = "accumulate on dips" if i == 0 else "deep bid — catch panic"
            orders.append(LadderOrder(
                ticker=ticker,
                action="buy",
                side="yes",
                count=size,
                price=price,
                purpose=purpose,
            ))

        return orders

    def generate_cross_market_shorts(self) -> list[LadderOrder]:
        """
        Generate short orders on overpriced candidates.

        Buy NO on Biss and Abughazaleh if we have remaining cash.
        These provide liquidity on the other side of the market
        and hedge our Simmons long.
        """
        orders: list[LadderOrder] = []

        # Only if we have meaningful cash left
        if self.cash < 1.0:
            return orders

        # Short Biss — buy NO at low prices
        # NO price should be ~76c if YES is 24c (our estimate)
        # Current YES is ~68c, so NO is ~32c — massively underpriced NO
        orders.append(LadderOrder(
            ticker=TICKERS["DBIS"],
            action="buy",
            side="no",
            count=2,
            price=30,
            purpose="short Biss — overpriced",
        ))

        # Short KA
        orders.append(LadderOrder(
            ticker=TICKERS["KA"],
            action="buy",
            side="no",
            count=1,
            price=70,
            purpose="short Abughazaleh — overpriced",
        ))

        return orders

    def summary(self, orders: list[LadderOrder]) -> str:
        """Pretty-print the order ladder with totals."""
        lines: list[str] = []
        lines.append("=" * 65)
        lines.append("  SIMMONS MARKET MAKER — ORDER LADDER")
        lines.append("=" * 65)
        lines.append(f"  Position: {self.position} YES contracts")
        lines.append(f"  Cash:     ${self.cash:.2f}")
        lines.append(f"  Fair val: {self.config.fair_value*100:.0f}c")
        lines.append("")

        ask_orders = [o for o in orders if o.action == "sell"]
        bid_orders = [o for o in orders if o.action == "buy" and o.ticker == TICKERS["MSIM"]]
        short_orders = [o for o in orders if o.action == "buy" and o.ticker != TICKERS["MSIM"]]

        if ask_orders:
            lines.append("  ── ASKS (sell YES — provide liquidity) ──")
            total_ask_contracts = 0
            for o in sorted(ask_orders, key=lambda x: x.price):
                lines.append(f"    {o}")
                total_ask_contracts += o.count
            lines.append(f"    Total: {total_ask_contracts} contracts offered")
            lines.append(f"    Remaining long: {self.position - total_ask_contracts}")
            lines.append("")

        if bid_orders:
            lines.append("  ── BIDS (buy YES — accumulate) ──")
            total_cost = 0.0
            for o in sorted(bid_orders, key=lambda x: -x.price):
                lines.append(f"    {o}")
                total_cost += o.count * o.price / 100.0
            lines.append(f"    Total cost if filled: ${total_cost:.2f}")
            lines.append("")

        if short_orders:
            lines.append("  ── CROSS-MARKET SHORTS ──")
            for o in short_orders:
                lines.append(f"    {o}")
            lines.append("")

        # Visualize the book
        lines.append("  ── BOOK VISUALIZATION ──")
        lines.append("")
        all_msim = [o for o in orders if o.ticker == TICKERS["MSIM"]]
        max_price = max((o.price for o in all_msim), default=20)
        min_price = min((o.price for o in all_msim), default=1)

        for price in range(max_price, min_price - 1, -1):
            asks_at = sum(o.count for o in ask_orders if o.price == price)
            bids_at = sum(o.count for o in bid_orders if o.price == price)

            bar = ""
            if asks_at:
                bar = f"  {'█' * min(asks_at // 5, 30)} ASK {asks_at}"
            elif bids_at:
                bar = f"  {'█' * min(bids_at // 5, 30)} BID {bids_at}"

            fair_marker = " ◄ FAIR" if price == round(self.config.fair_value * 100) else ""
            if bar or fair_marker:
                lines.append(f"    {price:>2d}c │{bar}{fair_marker}")

        lines.append("")
        lines.append("=" * 65)

        return "\n".join(lines)


async def place_ladder(orders: list[LadderOrder], dry_run: bool = True) -> list[dict]:
    """
    Place all orders in the ladder via the broker.

    Args:
        orders: list of LadderOrder to place
        dry_run: if True, just print orders without placing them

    Returns:
        List of order responses from the broker.
    """
    if dry_run:
        print("\n[DRY RUN] Would place these orders:\n")
        for o in orders:
            print(f"  {o}")
        return []

    from broker import KalshiBroker

    results: list[dict] = []
    async with KalshiBroker() as broker:
        for o in orders:
            try:
                if o.action == "buy" and o.side == "yes":
                    result = await broker.buy_yes(o.ticker, o.count, o.price)
                elif o.action == "sell" and o.side == "yes":
                    result = await broker.sell_yes(o.ticker, o.count, o.price)
                elif o.action == "buy" and o.side == "no":
                    result = await broker.buy_no(o.ticker, o.count, o.price)
                elif o.action == "sell" and o.side == "no":
                    result = await broker.sell_no(o.ticker, o.count, o.price)
                else:
                    logger.error(f"Unknown order type: {o}")
                    continue

                status = result.get("status", "?")
                oid = result.get("order_id", "?")
                print(f"  PLACED: {o} → {status} (id={oid})")
                results.append(result)

            except Exception as e:
                print(f"  FAILED: {o} → {e}")

            # Small delay between orders to avoid rate limiting
            await asyncio.sleep(0.2)

    return results


# ======================================================================
# CLI
# ======================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Simmons Market Maker")
    parser.add_argument("--live", action="store_true", help="Actually place orders (default: dry run)")
    parser.add_argument("--fair", type=int, default=10, help="Fair value in cents (default: 10)")
    parser.add_argument("--position", type=int, default=1176, help="Current YES position")
    parser.add_argument("--cash", type=float, default=3.88, help="Available cash")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    config = MarketMakerConfig(fair_value=args.fair / 100.0)
    maker = SimmonsMaker(
        config=config,
        current_position=args.position,
        available_cash=args.cash,
    )

    # Generate orders
    ladder = maker.generate_ladder()
    shorts = maker.generate_cross_market_shorts()
    all_orders = ladder + shorts

    # Print summary
    print(maker.summary(all_orders))

    if args.live:
        print("\n>>> LIVE MODE — placing orders <<<\n")
        asyncio.run(place_ladder(all_orders, dry_run=False))
    else:
        print("\n[DRY RUN] Add --live to actually place orders")
        asyncio.run(place_ladder(all_orders, dry_run=True))
