"""
IL-9 Democratic Primary — Configuration

All contract tickers, strategy parameters, and market metadata.
"""

from dataclasses import dataclass, field
from typing import Dict, List

# ============================================================================
# Kalshi IL-9 Series
# ============================================================================

SERIES_TICKER = "KXIL9D"

# All candidate tickers
TICKERS: Dict[str, str] = {
    "MSIM": "KXIL9D-26-MSIM",   # Mike Simmons
    "DBIS": "KXIL9D-26-DBIS",   # Daniel Biss
    "KA":   "KXIL9D-26-KA",     # Kat Abughazaleh
    "LFIN": "KXIL9D-26-LFIN",   # Laura Fine
    "JS":   "KXIL9D-26-JS",     # Jan Schakowsky
    "PAND": "KXIL9D-26-PAND",   # Phil Andrew
    "BAMI": "KXIL9D-26-BAMI",   # Bushra Amiwala
    "HHUY": "KXIL9D-26-HHUY",   # Hoan Huynh
    "NPYA": "KXIL9D-26-NPYA",   # Nick Pyati
    "SPOL": "KXIL9D-26-SPOL",   # Sam Polan
    "HROS": "KXIL9D-26-HROS",   # Howard Rosenblum
}

CANDIDATE_NAMES: Dict[str, str] = {
    "KXIL9D-26-MSIM": "Mike Simmons",
    "KXIL9D-26-DBIS": "Daniel Biss",
    "KXIL9D-26-KA":   "Kat Abughazaleh",
    "KXIL9D-26-LFIN": "Laura Fine",
    "KXIL9D-26-JS":   "Jan Schakowsky",
    "KXIL9D-26-PAND": "Phil Andrew",
    "KXIL9D-26-BAMI": "Bushra Amiwala",
    "KXIL9D-26-HHUY": "Hoan Huynh",
    "KXIL9D-26-NPYA": "Nick Pyati",
    "KXIL9D-26-SPOL": "Sam Polan",
    "KXIL9D-26-HROS": "Howard Rosenblum",
}

# Short names for display
SHORT_NAMES: Dict[str, str] = {
    "KXIL9D-26-MSIM": "MSIM",
    "KXIL9D-26-DBIS": "DBIS",
    "KXIL9D-26-KA":   "KA",
    "KXIL9D-26-LFIN": "LFIN",
    "KXIL9D-26-JS":   "JS",
    "KXIL9D-26-PAND": "PAND",
    "KXIL9D-26-BAMI": "BAMI",
    "KXIL9D-26-HHUY": "HHUY",
    "KXIL9D-26-NPYA": "NPYA",
    "KXIL9D-26-SPOL": "SPOL",
    "KXIL9D-26-HROS": "HROS",
}

# Primary candidates we actively trade
PRIMARY_TICKERS: List[str] = [
    "KXIL9D-26-MSIM",  # Long target
    "KXIL9D-26-DBIS",  # Short target
    "KXIL9D-26-KA",    # Short target
    "KXIL9D-26-LFIN",  # Monitor
]


# ============================================================================
# Strategy Parameters
# ============================================================================

@dataclass
class StrategyConfig:
    """Trading strategy configuration."""

    bankroll: float = 100.0          # Total capital (USD)
    kelly_scale: float = 0.25        # Quarter-Kelly
    max_position_pct: float = 0.30   # Max 30% of bankroll in one contract
    max_slippage_pct: float = 0.50   # Don't trade if slippage > 50% of edge
    rebalance_threshold: float = 0.20  # Rebalance when position deviates >20%

    # Our probability estimates (from polls + analysis)
    prob_estimates: Dict[str, float] = field(default_factory=lambda: {
        "KXIL9D-26-MSIM": 0.10,  # 10% (latest poll)
        "KXIL9D-26-DBIS": 0.24,  # 24% (latest poll)
        "KXIL9D-26-KA":   0.20,  # 20% (latest poll)
        "KXIL9D-26-LFIN": 0.14,  # 14% (latest poll)
        "KXIL9D-26-JS":   0.00,  # Not running
        "KXIL9D-26-PAND": 0.07,  # 7%
        "KXIL9D-26-BAMI": 0.06,  # 6%
    })

    # Target side for each ticker: "long_yes", "short_yes" (buy NO), or None
    target_sides: Dict[str, str] = field(default_factory=lambda: {
        "KXIL9D-26-MSIM": "long_yes",   # Buy YES — believe Simmons underpriced
        "KXIL9D-26-DBIS": "short_yes",  # Buy NO — believe Biss overpriced
        "KXIL9D-26-KA":   "short_yes",  # Buy NO — believe Abughazaleh overpriced
    })


# ============================================================================
# Polymarket IL-9 mapping (for cross-platform monitoring)
# ============================================================================

POLYMARKET_EVENT_SLUG = "il-09-democratic-primary-winner"

POLYMARKET_TOKENS: Dict[str, Dict[str, str]] = {
    "Biss": {
        "yes": "98027045933588113391594002302872713345778312449435531295569311707133119519467",
        "no":  "87138886338803690966189519615724968337140703583536608936756941718727622150140",
        "condition_id": "0x4c4eed6cb866f79c0f835eb975316dd63dcdd5d14753b6585480e6be1acb6621",
    },
    "Abughazaleh": {
        "yes": "33426386911449201200993871753296476633318725410732164535022150801792475186532",
        "no":  "112025433405666226446669622942471900940522851738277028399943457236315006973467",
        "condition_id": "0x55d72c54a82795de9e8ae8ccde764a3d49d4dc55af23c5043b2c049a47c73b33",
    },
    "Fine": {
        "yes": "25860577892871750390080842523241718753100902708921880792354927015073278860119",
        "no":  "103356578507319235908052201619577872670219960438511018631416359188978305244702",
        "condition_id": "0xd3aa8486173859dc9a0bc377f2774aae02f39dc3ac3c4b816ea93a40ecf669f0",
    },
    "Schakowsky": {
        "yes": "21562235124507422119256160643222981576308583504834497667425089970238988493268",
        "no":  "17090741728712324009634334251646301422398760998917620743674910855869569970495",
        "condition_id": "0x1adca37a094a4f6dfd1a823575e381e4c7a99b6ef63b316452decd6666577502",
    },
}


# ============================================================================
# API Configuration
# ============================================================================

KALSHI_TRADING_URL = "https://trading-api.kalshi.com/trade-api/v2"
KALSHI_ELECTIONS_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_DEMO_URL = "https://demo-api.kalshi.co/trade-api/v2"
KALSHI_WS_URL = "wss://trading-api.kalshi.com/trade-api/ws/v2"

# Monitor refresh interval (seconds)
MONITOR_REFRESH_INTERVAL = 5
STRATEGY_EVAL_INTERVAL = 30
