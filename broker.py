"""
Kalshi Broker — Standalone async client for prediction market trading.

Dual API support:
  - Trading API (authenticated) for orders, positions, balance
  - Elections API (public) for market data, orderbooks

Prices are integer cents (1-99) at the API boundary.
HMAC-SHA256 signing per Kalshi spec: message = timestamp + method + path [+ body].
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv
import os

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRADING_URL = "https://api.elections.kalshi.com/trade-api/v2"
ELECTIONS_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_URL = "https://demo-api.kalshi.co/trade-api/v2"


class KalshiBrokerError(Exception):
    """Raised when the Kalshi API returns an error."""

    def __init__(self, status: int, detail: str, endpoint: str) -> None:
        self.status = status
        self.detail = detail
        self.endpoint = endpoint
        super().__init__(f"[{status}] {endpoint} — {detail}")


class KalshiBroker:
    """
    Standalone async Kalshi broker.

    Handles HMAC signing, dual-API routing, and cent/float conversion
    so callers don't have to think about any of that plumbing.
    """

    def __init__(
        self,
        api_key: str | None = None,
        private_key: str | None = None,
        demo: bool = False,
    ) -> None:
        load_dotenv()
        self.api_key: str = api_key or os.environ.get("KALSHI_API_KEY", "")
        _pk: str = private_key or os.environ.get("KALSHI_PRIVATE_KEY", "")

        if not self.api_key:
            raise ValueError(
                "KALSHI_API_KEY not provided and not found in environment"
            )
        if not _pk:
            raise ValueError(
                "KALSHI_PRIVATE_KEY not provided and not found in environment"
            )

        # Load RSA private key for request signing
        # Support multiple formats:
        #   1. Raw PEM with real newlines
        #   2. PEM with escaped \n (from .env files)
        #   3. Base64-encoded PEM (for env vars that mangle newlines)
        pk_str = _pk.strip()
        if "\\n" in pk_str and "-----" in pk_str:
            pk_str = pk_str.replace("\\n", "\n")
        elif not pk_str.startswith("-----"):
            # Might be base64-encoded PEM
            try:
                import base64 as _b64
                pk_str = _b64.b64decode(pk_str).decode("utf-8")
            except Exception:
                pass
        self._private_key = serialization.load_pem_private_key(
            pk_str.encode("utf-8"), password=None
        )

        self._trading_url: str = DEMO_URL if demo else TRADING_URL
        self._elections_url: str = ELECTIONS_URL
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        """Lazy-init a shared session so we reuse the connection pool."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Drain the connection pool."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # HMAC signing
    # ------------------------------------------------------------------

    def _sign(self, timestamp: str, method: str, path: str) -> str:
        """
        RSA-PSS SHA256 signature per Kalshi API v2 spec.

        Message layout: timestamp + method + FULL_PATH (with /trade-api/v2 prefix).
        Path must NOT include query parameters.
        Signature is base64-encoded.
        """
        path_without_query = path.split("?")[0]
        message = f"{timestamp}{method}{path_without_query}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        authenticated: bool = True,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        """
        Fire an HTTP request against the appropriate Kalshi API.

        Args:
            method: HTTP verb (GET, POST, DELETE, ...).
            endpoint: Path after the base URL, e.g. ``/portfolio/balance``.
            data: JSON body (POST only).
            params: Query-string parameters.
            authenticated: Whether to attach HMAC headers.
            base_url: Override the base URL (defaults to trading API).
        """
        url = (base_url or self._trading_url) + endpoint
        session = await self._get_session()

        headers: dict[str, str] = {"Content-Type": "application/json"}

        if authenticated:
            ts = str(int(time.time() * 1000))
            # Sign the FULL path including /trade-api/v2 prefix
            full_path = "/trade-api/v2" + endpoint
            sig = self._sign(ts, method, full_path)
            headers["KALSHI-ACCESS-KEY"] = self.api_key
            headers["KALSHI-ACCESS-SIGNATURE"] = sig
            headers["KALSHI-ACCESS-TIMESTAMP"] = ts

        logger.debug("%s %s params=%s data=%s", method, url, params, data)

        async with session.request(
            method,
            url,
            headers=headers,
            json=data,
            params=params,
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                logger.error(
                    "Kalshi API error: %d %s — %s", resp.status, endpoint, text
                )
                raise KalshiBrokerError(resp.status, text, endpoint)
            try:
                return json.loads(text)  # type: ignore[no-any-return]
            except json.JSONDecodeError:
                return {"raw_text": text}

    async def _trading_get(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._request("GET", endpoint, params=params)

    async def _trading_post(
        self, endpoint: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request("POST", endpoint, data=data)

    async def _trading_delete(self, endpoint: str) -> dict[str, Any]:
        return await self._request("DELETE", endpoint)

    async def _elections_get(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Hit the public Elections API — no auth, no rate-limit pain."""
        return await self._request(
            "GET",
            endpoint,
            params=params,
            authenticated=False,
            base_url=self._elections_url,
        )

    # ==================================================================
    # Portfolio endpoints (Trading API, authenticated)
    # ==================================================================

    async def get_balance(self) -> dict[str, Any]:
        """
        GET /portfolio/balance

        Returns raw Kalshi response with ``balance`` in cents.
        """
        return await self._trading_get("/portfolio/balance")

    async def get_positions(self) -> list[dict[str, Any]]:
        """
        GET /portfolio/positions

        Returns the list of position dicts from the API.
        Positions with zero quantity are filtered out.
        """
        resp = await self._trading_get("/portfolio/positions")
        positions: list[dict[str, Any]] = []
        for pos in resp.get("market_positions", resp.get("positions", [])):
            # Skip flat positions
            yes_qty = pos.get("total_traded", 0)
            if (
                pos.get("position", 0) == 0
                and pos.get("market_exposure", 0) == 0
                and yes_qty == 0
            ):
                continue
            positions.append(pos)
        return positions

    async def get_position(self, ticker: str) -> dict[str, Any] | None:
        """Look up a single ticker in the positions list."""
        for pos in await self.get_positions():
            if pos.get("ticker") == ticker:
                return pos
        return None

    # ==================================================================
    # Market data endpoints (Elections API, public)
    # ==================================================================

    async def get_market(self, ticker: str) -> dict[str, Any]:
        """
        GET /markets/{ticker}

        Uses the Elections API — public, no auth needed, lighter rate limits.
        """
        resp = await self._elections_get(f"/markets/{ticker}")
        return resp.get("market", resp)

    async def get_orderbook(self, ticker: str) -> dict[str, Any]:
        """
        GET /markets/{ticker}/orderbook

        Uses the Elections API.
        """
        resp = await self._elections_get(f"/markets/{ticker}/orderbook")
        return resp.get("orderbook", resp)

    async def get_markets(
        self, series_ticker: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """
        GET /markets?series_ticker=...

        Uses the Elections API. Returns all contracts in a series.
        """
        params: dict[str, Any] = {
            "series_ticker": series_ticker,
            "limit": limit,
        }
        resp = await self._elections_get("/markets", params=params)
        return resp.get("markets", [])

    # ==================================================================
    # Order management (Trading API, authenticated)
    # ==================================================================

    async def place_order(
        self,
        ticker: str,
        action: str,
        side: str,
        count: int,
        order_type: str = "limit",
        yes_price: int | None = None,
        no_price: int | None = None,
    ) -> dict[str, Any]:
        """
        POST /portfolio/orders

        Args:
            ticker: Contract ticker, e.g. ``KXIL9D-26-MSIM``.
            action: ``"buy"`` or ``"sell"``.
            side: ``"yes"`` or ``"no"``.
            count: Number of contracts.
            order_type: ``"limit"`` or ``"market"``.
            yes_price: Limit price in cents (1-99) for YES side.
            no_price: Limit price in cents (1-99) for NO side.

        Returns:
            The ``order`` dict from the Kalshi response.
        """
        if action not in ("buy", "sell"):
            raise ValueError(f"action must be 'buy' or 'sell', got {action!r}")
        if side not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no', got {side!r}")
        if count <= 0:
            raise ValueError(f"count must be positive, got {count}")

        payload: dict[str, Any] = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "type": order_type,
        }

        if yes_price is not None:
            if not (1 <= yes_price <= 99):
                raise ValueError(f"yes_price must be 1-99 cents, got {yes_price}")
            payload["yes_price"] = yes_price

        if no_price is not None:
            if not (1 <= no_price <= 99):
                raise ValueError(f"no_price must be 1-99 cents, got {no_price}")
            payload["no_price"] = no_price

        logger.info(
            "Placing order: %s %s %s x%d @ yes=%s no=%s",
            action,
            side,
            ticker,
            count,
            yes_price,
            no_price,
        )

        resp = await self._trading_post("/portfolio/orders", data=payload)
        order = resp.get("order", resp)
        logger.info("Order placed: %s — status=%s", order.get("order_id"), order.get("status"))
        return order

    async def cancel_order(self, order_id: str) -> bool:
        """
        DELETE /portfolio/orders/{order_id}

        Returns True on success, False on failure.
        """
        try:
            await self._trading_delete(f"/portfolio/orders/{order_id}")
            logger.info("Cancelled order %s", order_id)
            return True
        except KalshiBrokerError as exc:
            logger.error("Failed to cancel order %s: %s", order_id, exc)
            return False

    async def get_orders(
        self,
        ticker: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        GET /portfolio/orders

        Optional filters: ticker, status (resting, pending, canceled, executed).
        """
        params: dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        resp = await self._trading_get("/portfolio/orders", params=params)
        return resp.get("orders", [])

    async def get_fills(
        self, ticker: str | None = None
    ) -> list[dict[str, Any]]:
        """
        GET /portfolio/fills

        Returns executed trade fills, optionally filtered by ticker.
        """
        params: dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        resp = await self._trading_get("/portfolio/fills", params=params)
        return resp.get("fills", [])

    # ==================================================================
    # Convenience shorthands
    # ==================================================================

    async def buy_yes(
        self, ticker: str, count: int, limit_price: int
    ) -> dict[str, Any]:
        """Buy YES contracts at ``limit_price`` cents."""
        return await self.place_order(
            ticker, "buy", "yes", count, "limit", yes_price=limit_price
        )

    async def buy_no(
        self, ticker: str, count: int, limit_price: int
    ) -> dict[str, Any]:
        """Buy NO contracts at ``limit_price`` cents."""
        return await self.place_order(
            ticker, "buy", "no", count, "limit", no_price=limit_price
        )

    async def sell_yes(
        self, ticker: str, count: int, limit_price: int
    ) -> dict[str, Any]:
        """Sell (close) YES position at ``limit_price`` cents."""
        return await self.place_order(
            ticker, "sell", "yes", count, "limit", yes_price=limit_price
        )

    async def sell_no(
        self, ticker: str, count: int, limit_price: int
    ) -> dict[str, Any]:
        """Sell (close) NO position at ``limit_price`` cents."""
        return await self.place_order(
            ticker, "sell", "no", count, "limit", no_price=limit_price
        )

    # ==================================================================
    # Context manager
    # ==================================================================

    async def __aenter__(self) -> KalshiBroker:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


# ======================================================================
# CLI smoke test
# ======================================================================

async def _main() -> None:
    """Quick integration test — hit real APIs, print results."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    )

    async with KalshiBroker() as broker:
        # 1. Balance
        print("\n=== Balance ===")
        balance = await broker.get_balance()
        cents = balance.get("balance", 0)
        print(f"  Balance: ${cents / 100:.2f} ({cents}c)")

        # 2. All KXIL9D markets
        print("\n=== KXIL9D Markets ===")
        markets = await broker.get_markets("KXIL9D")
        for m in markets:
            ticker = m.get("ticker", "?")
            title = m.get("title", m.get("subtitle", ""))
            yes_bid = m.get("yes_bid", 0)
            yes_ask = m.get("yes_ask", 0)
            volume = m.get("volume", 0)
            print(f"  {ticker:<22} bid={yes_bid:>2}c  ask={yes_ask:>2}c  vol={volume:>6}  {title}")

        # 3. Orderbook for KXIL9D-26-MSIM
        print("\n=== Orderbook: KXIL9D-26-MSIM ===")
        ob = await broker.get_orderbook("KXIL9D-26-MSIM")
        print(f"  YES bids: {ob.get('yes', [])[:5]}")
        print(f"   NO bids: {ob.get('no', [])[:5]}")

        # 4. Current positions
        print("\n=== Positions ===")
        positions = await broker.get_positions()
        if not positions:
            print("  (no open positions)")
        for pos in positions:
            print(f"  {pos.get('ticker', '?')}: {pos}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
