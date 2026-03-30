"""
IL-9 Adaptive Market-Making Agent

An autonomous agent that manages the MSIM orderbook to:
1. Walk the displayed price up toward fair value
2. Tighten spreads to attract real counterparties
3. Cancel stale orders and repost at better levels
4. React to fills by immediately reposting
5. Manage inventory risk across MSIM, DBIS, KA

Strategy: "Crawl-Up" — start tight, gradually widen the ask ladder
as the price approaches fair value. Each fill funds the next bid.

NOT reinforcement learning (no training data in a dead market).
This is rule-based adaptive market-making with a directional bias.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from broker import KalshiBroker
from config import CANDIDATE_NAMES, SHORT_NAMES, TICKERS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agent")


# ============================================================================
# Agent State
# ============================================================================

@dataclass
class AgentState:
    """Tracks the agent's view of the world."""

    # Portfolio
    balance_cents: int = 0
    msim_position: int = 0
    dbis_no_position: int = 0
    ka_no_position: int = 0

    # Market
    msim_bid: int = 0       # cents
    msim_ask: int = 0       # cents
    dbis_yes: int = 0       # cents
    ka_yes: int = 0         # cents

    # Agent's orders
    resting_sells: list[dict] = field(default_factory=list)
    resting_buys: list[dict] = field(default_factory=list)

    # Strategy state
    target_price: int = 10  # Where we want MSIM to trade (cents)
    current_displayed: int = 1  # What the market shows
    fills_since_last_adjust: int = 0
    last_adjust_time: float = 0
    cycle_count: int = 0


# ============================================================================
# Agent Configuration
# ============================================================================

@dataclass
class AgentConfig:
    """Tunable parameters for the market-making agent."""

    # Target fair value for MSIM (cents)
    fair_value: int = 10

    # How fast to walk up (cents per adjustment cycle)
    crawl_step: int = 1

    # Spread parameters
    min_spread: int = 2     # Minimum bid-ask spread in cents
    max_spread: int = 8     # Maximum spread

    # Order sizing
    ask_size: int = 50      # Contracts per ask level
    bid_size: int = 20      # Contracts per bid level (limited by cash)
    num_ask_levels: int = 3 # Ask ladder depth
    num_bid_levels: int = 2 # Bid ladder depth

    # Timing
    adjust_interval: int = 60   # Seconds between adjustments
    check_interval: int = 10    # Seconds between state checks

    # Inventory limits
    min_long_position: int = 800   # Never sell below this
    max_cash_deploy: float = 1.50  # Max $ to deploy on bids per cycle

    # Price walk-up rules
    walk_up_after_fills: int = 1  # Walk up after N fills
    max_price: int = 15           # Don't post asks above this


# ============================================================================
# Core Agent
# ============================================================================

