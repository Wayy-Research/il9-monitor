"""
IL-9 Democratic Primary -- Market Impact & Slippage Estimation

These markets are EXTREMELY thin. Most contracts have zero volume, zero OI,
and empty orderbooks. This module estimates the true cost of trading in
these conditions so we don't fool ourselves about our edge.

The core insight: in a market with no liquidity, YOU are the liquidity.
Your order doesn't just move the price -- it IS the price. That changes
everything about how you think about position sizing.

Three regimes:
  1. Empty book, zero OI  -> You're the price-setter. No reference price.
  2. Thin book (< 50 lots) -> Walk-the-book model, expect full slippage.
  3. Moderate book (50+)   -> Square-root impact model applies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ImpactEstimate:
    """Estimated market impact for a trade."""

    ticker: str
    side: str  # "buy_yes", "buy_no", "sell_yes", "sell_no"
    count: int  # contracts
    current_price: float  # current mid or last price (0-1 scale)
    expected_fill: float  # expected average fill price (0-1 scale)
    slippage_cents: float  # slippage in cents
    slippage_pct: float  # slippage as % of price
    total_cost: float  # total cost in USD (count * fill_price)
    is_price_setter: bool  # True if OI=0 and book empty
    book_depth_yes: float  # total $ available on YES side
    book_depth_no: float  # total $ available on NO side
    warning: str  # human-readable warning if any


# ---------------------------------------------------------------------------
# Orderbook walking
# ---------------------------------------------------------------------------


def walk_book(
    orders: list[dict[str, int]],
    count: int,
) -> tuple[float, float, int]:
    """
    Walk an orderbook to fill `count` contracts.

    Orders should be the ask side sorted ascending by price (cheapest first).
    Each order is {"price": int_cents, "quantity": int}.

    Returns:
        (avg_fill_price_cents, total_cost_cents, unfilled_count)

    If the book is empty or too thin, unfilled_count > 0.
    """
    if not orders or count <= 0:
        return 0.0, 0.0, count

    filled: int = 0
    cost_cents: float = 0.0

    for level in orders:
        price: int = level["price"]
        qty: int = level["quantity"]

        take: int = min(qty, count - filled)
        cost_cents += price * take
        filled += take

        if filled >= count:
            break

    unfilled: int = count - filled
    avg_fill: float = cost_cents / filled if filled > 0 else 0.0

    return avg_fill, cost_cents, unfilled


# ---------------------------------------------------------------------------
# Square-root impact model
# ---------------------------------------------------------------------------


def estimate_sqrt_impact(
    count: int,
    current_price: float,
    daily_volume: int,
    volatility: float = 0.10,
    direction: str = "buy",
) -> float:
    """
    Square-root market impact model for when orderbook is empty or thin.

    Classic Almgren-Chriss style:
        dP = volatility * sqrt(count / max(daily_volume, 1))

    For prediction markets the "volatility" is the implied vol of the
    binary outcome. In thin IL-9 markets with prices near the edges
    (5-30c), realized vol is high relative to price.

    Args:
        count: number of contracts to trade
        current_price: current price on 0-1 scale
        daily_volume: average daily volume in contracts
        volatility: annualized vol of the contract (0-1 scale)
        direction: "buy" (pushes price up) or "sell" (pushes price down)

    Returns:
        Expected price move on 0-1 scale (always positive).
    """
    adv: int = max(daily_volume, 1)
    impact: float = volatility * math.sqrt(count / adv)

    # Clamp: impact can't push price beyond 0-1 boundaries
    if direction == "buy":
        impact = min(impact, 1.0 - current_price)
    else:
        impact = min(impact, current_price)

    return max(0.0, impact)


# ---------------------------------------------------------------------------
# Book depth helpers
# ---------------------------------------------------------------------------


def _book_depth_usd(orders: list[dict[str, int]]) -> float:
    """Total dollar value resting on one side of the book."""
    return sum(o["price"] * o["quantity"] for o in orders) / 100.0


# ---------------------------------------------------------------------------
# Main estimation
# ---------------------------------------------------------------------------

# Sides that consume the YES ask
_YES_SIDES: set[str] = {"buy_yes", "sell_no"}
# Sides that consume the NO ask
_NO_SIDES: set[str] = {"buy_no", "sell_yes"}


def estimate_impact(
    ticker: str,
    side: str,
    count: int,
    current_yes_price: float,
    ob_yes: list[dict[str, int]],
    ob_no: list[dict[str, int]],
    volume: int = 0,
    open_interest: int = 0,
) -> ImpactEstimate:
    """
    Estimate market impact for a proposed trade on a Kalshi binary contract.

    For buy_yes: we walk up the YES ask book.
    For buy_no:  we walk up the NO ask book.
    For sell_yes / sell_no: we hit the respective bid
      (modeled as the opposite ask in Kalshi's YES/NO duality).

    When the book is empty (the common case in IL-9):
    - Use the square-root model with elevated volatility.
    - When volume AND OI are both zero, flag as a price-setter trade.

    Args:
        ticker: contract ticker (e.g. "KXIL9D-26-MSIM")
        side: one of "buy_yes", "buy_no", "sell_yes", "sell_no"
        count: number of contracts
        current_yes_price: current YES price on 0-1 scale
        ob_yes: YES ask side [{"price": cents, "quantity": qty}, ...]
        ob_no: NO ask side
        volume: recent daily volume
        open_interest: current open interest

    Returns:
        ImpactEstimate with all the gory details.
    """
    if side not in ("buy_yes", "buy_no", "sell_yes", "sell_no"):
        raise ValueError(f"Invalid side: {side!r}")

    current_no_price: float = 1.0 - current_yes_price
    book_depth_yes: float = _book_depth_usd(ob_yes)
    book_depth_no: float = _book_depth_usd(ob_no)

    # Determine which book we consume and the reference price
    if side in _YES_SIDES:
        book: list[dict[str, int]] = ob_yes
        ref_price: float = current_yes_price
    else:
        book = ob_no
        ref_price = current_no_price

    ref_cents: float = ref_price * 100.0
    warning: str = ""
    is_price_setter: bool = False

    # --- Case 1: Book has orders, walk it ---
    if book:
        avg_fill_cents, total_cost_cents, unfilled = walk_book(book, count)

        if unfilled > 0:
            # Book ran out. Estimate the rest with sqrt impact.
            filled_count: int = count - unfilled
            sqrt_dp: float = estimate_sqrt_impact(
                unfilled,
                ref_price,
                volume,
                volatility=_implied_vol(ref_price),
            )
            # Unfilled portion fills at ref_price + impact
            extra_price_cents: float = (ref_price + sqrt_dp) * 100.0
            total_cost_cents += extra_price_cents * unfilled
            avg_fill_cents = total_cost_cents / count

            warning = (
                f"Book only covers {filled_count}/{count} contracts. "
                f"Remaining {unfilled} estimated via sqrt impact model."
            )

        expected_fill: float = avg_fill_cents / 100.0
        slippage_cents: float = avg_fill_cents - ref_cents
        total_cost_usd: float = total_cost_cents / 100.0

    # --- Case 2: Empty book ---
    else:
        is_price_setter = volume == 0 and open_interest == 0

        if is_price_setter:
            # No reference at all. Our limit order becomes the market.
            expected_fill = ref_price
            slippage_cents = 0.0
            total_cost_usd = count * ref_price
            warning = (
                "PRICE-SETTER: Zero volume, zero OI, empty book. "
                "Your order defines the market price. "
                "Use a limit order and be patient."
            )
        else:
            # There is some OI or volume, but no resting orders.
            sqrt_dp = estimate_sqrt_impact(
                count,
                ref_price,
                volume,
                volatility=_implied_vol(ref_price),
            )
            expected_fill = ref_price + sqrt_dp
            slippage_cents = sqrt_dp * 100.0
            total_cost_usd = count * expected_fill
            warning = (
                f"Empty book but OI={open_interest}, vol={volume}. "
                f"Estimated {slippage_cents:.1f}c slippage via sqrt model."
            )

    # Compute slippage percentage
    slippage_pct: float
    if ref_cents > 0:
        slippage_pct = (slippage_cents / ref_cents) * 100.0
    else:
        slippage_pct = 0.0

    return ImpactEstimate(
        ticker=ticker,
        side=side,
        count=count,
        current_price=ref_price,
        expected_fill=expected_fill,
        slippage_cents=slippage_cents,
        slippage_pct=slippage_pct,
        total_cost=total_cost_usd,
        is_price_setter=is_price_setter,
        book_depth_yes=book_depth_yes,
        book_depth_no=book_depth_no,
        warning=warning,
    )


# ---------------------------------------------------------------------------
# Implied vol heuristic for binary contracts
# ---------------------------------------------------------------------------


def _implied_vol(price: float) -> float:
    """
    Rough implied vol for a binary contract at a given price.

    Contracts near 50c have lower relative vol; contracts near the
    extremes (5c, 95c) have massive relative vol because a small
    absolute move is a huge percentage of the price.

    We use a simple heuristic: vol = 0.5 * sqrt(p * (1-p)) / max(p, 0.01)
    which peaks at the extremes. Clamped to [0.05, 0.50].
    """
    p: float = max(min(price, 0.99), 0.01)
    bernoulli_std: float = math.sqrt(p * (1.0 - p))
    vol: float = 0.5 * bernoulli_std / p
    return max(0.05, min(vol, 0.50))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def format_impact_report(estimates: list[ImpactEstimate]) -> str:
    """
    Format a list of impact estimates as an ASCII table.

    Works with Rich if you wrap it in a Text() object, but also
    readable in plain terminals and Jupyter notebooks.
    """
    header: str = (
        f"{'Ticker':<20} {'Side':<10} {'Qty':>5} {'Price':>7} "
        f"{'Fill':>7} {'Slip':>7} {'Slip%':>7} {'Cost$':>8} "
        f"{'Setter':>7} {'Depth$':>8} Warning"
    )
    sep: str = "-" * len(header)
    lines: list[str] = [sep, header, sep]

    for e in estimates:
        depth: float = (
            e.book_depth_yes if e.side in ("buy_yes", "sell_no") else e.book_depth_no
        )
        setter_flag: str = "YES" if e.is_price_setter else ""
        line: str = (
            f"{e.ticker:<20} {e.side:<10} {e.count:>5} "
            f"{e.current_price:>7.2f} {e.expected_fill:>7.2f} "
            f"{e.slippage_cents:>6.1f}c {e.slippage_pct:>6.1f}% "
            f"{e.total_cost:>8.2f} {setter_flag:>7} {depth:>8.2f} "
            f"{e.warning}"
        )
        lines.append(line)

    lines.append(sep)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from config import TICKERS

    msim_ticker: str = TICKERS["MSIM"]
    dbis_ticker: str = TICKERS["DBIS"]

    print("=" * 80)
    print("IL-9 Market Impact Analysis")
    print("=" * 80)

    estimates: list[ImpactEstimate] = []

    # -----------------------------------------------------------------------
    # Scenario 1: MSIM — Empty book, zero everything (most common case)
    # -----------------------------------------------------------------------
    print("\n--- Scenario 1: MSIM empty book (price-setter) ---")
    est = estimate_impact(
        ticker=msim_ticker,
        side="buy_yes",
        count=10,
        current_yes_price=0.10,
        ob_yes=[],
        ob_no=[],
        volume=0,
        open_interest=0,
    )
    estimates.append(est)

    # -----------------------------------------------------------------------
    # Scenario 2: MSIM — Thin book (a few resting orders)
    # -----------------------------------------------------------------------
    print("--- Scenario 2: MSIM thin book ---")
    est = estimate_impact(
        ticker=msim_ticker,
        side="buy_yes",
        count=10,
        current_yes_price=0.10,
        ob_yes=[
            {"price": 12, "quantity": 3},
            {"price": 15, "quantity": 2},
            {"price": 20, "quantity": 5},
        ],
        ob_no=[
            {"price": 88, "quantity": 2},
        ],
        volume=5,
        open_interest=10,
    )
    estimates.append(est)

    # -----------------------------------------------------------------------
    # Scenario 3: MSIM — Moderate book (unlikely but illustrative)
    # -----------------------------------------------------------------------
    print("--- Scenario 3: MSIM moderate book ---")
    est = estimate_impact(
        ticker=msim_ticker,
        side="buy_yes",
        count=10,
        current_yes_price=0.10,
        ob_yes=[
            {"price": 11, "quantity": 20},
            {"price": 12, "quantity": 30},
            {"price": 13, "quantity": 25},
            {"price": 15, "quantity": 50},
        ],
        ob_no=[
            {"price": 88, "quantity": 15},
            {"price": 89, "quantity": 20},
        ],
        volume=50,
        open_interest=100,
    )
    estimates.append(est)

    # -----------------------------------------------------------------------
    # Scenario 4: DBIS — Buy NO (we think Biss is overpriced)
    # -----------------------------------------------------------------------
    print("--- Scenario 4: DBIS buy NO (empty book) ---")
    est = estimate_impact(
        ticker=dbis_ticker,
        side="buy_no",
        count=10,
        current_yes_price=0.24,
        ob_yes=[],
        ob_no=[],
        volume=0,
        open_interest=0,
    )
    estimates.append(est)

    print("--- Scenario 5: DBIS buy NO (thin book) ---")
    est = estimate_impact(
        ticker=dbis_ticker,
        side="buy_no",
        count=10,
        current_yes_price=0.24,
        ob_yes=[
            {"price": 25, "quantity": 5},
        ],
        ob_no=[
            {"price": 78, "quantity": 4},
            {"price": 80, "quantity": 3},
            {"price": 85, "quantity": 5},
        ],
        volume=8,
        open_interest=15,
    )
    estimates.append(est)

    # -----------------------------------------------------------------------
    # Print the report
    # -----------------------------------------------------------------------
    print("\n")
    print(format_impact_report(estimates))

    # -----------------------------------------------------------------------
    # Summary: what does this mean for our $100 bankroll?
    # -----------------------------------------------------------------------
    print("\n--- Bankroll Impact Summary ---")
    bankroll: float = 100.0
    for e in estimates:
        pct_of_bankroll: float = (e.total_cost / bankroll) * 100.0
        status: str = "PRICE-SETTER" if e.is_price_setter else "OK"
        if e.slippage_pct > 20.0:
            status = "HIGH SLIPPAGE"
        print(
            f"  {e.ticker:<20} {e.side:<10} "
            f"cost=${e.total_cost:>6.2f} ({pct_of_bankroll:>5.1f}% of bankroll) "
            f"slip={e.slippage_cents:>5.1f}c [{status}]"
        )

    print("\nKey takeaway: In a market this thin, limit orders are mandatory.")
    print("Market orders will get destroyed by slippage or simply won't fill.")
    print("Be patient. Post limit orders. You ARE the market maker here.")