class MarketMakingAgent:
    """
    Autonomous market-making agent for KXIL9D-26-MSIM.

    Loop:
      1. Refresh state (positions, orders, market prices)
      2. Evaluate: are orders stale? Did fills happen? Time to adjust?
      3. Act: cancel stale orders, post new orders, walk price up
      4. Log everything
      5. Sleep and repeat
    """

    def __init__(self, config: AgentConfig | None = None) -> None:
        self.config = config or AgentConfig()
        self.state = AgentState()
        self.broker: KalshiBroker | None = None
        self._running = False

    async def start(self) -> None:
        """Main agent loop."""
        self.broker = KalshiBroker()
        self._running = True

        logger.info("=" * 60)
        logger.info("  MSIM MARKET-MAKING AGENT STARTED")
        logger.info("  Fair value: %dc | Walk step: %dc", self.config.fair_value, self.config.crawl_step)
        logger.info("=" * 60)

        try:
            # Initial state refresh
            await self._refresh_state()
            self._log_state()

            # Initial order setup
            await self._setup_initial_orders()

            while self._running:
                await asyncio.sleep(self.config.check_interval)
                self.state.cycle_count += 1

                # Refresh
                await self._refresh_state()

                # Check for fills
                fills = await self._check_fills()
                if fills:
                    self.state.fills_since_last_adjust += fills
                    logger.info("Detected %d new fills! Total since last adjust: %d",
                                fills, self.state.fills_since_last_adjust)

                # Time to adjust?
                elapsed = time.time() - self.state.last_adjust_time
                should_adjust = (
                    elapsed >= self.config.adjust_interval
                    or self.state.fills_since_last_adjust >= self.config.walk_up_after_fills
                )

                if should_adjust:
                    await self._adjust_orders()
                    self.state.last_adjust_time = time.time()
                    self.state.fills_since_last_adjust = 0

                # Log every 6th cycle (~60s at 10s interval)
                if self.state.cycle_count % 6 == 0:
                    self._log_state()

        except KeyboardInterrupt:
            logger.info("Agent stopped by user")
        except Exception as e:
            logger.error("Agent error: %s", e, exc_info=True)
        finally:
            if self.broker:
                await self.broker.close()

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._running = False
        logger.info("Agent stopping...")

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    async def _refresh_state(self) -> None:
        """Pull latest data from broker."""
        assert self.broker is not None

        try:
            # Balance
            bal = await self.broker.get_balance()
            self.state.balance_cents = bal.get("balance", 0)

            # Positions
            resp = await self.broker._trading_get("/portfolio/positions")
            for p in resp.get("market_positions", []):
                ticker = p.get("ticker", "")
                pos = float(p.get("position_fp", 0))
                if ticker == TICKERS["MSIM"]:
                    self.state.msim_position = int(pos)
                elif ticker == TICKERS["DBIS"]:
                    self.state.dbis_no_position = abs(int(pos))
                elif ticker == TICKERS["KA"]:
                    self.state.ka_no_position = abs(int(pos))

            # Resting orders
            orders = await self.broker.get_orders(status="resting")
            self.state.resting_sells = [
                o for o in orders
                if o.get("ticker") == TICKERS["MSIM"] and o.get("action") == "sell"
            ]
            self.state.resting_buys = [
                o for o in orders
                if o.get("ticker") == TICKERS["MSIM"] and o.get("action") == "buy"
            ]

            # Market prices
            for ticker_key, attr_bid, attr_ask in [
                ("MSIM", "msim_bid", "msim_ask"),
            ]:
                try:
                    mkt = await self.broker.get_market(TICKERS[ticker_key])
                    bid_str = mkt.get("yes_bid", "0")
                    ask_str = mkt.get("yes_ask", "0")
                    # Handle both cents (int) and dollars (string like "0.01")
                    bid_val = float(bid_str) if bid_str else 0
                    ask_val = float(ask_str) if ask_str else 0
                    if bid_val < 1:  # dollars format
                        bid_val = int(bid_val * 100)
                        ask_val = int(ask_val * 100)
                    setattr(self.state, attr_bid, int(bid_val))
                    setattr(self.state, attr_ask, int(ask_val))
                except Exception:
                    pass

            # Track what the market "displays" as MSIM price
            if self.state.msim_ask > 0:
                self.state.current_displayed = self.state.msim_ask
            elif self.state.msim_bid > 0:
                self.state.current_displayed = self.state.msim_bid

        except Exception as e:
            logger.error("State refresh error: %s", e)

    async def _check_fills(self) -> int:
        """Check if any of our resting orders got filled since last check."""
        assert self.broker is not None

        # Compare current resting count to previous
        prev_sells = len(self.state.resting_sells)
        prev_buys = len(self.state.resting_buys)

        orders = await self.broker.get_orders(status="resting")
        curr_sells = len([
            o for o in orders
            if o.get("ticker") == TICKERS["MSIM"] and o.get("action") == "sell"
        ])
        curr_buys = len([
            o for o in orders
            if o.get("ticker") == TICKERS["MSIM"] and o.get("action") == "buy"
        ])

        filled = max(0, (prev_sells - curr_sells) + (prev_buys - curr_buys))
        return filled

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    async def _cancel_all_msim_orders(self) -> int:
        """Cancel all resting MSIM orders. Returns count cancelled."""
        assert self.broker is not None
        orders = await self.broker.get_orders(
            ticker=TICKERS["MSIM"], status="resting"
        )
        cancelled = 0
        for o in orders:
            oid = o.get("order_id", "")
            if oid:
                ok = await self.broker.cancel_order(oid)
                if ok:
                    cancelled += 1
                await asyncio.sleep(0.1)
        if cancelled:
            logger.info("Cancelled %d resting MSIM orders", cancelled)
        return cancelled

    async def _setup_initial_orders(self) -> None:
        """Set up the initial order ladder."""
        logger.info("Setting up initial order ladder...")
        await self._cancel_all_msim_orders()
        await asyncio.sleep(0.5)
        await self._post_orders(self.state.current_displayed)
        self.state.last_adjust_time = time.time()

    async def _adjust_orders(self) -> None:
        """
        Core adjustment logic — the "brain" of the agent.

        Rules:
        1. If fills happened → walk up: cancel and repost higher
        2. If no fills and timer expired → tighten spread to attract
        3. Never post above max_price
        4. Never sell below min_long_position
        """
        s = self.state
        c = self.config

        # Determine new price level
        new_price = s.current_displayed

        if s.fills_since_last_adjust >= c.walk_up_after_fills:
            # Fills happened — walk up
            new_price = min(s.current_displayed + c.crawl_step, c.max_price)
            logger.info("WALK UP: %dc → %dc (after %d fills)",
                        s.current_displayed, new_price, s.fills_since_last_adjust)
        else:
            # No fills — tighten spread by posting closer to current best
            # If our lowest ask is far from the bid, move it down
            if s.msim_ask > 0 and s.msim_bid > 0:
                spread = s.msim_ask - s.msim_bid
                if spread > c.min_spread:
                    new_price = max(s.msim_bid + c.min_spread, 2)
                    logger.info("TIGHTEN: spread was %dc, posting ask at %dc",
                                spread, new_price)

        # Cancel and repost
        await self._cancel_all_msim_orders()
        await asyncio.sleep(0.3)
        await self._post_orders(new_price)

    async def _post_orders(self, base_ask_price: int) -> None:
        """
        Post a full order ladder centered around base_ask_price.

        ASK side: sell YES from base_ask_price upward
        BID side: buy YES below base_ask_price
        """
        assert self.broker is not None
        s = self.state
        c = self.config

        base_ask_price = max(2, min(base_ask_price, c.max_price))

        # ── ASKS ──
        sellable = s.msim_position - c.min_long_position
        if sellable > 0:
            posted = 0
            for i in range(c.num_ask_levels):
                price = base_ask_price + (i * 2)  # 2c spacing
                if price > 99 or price > c.max_price:
                    break
                size = min(c.ask_size, sellable - posted)
                if size <= 0:
                    break

                try:
                    result = await self.broker.sell_yes(TICKERS["MSIM"], size, price)
                    status = result.get("status", "?")
                    logger.info("  ASK: SELL %dx YES MSIM @ %dc → %s", size, price, status)
                    posted += size
                except Exception as e:
                    logger.error("  ASK failed @ %dc: %s", price, e)
                await asyncio.sleep(0.15)

        # ── BIDS ──
        cash_available = min(s.balance_cents / 100, c.max_cash_deploy)
        if cash_available > 0.05:
            bid_price = max(1, base_ask_price - c.min_spread)
            for i in range(c.num_bid_levels):
                price = max(1, bid_price - i)
                cost_per = price / 100
                max_contracts = math.floor(cash_available / cost_per) if cost_per > 0 else 0
                size = min(c.bid_size, max_contracts)
                if size <= 0:
                    break

                try:
                    result = await self.broker.buy_yes(TICKERS["MSIM"], size, price)
                    status = result.get("status", "?")
                    logger.info("  BID: BUY %dx YES MSIM @ %dc → %s", size, price, status)
                    cash_available -= size * cost_per
                except Exception as e:
                    logger.error("  BID failed @ %dc: %s", price, e)
                await asyncio.sleep(0.15)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_state(self) -> None:
        s = self.state
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        logger.info("─" * 50)
        logger.info("State @ %s (cycle %d)", now, s.cycle_count)
        logger.info("  Balance: $%.2f | MSIM: %d YES | DBIS NO: %d | KA NO: %d",
                     s.balance_cents / 100, s.msim_position,
                     s.dbis_no_position, s.ka_no_position)
        logger.info("  MSIM market: bid=%dc ask=%dc displayed=%dc",
                     s.msim_bid, s.msim_ask, s.current_displayed)
        logger.info("  Resting: %d sells, %d buys",
                     len(s.resting_sells), len(s.resting_buys))
        logger.info("  Target: %dc | Fills pending: %d",
                     self.config.fair_value, s.fills_since_last_adjust)
        logger.info("─" * 50)


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="MSIM Market-Making Agent")
    parser.add_argument("--fair", type=int, default=10, help="Fair value cents (default: 10)")
    parser.add_argument("--crawl", type=int, default=1, help="Walk-up step cents (default: 1)")
    parser.add_argument("--interval", type=int, default=60, help="Adjust interval seconds (default: 60)")
    parser.add_argument("--check", type=int, default=10, help="Check interval seconds (default: 10)")
    parser.add_argument("--ask-size", type=int, default=50, help="Contracts per ask level (default: 50)")
    parser.add_argument("--min-long", type=int, default=800, help="Min position to keep (default: 800)")
    parser.add_argument("--max-price", type=int, default=15, help="Max ask price cents (default: 15)")
    args = parser.parse_args()

    config = AgentConfig(
        fair_value=args.fair,
        crawl_step=args.crawl,
        adjust_interval=args.interval,
        check_interval=args.check,
        ask_size=args.ask_size,
        min_long_position=args.min_long,
        max_price=args.max_price,
    )

    agent = MarketMakingAgent(config=config)
    asyncio.run(agent.start())
