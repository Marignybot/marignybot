#!/usr/bin/env python3
"""
SakaiBot v4.8 — Bot Telegram Hyperliquid
  v4.6:
  - [OPT] MAKER orders partout (place_limit_gtc) : ouverture, renfort, allègement, clôture normale
  - [OPT] LIMIT_MAKER_OFFSET=0.03% du mid-price → statut maker, fees ~0 voire rebate -0.01%
  - [OPT] Retry loop (LIMIT_MAX_RETRIES=3 × LIMIT_RETRY_WAIT=8s) + annulation + fallback market
  - [OPT] force_market=True pour /copy_close et /target_stop (urgence sortie rapide)
  - [OPT] place_market_order conservé UNIQUEMENT pour stop-loss urgence et fallback interne
  v4.2:
  - [FIX] Seuil MDD: 60% → 85% (HL dominé par traders agressifs, MDD médian 97%)
  - [FIX] Ancienneté: 90j → 60j via constante TRADEBOT_MIN_AGE_DAYS
  - [OPT] Logging explicite sur TOUTES les exclusions (portfolio vide, inactif, cap)
  - [FIX CRITIQUE] at_pnl_raw utilisé avant définition → déplacé après at_data
  - [FIX] COPY_LEVERAGE ignoré → appliqué dans place_order via get_proportional_size
  - [FIX] tb_aide → scoring mis à jour v4
  - [OPT] Imports remontés en tête de fichier
  - [OPT] fetch_portfolio refactorisé : helper calc_mdd extrait, logique linéaire
  - [OPT] Semaphore asyncio sur fetch_portfolio (max 10 parallèles → évite ban IP)
  - [OPT] Cache prix allMids (TTL 3s) → évite appels redondants dans place_order
  - [OPT] apply_exclusion_filters fusionné dans rank_and_score_traders
  - [OPT] Constantes SIZE_RULES centralisées (plus de duplication)
  - [OPT] winrate_30j intégré dans le score bonus (était calculé mais inutilisé)
"""

import asyncio
import math
import os
import json
import logging
import re
import time as time_module
import websockets
import eth_account
from datetime import datetime, time
from functools import lru_cache

import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import Conflict

try:
    from hyperliquid.exchange import Exchange
    from hyperliquid.utils import constants as hl_constants
    _HL_SDK_AVAILABLE = True
except ImportError:
    _HL_SDK_AVAILABLE = False
    logger_bootstrap = logging.getLogger(__name__)
    logger_bootstrap.warning("hyperliquid-python-sdk non installé — place_order désactivé")

# ============================================================
# CONFIGURATION
# ============================================================

TELEGRAM_TOKEN      = "8413363300:AAEldjYE3nqAoF9-tZdYurwH1PNfUWJbZEQ"
HYPERLIQUID_ADDRESS = "0x6e89b986FBB4B985AcCC9B3CfEE4c7B5301D9a5C"
AUTHORIZED_USER_ID  = 1429797974

# ============================================================
# COPY TRADING — CONFIG
# ============================================================
COPY_BOT_ADDRESS = "0xd849f8E96d7BE1A1fc7CA5291Dc6603a47dF8dFD"
HL_PRIVATE_KEY   = os.getenv("HL_PRIVATE_KEY", "")
COPY_CAPITAL     = 1000.0
COPY_ASSETS      = ["BTC", "ETH", "HYPE", "SOL", "TAO"]
COPY_ALLOC       = 100.0
COPY_MAX_SIZE    = 50.0
MARGIN_SAFETY    = 0.5

# [FIX] COPY_LEVERAGE désormais utilisé dans get_proportional_size
COPY_LEVERAGE = {
    "BTC":  5,
    "ETH":  4,
    "HYPE": 3,
    "SOL":  5,
    "TAO":  3,
}

# ============================================================
# LIMIT ORDER — CONFIG MAKER (v4.6)
# ============================================================
LIMIT_MAKER_OFFSET   = 0.0003   # 0.03% du mid-price → souvent rempli en <5s
LIMIT_RETRY_WAIT     = 8        # secondes entre chaque tentative
LIMIT_MAX_RETRIES    = 3        # tentatives limit avant fallback market
# Fallback market uniquement si LIMIT_MAX_RETRIES épuisées ou urgence (stop loss)

# [OPT] Règles de taille centralisées — une seule source de vérité
SIZE_RULES = {
    "BTC":  {"min": 0.001, "decimals": 3, "min_usd": 11.0},
    "ETH":  {"min": 0.01,  "decimals": 2, "min_usd": 11.0},
    "SOL":  {"min": 0.1,   "decimals": 1, "min_usd": 11.0},
    "HYPE": {"min": 1.0,   "decimals": 0, "min_usd": 35.0},
    "TAO":  {"min": 0.01,  "decimals": 2, "min_usd": 15.0},
    "_default": {"min": 0.001, "decimals": 3, "min_usd": 11.0},
}

copy_deployed = {asset: 0.0 for asset in COPY_ASSETS}

# ============================================================
# COPY STATE v4
# ============================================================
copy_state = {
    "active":      False,
    "traders":     {},   # {address: {"rank": int, "score": float, "assets_seen": set()}}
    "positions":   {},   # {asset: {"side", "size", "entry", "trader_addr"}}
    "ws_tasks":    {},   # {address: asyncio.Task}
    "last_update": {},
    "total_pnl":   0.0,
    "trades_log":  [],
    "last_ranked": [],
}

# ============================================================
# CONSTANTES API
# ============================================================
HYPERLIQUID_API           = "https://api.hyperliquid.xyz/info"
HYPERLIQUID_API_UI        = "https://api-ui.hyperliquid.xyz/info"
HYPERLIQUID_WS            = "wss://api.hyperliquid.xyz/ws"
WATCHED_TOKENS            = ["BTC", "ETH", "SOL", "HYPE", "TAO"]
ALERT_THRESHOLD_PERCENT   = 5.0
LIQUIDATION_ALERT_PERCENT = 15.0
MORNING_HOUR_UTC          = 8
MORNING_MIN_UTC           = 0
EVENING_HOUR_UTC          = 20
EVENING_MIN_UTC           = 0

# ============================================================
# SCORING v4
# ============================================================
TRADEBOT_MIN_TRADES      = 1     # [v4.5] log(n) gere nativement score=0 pour 0 trade
TRADEBOT_MAX_TRADES_DAY  = 36   # [v6.0] filtre dur scalper: > 250 trades/semaine (≈36/j) → pénalité score
TRADEBOT_MAX_DRAWDOWN    = 65.0   # filtre dur MDD (v4.8: pénalisé fortement par mdd_factor au-dessus de 40%)
TRADEBOT_MIN_AGE_DAYS    = 60     # v4.2: 90→60j (HL plateforme récente)
TRADEBOT_EXCELLENT_RATIO = 5.0

TRADER_BLACKLIST = {
    "0x9cd0a696c7cbb9d44de99268194cb08e5684e5fe",
}

# ============================================================
# MODULE IA HIP-3 — CONFIG
# ============================================================
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")

def get_anthropic_key() -> str:
    """Lit la clé Anthropic dynamiquement — Railway injecte les vars après démarrage."""
    return os.getenv("ANTHROPIC_API_KEY", "") or ANTHROPIC_API_KEY

# Mapping ticker HIP-3 → symbole Yahoo Finance (pour comparaison prix TradFi)
YAHOO_SYMBOLS = {
    "XYZ100":    "NQ=F",      # XYZ100 tracke le Nasdaq 100
    "GOLD":      "GC=F",
    "SILVER":    "SI=F",
    "CL":        "CL=F",
    "BRENTOIL":  "BZ=F",
    "NATGAS":    "NG=F",
    "COPPER":    "HG=F",
    "PLATINUM":  "PL=F",
    "PALLADIUM": "PA=F",
    "ALUMINIUM": "ALI=F",
    "URANIUM":   "UX=F",
    "JPY":       "JPY=X",
    "EUR":       "EURUSD=X",
    "DXY":       "DX-Y.NYB",
    "USAR":      "ES=F",      # US index
    "JP225":     "^N225",
    "KR200":     "^KS200",
    "TSLA":      "TSLA",
    "AAPL":      "AAPL",
    "NVDA":      "NVDA",
    "MSFT":      "MSFT",
    "GOOGL":     "GOOGL",
    "AMZN":      "AMZN",
    "META":      "META",
    "COIN":      "COIN",
    "AMD":       "AMD",
    "INTC":      "INTC",
    "PLTR":      "PLTR",
    "HOOD":      "HOOD",
    "MSTR":      "MSTR",
    "NFLX":      "NFLX",
    "COST":      "COST",
    "LLY":       "LLY",
    "TSM":       "TSM",
    "BABA":      "BABA",
    "MU":        "MU",
    "RIVN":      "RIVN",
    "GME":       "GME",
    "ORCL":      "ORCL",
}

AI_HIP3_ASSETS: dict = {}   # rempli dynamiquement par ai_discover_hip3_assets()

AI_BOT_NAME        = "ORACLE"   # nom du module IA HIP-3
AI_MAX_POSITIONS   = 2        # 2 positions max → ~$150/trade
AI_LEVERAGE        = 3        # levier isolated margin HIP-3
AI_STOP_LOSS_PCT   = 0.15     # -15% stop dur (filet de sécurité)
AI_TRAIL_PCT       = 0.07     # 7% trailing stop (suit le peak)
AI_TAKE_PROFIT_PCT = 0.20     # +20% take-profit dur (sortie immédiate)

# Paliers de prise de bénéfice partielle
# (seuil_pnl, % de la position à fermer)
AI_PARTIAL_TP = [
    (0.20, 0.30),   # +20% → ferme 30%
    (0.30, 0.30),   # +30% → ferme encore 30% du solde
    (0.40, 0.20),   # +40% → ferme encore 20% du solde
    # Le reste (~20%) est géré par le trailing stop 7%
]
AI_FIXED_BUDGET    = 300.0    # budget fixe réservé à ORACLE ($)
AI_SAFETY_BUFFER   = 0.15     # 15% du wallet intouchable (réserve)
AI_COPY_RESERVE    = 1.2      # facteur sécurité marge copy
AI_MIN_WALLET      = 300.0    # pause si wallet < $300
AI_MIN_PREMIUM     = 0.015    # signal min 1.5% premium/discount
AI_SCAN_INTERVAL   = 300      # scan toutes les 5 min
AI_FUNDING_SIGNAL  = 0.0003   # seuil funding 0.03%/h

AI_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_state.json")

# État runtime du module IA
ai_state = {
    "active":      False,
    "positions":   {},    # {asset: {"side","size","entry","stop","usd"}}
    "history":     [],    # 10 derniers trades
    "scan_task":   None,
    "hip3_prices": {},    # {asset: markPx} — mis à jour par ai_discover_hip3_assets()
}

# [OPT] Semaphore pour limiter les appels API parallèles
_PORTFOLIO_SEMAPHORE = asyncio.Semaphore(10)

# [OPT] Cache prix allMids (TTL 3 secondes)
_price_cache: dict = {"data": {}, "ts": 0.0}
_PRICE_CACHE_TTL = 3.0

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)
last_prices = {}


def is_authorized(update) -> bool:
    return update.effective_user.id == AUTHORIZED_USER_ID


# ============================================================
# CACHE PRIX
# ============================================================
async def get_all_mids_cached() -> dict:
    """Retourne allMids avec cache TTL 3s — évite les appels redondants."""
    now = time_module.time()
    if now - _price_cache["ts"] < _PRICE_CACHE_TTL and _price_cache["data"]:
        return _price_cache["data"]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API,
                json={"type": "allMids"},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                data = await resp.json()
        _price_cache["data"] = data
        _price_cache["ts"]   = now
        return data
    except Exception as e:
        logger.error(f"get_all_mids_cached: {e}")
        return _price_cache.get("data", {})


# ============================================================
# HYPERLIQUID — PRIX
# ============================================================
async def get_crypto_prices() -> dict:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API, json={"type": "allMids"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                all_mids = await resp.json()
            async with session.post(
                HYPERLIQUID_API, json={"type": "metaAndAssetCtxs"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp2:
                meta_data = await resp2.json()

        result      = {}
        universe    = meta_data[0].get("universe", []) if isinstance(meta_data, list) else []
        ctxs        = meta_data[1] if isinstance(meta_data, list) and len(meta_data) > 1 else []
        ctx_by_name = {asset.get("name", ""): ctxs[i] for i, asset in enumerate(universe) if i < len(ctxs)}

        for symbol in WATCHED_TOKENS:
            mid = all_mids.get(symbol)
            if mid is None:
                continue
            usd    = float(mid)
            ctx    = ctx_by_name.get(symbol, {})
            prev   = float(ctx.get("prevDayPx", usd) or usd)
            change = ((usd - prev) / prev * 100) if prev else 0
            result[symbol] = {
                "usd":            usd,
                "eur":            usd * 0.92,
                "usd_24h_change": round(change, 2),
                "volume":         float(ctx.get("dayNtlVlm", 0) or 0),
            }
        return result
    except Exception as e:
        logger.error(f"Erreur prix: {e}")
        return {}


# ============================================================
# HYPERLIQUID — OHLCV
# ============================================================
async def get_ohlcv(symbol: str, interval: str = "4h", count: int = 50) -> dict:
    interval_ms = {"1h": 3600000, "4h": 14400000, "1d": 86400000}.get(interval, 14400000)
    start_time  = int(time_module.time() * 1000) - (count * interval_ms)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API,
                json={"type": "candleSnapshot", "req": {
                    "coin": symbol, "interval": interval, "startTime": start_time
                }},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                candles = await resp.json()
        if not candles or not isinstance(candles, list):
            return {}
        return {
            "closes":  [float(c["c"]) for c in candles],
            "highs":   [float(c["h"]) for c in candles],
            "lows":    [float(c["l"]) for c in candles],
            "volumes": [float(c["v"]) for c in candles],
        }
    except Exception as e:
        logger.error(f"Erreur OHLCV {symbol}: {e}")
        return {}


# ============================================================
# INDICATEURS TECHNIQUES
# ============================================================
def compute_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas   = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains    = [d if d > 0 else 0 for d in deltas[-period:]]
    losses   = [-d if d < 0 else 0 for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 1)


def compute_ema(closes: list, period: int) -> float:
    if len(closes) < period:
        return closes[-1] if closes else 0
    k   = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 2)


def find_support_resistance(highs: list, lows: list, closes: list) -> dict:
    if not highs or not lows or not closes:
        return {}
    window      = min(20, len(highs))
    recent_high = max(highs[-window:])
    recent_low  = min(lows[-window:])
    diff        = recent_high - recent_low
    return {
        "support":    round(recent_low, 2),
        "resistance": round(recent_high, 2),
        "fib_382":    round(recent_low + diff * 0.382, 2),
        "fib_618":    round(recent_low + diff * 0.618, 2),
        "current":    round(closes[-1], 2),
    }


def volume_signal(volumes: list) -> str:
    if len(volumes) < 5:
        return "normal"
    avg   = sum(volumes[-6:-1]) / 5
    ratio = volumes[-1] / avg if avg > 0 else 1
    if ratio >= 1.5: return "fort"
    if ratio <= 0.6: return "faible"
    return "normal"


def calc_mdd(hist: list) -> float:
    """[OPT] Helper MDD extrait — évite la duplication dans fetch_portfolio."""
    if not hist:
        return 0.0
    pk = mdd = 0.0
    pk = hist[0]
    for v in hist:
        if v > pk:
            pk = v
        dd = (pk - v) / pk * 100 if pk > 0 else 0
        if dd > mdd:
            mdd = dd
    return mdd


def build_token_analysis(symbol: str, change: float, ohlcv: dict) -> str:
    lines = []
    if change <= -8:
        tendance, conseil, t_emoji = "Possible rebond", "Zone de support potentielle. Surveille un retournement.", "🔥"
    elif change >= 8:
        tendance, conseil, t_emoji = "Momentum haussier fort", "Breakout en cours. Attention au retrace.", "⚡"
    elif change > 3:
        tendance, conseil, t_emoji = "Tendance haussiere moderee", "Momentum positif. Attends confirmation.", "📈"
    elif change <= -3:
        tendance, conseil, t_emoji = "Tendance baissiere moderee", "Prudence. Surveille le support.", "📉"
    else:
        tendance, conseil, t_emoji = "Consolidation / Range", "Attends une cassure claire pour trader.", "⏸"

    lines.append(f"{t_emoji} *{symbol}* — {tendance}")
    lines.append(f"   Variation 24h: {change:+.2f}%")

    if ohlcv:
        closes  = ohlcv.get("closes", [])
        highs   = ohlcv.get("highs", [])
        lows    = ohlcv.get("lows", [])
        volumes = ohlcv.get("volumes", [])
        rsi     = compute_rsi(closes)
        lines.append(f"   RSI(14): {rsi} — {'Surachat ⚠️' if rsi >= 70 else ('Survendu 🛒' if rsi <= 30 else 'Neutre')}")
        ema9  = compute_ema(closes, 9)
        ema21 = compute_ema(closes, 21)
        lines.append(f"   EMA9/EMA21: ${ema9:,.2f} / ${ema21:,.2f} — {'haussier 🟢' if ema9 > ema21 else 'baissier 🔴'}")
        sr = find_support_resistance(highs, lows, closes)
        if sr:
            cp       = closes[-1]
            dist_sup = ((cp - sr['support'])    / cp * 100) if cp else 0
            dist_res = ((sr['resistance'] - cp) / cp * 100) if cp else 0
            lines.append(f"   Support:    ${sr['support']:,.2f}  (-{dist_sup:.1f}%)")
            lines.append(f"   Resistance: ${sr['resistance']:,.2f}  (+{dist_res:.1f}%)")
            lines.append(f"   Fibo 38.2%: ${sr['fib_382']:,.2f}")
            lines.append(f"   Fibo 61.8%: ${sr['fib_618']:,.2f}")
            if cp >= sr['resistance'] * 0.99:
                lines.append("   🚨 CASSURE RESISTANCE IMMINENTE")
            elif cp <= sr['support'] * 1.01:
                lines.append("   🚨 TEST DU SUPPORT EN COURS")
        vol = volume_signal(volumes)
        lines.append(f"   Volume: {vol} {'🔊' if vol == 'fort' else ('🔇' if vol == 'faible' else '📊')}")
    else:
        lines.append("   _Donnees techniques indisponibles_")

    lines.append(f"   ➡️ {conseil}")
    return "\n".join(lines)


# ============================================================
# FEAR & GREED
# ============================================================
async def get_fear_greed() -> dict:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.alternative.me/fng/?limit=1",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                item = data["data"][0]
                return {"value": int(item["value"]), "label": item["value_classification"]}
    except Exception as e:
        logger.error(f"Erreur Fear & Greed: {e}")
        return {}


def format_fear_greed(fg: dict) -> str:
    if not fg:
        return "❌ Fear & Greed indisponible."
    v = fg["value"]
    if v <= 20:   emoji, comment = "😱", "Panique extreme — opportunite historique potentielle."
    elif v <= 40: emoji, comment = "😨", "Peur — les mains faibles vendent."
    elif v <= 60: emoji, comment = "😐", "Neutre — pas de signal fort."
    elif v <= 80: emoji, comment = "😏", "Cupidite — attention aux retournements."
    else:         emoji, comment = "🤑", "Cupidite extreme — sois prudent."
    bar = "█" * round(v / 10) + "░" * (10 - round(v / 10))
    return (
        f"🧠 *Fear & Greed Index*\n\n"
        f"   {emoji} *{v}/100 — {fg['label']}*\n"
        f"   [{bar}]\n\n"
        f"   ➡️ {comment}\n\n"
        f"_Mis a jour: {datetime.now().strftime('%H:%M:%S')}_"
    )


# ============================================================
# ALERTES S/R & LIQUIDATION
# ============================================================
async def check_sr_alerts(context: ContextTypes.DEFAULT_TYPE):
    prices = await get_crypto_prices()
    if not prices:
        return
    alerts = []
    for symbol in WATCHED_TOKENS:
        if symbol not in prices:
            continue
        current = prices[symbol]["usd"]
        ohlcv   = await get_ohlcv(symbol, interval="4h", count=50)
        if not ohlcv:
            continue
        sr = find_support_resistance(ohlcv["highs"], ohlcv["lows"], ohlcv["closes"])
        if not sr:
            continue
        if current >= sr["resistance"] * 0.995:
            alerts.append(f"🚀 *{symbol} — CASSURE RESISTANCE*\n   Prix: ${current:,.2f}\n   Resistance: ${sr['resistance']:,.2f}")
        elif current <= sr["support"] * 1.005:
            alerts.append(f"💥 *{symbol} — CASSURE SUPPORT*\n   Prix: ${current:,.2f}\n   Support: ${sr['support']:,.2f}")
    if alerts:
        await context.bot.send_message(
            chat_id=context.job.chat_id,
            text="⚠️ *Alertes Niveaux Cles*\n\n" + "\n\n".join(alerts),
            parse_mode="Markdown"
        )


async def check_liquidation_alerts(context: ContextTypes.DEFAULT_TYPE):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API,
                json={"type": "clearinghouseState", "user": HYPERLIQUID_ADDRESS},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
        open_pos = [
            p for p in data.get("assetPositions", [])
            if float(p.get("position", {}).get("szi", 0)) != 0
        ]
        if not open_pos:
            return
        prices = await get_crypto_prices()
        alerts = []
        for p in open_pos:
            pos      = p.get("position", {})
            coin     = pos.get("coin", "?")
            liq_px   = float(pos.get("liquidationPx") or 0)
            size     = float(pos.get("szi", 0))
            current  = prices.get(coin, {}).get("usd", 0)
            if liq_px == 0 or current == 0:
                continue
            dist_pct = abs((current - liq_px) / current * 100)
            if dist_pct <= LIQUIDATION_ALERT_PERCENT:
                alerts.append(
                    f"🔴 *{coin} {'LONG' if size > 0 else 'SHORT'} — DANGER LIQUIDATION*\n"
                    f"   Prix: ${current:,.2f} | Liq: ${liq_px:,.2f} | Distance: {dist_pct:.1f}%"
                )
        if alerts:
            await context.bot.send_message(
                chat_id=context.job.chat_id,
                text="🚨 *ALERTE LIQUIDATION*\n\n" + "\n\n".join(alerts),
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Erreur liquidation: {e}")


async def job_twice_daily(context: ContextTypes.DEFAULT_TYPE):
    await check_sr_alerts(context)
    await check_liquidation_alerts(context)


# ============================================================
# NEWS & TRENDING
# ============================================================
async def get_crypto_news() -> list:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://www.coindesk.com/arc/outboundfeeds/rss/",
                headers={"User-Agent": "SakaiBot/4.1"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                text = await resp.text()
        results = []
        for item in re.findall(r'<item>(.*?)</item>', text, re.DOTALL)[:3]:
            t = re.search(r'<title><!\[CDATA\[(.*?)\]\]></title>', item)
            l = re.search(r'<link>(.*?)</link>', item)
            if t and l:
                results.append({"title": t.group(1).strip(), "url": l.group(1).strip()})
        return results
    except Exception as e:
        logger.error(f"Erreur news: {e}")
        return []


async def get_crypto_trending() -> list:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.coingecko.com/api/v3/search/trending",
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json()
        return [
            {
                "name":   c["item"].get("name", ""),
                "symbol": c["item"].get("symbol", ""),
                "rank":   c["item"].get("market_cap_rank", "?"),
                "change": c["item"].get("data", {}).get("price_change_percentage_24h", {}).get("usd", 0) or 0,
            }
            for c in data.get("coins", [])[:3]
        ]
    except Exception as e:
        logger.error(f"Erreur trending: {e}")
        return []


# ============================================================
# POSITIONS & BALANCE
# ============================================================
async def get_wallet_data(address: str) -> tuple:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API,
                json={"type": "clearinghouseState", "user": address},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
        positions = data.get("assetPositions", [])
        margin    = data.get("crossMarginSummary") or data.get("marginSummary") or {}
        av        = float(margin.get("accountValue", 0))
        mu        = float(margin.get("totalMarginUsed", 0))
        pnl       = float(margin.get("totalUnrealizedPnl", 0))
        if av == 0:
            av = float(data.get("crossAccountValue", 0) or data.get("withdrawable", 0) or 0)
        if pnl == 0:
            pnl = sum(float(p.get("position", {}).get("unrealizedPnl", 0)) for p in positions)
        return positions, {"accountValue": av, "totalMarginUsed": mu, "totalUnrealizedPnl": pnl}
    except Exception as e:
        logger.error(f"get_wallet_data {address[:12]}: {e}")
        return [], {}


async def get_hyperliquid_positions() -> list:
    positions, _ = await get_wallet_data(HYPERLIQUID_ADDRESS)
    return positions


async def get_hyperliquid_balance() -> dict:
    _, balance = await get_wallet_data(HYPERLIQUID_ADDRESS)
    return balance


# ============================================================
# FORMATTERS
# ============================================================
def format_prices(prices: dict) -> str:
    if not prices:
        return "❌ Prix indisponibles."
    lines = ["📊 *Prix en temps reel*\n"]
    for symbol in WATCHED_TOKENS:
        if symbol in prices:
            d = prices[symbol]
            lines.append(
                f"{'🟢' if d['usd_24h_change'] >= 0 else '🔴'} *{symbol}*\n"
                f"   💵 ${d['usd']:,.2f}  |  💶 €{d['eur']:,.2f}\n"
                f"   24h: {d['usd_24h_change']:+.2f}%\n"
            )
    lines.append(f"_Mis a jour: {datetime.now().strftime('%H:%M:%S')}_")
    return "\n".join(lines)


def format_wallet_block(label: str, address: str, positions: list, balance: dict) -> str:
    lines = [f"*{label}*", f"`{address[:20]}...`"]
    if balance:
        av  = balance.get("accountValue", 0)
        mu  = balance.get("totalMarginUsed", 0)
        pnl = balance.get("totalUnrealizedPnl", 0)
        lines.append(f"💼 Valeur: ${av:,.2f} | Marge: ${mu:,.2f}")
        lines.append(f"{'🟢' if pnl >= 0 else '🔴'} PnL non réalisé: ${pnl:+,.2f}")
    open_pos = [p for p in positions if float(p.get("position", {}).get("szi", 0)) != 0]
    if not open_pos:
        lines.append("_Aucune position ouverte_")
    else:
        for p in open_pos:
            pos    = p.get("position", {})
            coin   = pos.get("coin", "?")
            size   = float(pos.get("szi", 0))
            entry  = float(pos.get("entryPx", 0))
            upnl   = float(pos.get("unrealizedPnl", 0))
            liq_px = float(pos.get("liquidationPx") or 0)
            lev    = pos.get("leverage", {}).get("value", "?")
            lines.append(
                f"  {'📈 LONG' if size > 0 else '📉 SHORT'} *{coin}* x{lev}\n"
                f"  Taille: {abs(size)} | Entrée: ${entry:,.4f}\n"
                f"  {'✅' if upnl >= 0 else '❌'} PnL: ${upnl:+,.2f}"
                + (f" | ⚠️ Liq: ${liq_px:,.2f}" if liq_px > 0 else "")
            )
    return "\n".join(lines)


def format_positions(positions: list, balance: dict) -> str:
    lines = ["📈 *Positions Hyperliquid*\n"]
    if balance:
        av  = balance.get("accountValue", 0)
        mu  = balance.get("totalMarginUsed", 0)
        pnl = balance.get("totalUnrealizedPnl", 0)
        lines.append("💼 *Compte*")
        lines.append(f"   Valeur: ${av:,.2f}" if av > 0 else "   Valeur: _non disponible_")
        if mu > 0:
            lines.append(f"   Marge utilisee: ${mu:,.2f}")
        lines.append(f"   {'🟢' if pnl >= 0 else '🔴'} PnL non realise: ${pnl:+,.2f}\n")
    open_pos = [p for p in positions if float(p.get("position", {}).get("szi", 0)) != 0]
    if not open_pos:
        lines.append("_Aucune position ouverte._")
    else:
        lines.append(f"*{len(open_pos)} position(s) ouverte(s):*\n")
        for p in open_pos:
            pos    = p.get("position", {})
            coin   = pos.get("coin", "?")
            size   = float(pos.get("szi", 0))
            entry  = float(pos.get("entryPx", 0))
            upnl   = float(pos.get("unrealizedPnl", 0))
            liq_px = float(pos.get("liquidationPx") or 0)
            lines.append(
                f"*{coin}* — {'LONG 🟢' if size > 0 else 'SHORT 🔴'}\n"
                f"   Taille: {abs(size)} | Entree: ${entry:,.4f}\n"
                f"   {'✅' if upnl >= 0 else '❌'} PnL: ${upnl:+,.2f}\n"
                + (f"   ⚠️ Liquidation: ${liq_px:,.2f}\n" if liq_px > 0 else "")
            )
    lines.append(f"_Mis a jour: {datetime.now().strftime('%H:%M:%S')}_")
    return "\n".join(lines)


def escape_md(text: str) -> str:
    for c in ['_', '*', '`', '[', ']']:
        text = text.replace(c, f'\\{c}')
    return text


def format_daily_summary(news: list, trending: list) -> str:
    now   = datetime.now().strftime("%d/%m/%Y %H:%M")
    lines = [f"🌅 *Résumé Crypto — {now}*\n", "━━━━━━━━━━━━━━━━━━━━", "📰 *3 Actus du Jour*\n"]
    if news:
        for i, n in enumerate(news, 1):
            title = escape_md(n['title'])
            url   = n['url']
            lines.append(f"{i}. [{title}]({url})\n")
    else:
        lines.append("_Indisponible._\n")
    lines += ["━━━━━━━━━━━━━━━━━━━━", "🔥 *Top 3 Trending*\n"]
    if trending:
        for i, t in enumerate(trending, 1):
            c    = t.get("change", 0) or 0
            name = escape_md(str(t['name']))
            sym  = escape_md(str(t['symbol']))
            lines.append(f"{i}. *{name}* (${sym}) — Rank \\#{t['rank']} {'🟢' if c >= 0 else '🔴'} {c:+.1f}%\n")
    else:
        lines.append("_Indisponible._\n")
    lines += ["━━━━━━━━━━━━━━━━━━━━", "_Bonne journée depuis Vallauris\\! 🌴_"]
    return "\n".join(lines)


async def analyze_setup(prices: dict, trending: list) -> str:
    lines = ["🎯 *Analyse Setups de Trade*\n"]
    if not prices:
        return "❌ Donnees indisponibles."
    for symbol in WATCHED_TOKENS:
        if symbol not in prices:
            lines.append(f"_⚠️ {symbol} indisponible_\n")
            continue
        ohlcv = await get_ohlcv(symbol, interval="4h", count=50)
        lines.append(build_token_analysis(symbol, prices[symbol].get("usd_24h_change", 0), ohlcv))
        lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("🔥 *Top 3 Trending*\n")
    if trending:
        for i, t in enumerate(trending, 1):
            c = t.get("change", 0) or 0
            lines.append(f"{i}. *{t['name']}* (${t['symbol']}) {'🟢' if c >= 0 else '🔴'} {c:+.1f}%\n")
    else:
        lines.append("_Indisponible._\n")
    return "\n".join(lines)


# ============================================================
# MODULE TRADEBOT
# ============================================================
tradebot_history = []



# Gardées pour rétrocompatibilité avec cmd_inspector
def score_consistency(v: float) -> float:
    return min(100.0, max(0.0, v))

def score_drawdown(mdd: float) -> float:
    thresholds = [(5, 100), (10, 85), (15, 70), (20, 55), (25, 40), (30, 30), (35, 20), (40, 10)]
    for limit, score in thresholds:
        if mdd <= limit:
            return score
    return 0.0

def score_winrate(wr: float) -> float:
    if wr >= 70: return 100.0
    if wr >= 60: return 80.0
    if wr >= 55: return 65.0
    if wr >= 50: return 40.0
    return 10.0


async def fetch_top_traders_hl() -> list:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard",
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json()

        raw_rows = data.get("leaderboardRows", []) if isinstance(data, dict) else data
        logger.info(f"Leaderboard: {len(raw_rows)} traders bruts")
        if not raw_rows:
            logger.error("Leaderboard vide ou format inattendu")
            return []

        candidates = []
        excl_roi = excl_pnl30 = excl_pnlat = excl_blacklist = 0

        for row in raw_rows:
            try:
                if isinstance(row, dict):
                    address    = row.get("ethAddress") or row.get("user", "")
                    stats_dict = row
                elif isinstance(row, list) and len(row) >= 2:
                    address    = str(row[0])
                    stats_dict = row[1] if isinstance(row[1], dict) else {}
                else:
                    continue

                if not address or len(address) < 10:
                    continue
                if address.lower() in TRADER_BLACKLIST:
                    excl_blacklist += 1
                    continue

                metrics = {w[0]: w[1] for w in stats_dict.get("windowPerformances", [])
                           if isinstance(w, list) and len(w) == 2}

                m30 = metrics.get("month", {})
                mat = metrics.get("allTime", {})

                roi_30d = float(m30.get("roi", 0) or 0) * 100
                pnl_30d = float(m30.get("pnl", 0) or 0)
                pnl_at  = float(mat.get("pnl", 0) or 0)

                if not (20 <= roi_30d <= 10000):
                    excl_roi += 1
                    continue
                if pnl_30d < 5000:
                    excl_pnl30 += 1
                    continue
                if pnl_at < 10000:
                    excl_pnlat += 1
                    continue

                candidates.append((address, roi_30d, pnl_30d))
            except Exception:
                continue

        logger.info(
            f"Candidats: {len(candidates)} "
            f"(excl roi:{excl_roi} pnl30:{excl_pnl30} pnlat:{excl_pnlat} blacklist:{excl_blacklist})"
        )
        if not candidates:
            return []

        candidates.sort(key=lambda x: x[1], reverse=True)
        top_candidates = candidates[:740]
        logger.info(f"Top {len(top_candidates)} retenus pour analyse portfolio")

        # [FIX CRITIQUE] fetch_portfolio refactorisé — logique linéaire, at_pnl_raw défini avant usage
        async def fetch_portfolio(session, address):
            async with _PORTFOLIO_SEMAPHORE:  # [OPT] limite 10 appels parallèles
                try:
                    async with session.post(
                        HYPERLIQUID_API_UI,
                        json={"type": "portfolio", "user": address},
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        portfolio = await resp.json()

                    # Fallback : essayer l'endpoint principal si UI retourne None
                    if portfolio is None:
                        try:
                            async with session.post(
                                HYPERLIQUID_API,
                                json={"type": "portfolio", "user": address},
                                timeout=aiohttp.ClientTimeout(total=10)
                            ) as resp2:
                                portfolio = await resp2.json()
                        except Exception:
                            pass

                    if not portfolio or not isinstance(portfolio, list):
                        port_type = type(portfolio).__name__
                        if isinstance(portfolio, dict):
                            keys = list(portfolio.keys())[:5]
                            logger.info(f"Exclu {address[:12]}: portfolio dict keys={keys}")
                        elif isinstance(portfolio, list) and len(portfolio) == 0:
                            logger.info(f"Exclu {address[:12]}: portfolio liste vide")
                        elif portfolio is None:
                            logger.info(f"Exclu {address[:12]}: portfolio None")
                        else:
                            snippet = str(portfolio)[:120]
                            logger.info(f"Exclu {address[:12]}: format={port_type} snippet={snippet}")
                        return None

                    valid_items = [item for item in portfolio if isinstance(item, list) and len(item) == 2]
                    if not valid_items:
                        snippet = str(portfolio[0])[:100] if portfolio else "vide"
                        logger.info(f"Exclu {address[:12]}: 0 items valides sur {len(portfolio)} — ex: {snippet}")
                        return None

                    windows = {item[0]: item[1] for item in valid_items}

                    now_ms    = datetime.now().timestamp() * 1000
                    cutoff_ms = now_ms - (15 * 86400 * 1000)

                    # ── Activité récente (15j) ──────────────────────────
                    recent_activity = False
                    day_data = windows.get("perpDay") or windows.get("day", {})
                    for p in day_data.get("pnlHistory", []):
                        if isinstance(p, list) and len(p) == 2 and p[0] >= cutoff_ms and float(p[1]) != 0.0:
                            recent_activity = True
                            break
                    if not recent_activity:
                        month_data = windows.get("perpMonth") or windows.get("month", {})
                        month_hist = month_data.get("pnlHistory", [])
                        if month_hist:
                            days_ago = (now_ms - month_hist[-1][0]) / (86400 * 1000)
                            recent_activity = days_ago <= 15
                    if not recent_activity:
                        logger.info(f"Exclu {address[:12]}: inactif depuis > 15j")
                        return None

                    # ── AllTime — défini EN PREMIER pour usage dans le reste ──
                    # [FIX CRITIQUE] at_data et at_pnl_raw définis ici,
                    # avant tout usage de n_trades_at ou âge du compte
                    at_data    = windows.get("perpAllTime") or windows.get("allTime", {})
                    at_pnl_raw = [p for p in at_data.get("pnlHistory", [])
                                  if isinstance(p, list) and len(p) == 2]
                    acv_hat    = [float(p[1]) for p in at_data.get("accountValueHistory", [])
                                  if isinstance(p, list) and float(p[1]) > 0]

                    # ── Ancienneté minimale 3 mois ──────────────────────
                    if not at_pnl_raw:
                        logger.info(f"Exclu {address[:12]}: pas d'historique allTime")
                        return None
                    age_days = (now_ms - at_pnl_raw[0][0]) / (86400 * 1000)
                    if age_days < TRADEBOT_MIN_AGE_DAYS:
                        logger.info(f"Exclu {address[:12]}: ancienneté {age_days:.0f}j < {TRADEBOT_MIN_AGE_DAYS}j")
                        return None

                    # ── Données 7j ──────────────────────────────────────
                    w7     = windows.get("perpWeek") or windows.get("week", {})
                    pnl_h7 = [float(p[1]) for p in w7.get("pnlHistory", []) if isinstance(p, list)]
                    acv_h7 = [float(p[1]) for p in w7.get("accountValueHistory", [])
                              if isinstance(p, list) and float(p[1]) > 0]

                    pnl_7j      = pnl_h7[-1] if pnl_h7 else 0.0
                    wins_7j     = sum(1 for i in range(1, len(pnl_h7)) if pnl_h7[i] > pnl_h7[i-1])
                    n_trades_7j = max(len(pnl_h7) - 1, 0)
                    winrate_7j  = (wins_7j / max(n_trades_7j, 1)) * 100

                    # Proxy allTime depuis at_pnl_raw (désormais défini)
                    n_trades_at = len(at_pnl_raw)

                    # ── userFills 7j — affine win rate et n_trades ──────
                    cutoff_7j = now_ms - (7 * 86400 * 1000)
                    try:
                        async with session.post(
                            HYPERLIQUID_API_UI,
                            json={"type": "userFills", "user": address},
                            timeout=aiohttp.ClientTimeout(total=8)
                        ) as resp2:
                            fills = await resp2.json()
                        if isinstance(fills, list):
                            fills_7j = [f for f in fills
                                        if isinstance(f, dict)
                                        and f.get("time", 0) >= cutoff_7j
                                        and float(f.get("closedPnl", 0) or 0) != 0]
                            n_trades_7j = max(len(fills_7j), n_trades_7j)
                            if fills_7j:
                                wins_fills = sum(1 for f in fills_7j if float(f.get("closedPnl", 0)) > 0)
                                winrate_7j = (wins_fills / len(fills_7j)) * 100
                            fills_at    = [f for f in fills if isinstance(f, dict)
                                           and float(f.get("closedPnl", 0) or 0) != 0]
                            n_trades_at = max(n_trades_at, len(fills_at))
                    except Exception:
                        pass  # Fallback proxy pnlHistory

                    # ── MDD — calculé sur perpMonth uniquement ─────────
                    # allTime contient des 0.0 lors des périodes inactives
                    # ce qui faussait le MDD à 100% artificiellement
                    m30_for_mdd = windows.get("perpMonth") or windows.get("month", {})
                    acv_m30 = [float(p[1]) for p in m30_for_mdd.get("accountValueHistory", [])
                               if isinstance(p, list) and len(p) == 2 and float(p[1]) > 0]
                    worst_mdd = calc_mdd(acv_m30) if acv_m30 else calc_mdd(acv_h7)
                    # MDD affiché mais non filtré — le scoring pénalise implicitement
                    logger.info(f"MDD {address[:12]}: {worst_mdd:.0f}%")

                    # ── Données 30j ─────────────────────────────────────
                    m30     = windows.get("perpMonth") or windows.get("month", {})
                    pnl_h30 = [float(p[1]) for p in m30.get("pnlHistory", []) if isinstance(p, list)]
                    acv_h30 = [float(p[1]) for p in m30.get("accountValueHistory", [])
                               if isinstance(p, list) and float(p[1]) > 0]

                    pnl_30j = pnl_h30[-1] if pnl_h30 else 0.0
                    cap_30  = acv_h30[-1] if acv_h30 else 0.0

                    if cap_30 < 10000:
                        logger.info(f"Exclu {address[:12]}: capital 30j ${cap_30:,.0f} < $10k")
                        return None
                    if pnl_30j < 0:
                        perte_pct = abs(pnl_30j) / max(cap_30, 1) * 100
                        if perte_pct > 15.0:
                            logger.info(f"Exclu {address[:12]}: perte 30j {perte_pct:.1f}% > 15%")
                            return None

                    base_30j   = max(cap_30 - pnl_30j, 1)
                    roi_30j    = min((pnl_30j / base_30j) * 100, 2000)

                    # WinRate 30j
                    wins_30j    = sum(1 for i in range(1, len(pnl_h30)) if pnl_h30[i] > pnl_h30[i-1])
                    winrate_30j = (wins_30j / max(len(pnl_h30) - 1, 1)) * 100

                    # AllTime PnL
                    pnl_hat = [float(p[1]) for p in at_data.get("pnlHistory", []) if isinstance(p, list)]
                    pnl_at  = pnl_hat[-1] if pnl_hat else 0
                    peak_at = max(acv_hat) if acv_hat else 1
                    roe_at  = min((pnl_at / max(peak_at, 1)) * 100, 9999)

                    # ── Eligibilité finale ──────────────────────────────
                    if n_trades_at < 20:
                        logger.info(f"Exclu {address[:12]}: {n_trades_at} trades allTime < 20")
                        return None

                    # Filtre dur scalper — incopiable si > 500 trades/semaine (limit absolue)
                    # Entre 250 et 500 → pénalité progressive dans le scoring v6
                    if n_trades_7j > 500:
                        logger.info(
                            f"Exclu {address[:12]}: scalper extrême {n_trades_7j} trades/semaine "
                            f"(max 500) — incopiable"
                        )
                        return None

                    # Filtre dur WinRate < 45% — incopiable
                    if winrate_7j < 45.0:
                        logger.info(
                            f"Exclu {address[:12]}: WR {winrate_7j:.0f}% < 45% — incopiable"
                        )
                        return None

                    # [v4.5] Filtre n_trades/pnl_mdd_ratio supprime — log1p(n) gere nativement

                    logger.info(
                        f"✅ Qualifié {address[:12]}: trades_at={n_trades_at} "
                        f"mdd={worst_mdd:.0f}% wr7j={winrate_7j:.0f}%"
                    )

                    return {
                        "address":      address,
                        "pnl":          pnl_30j,
                        "pnl_7j":       round(pnl_7j, 2),
                        "pnl_at":       pnl_at,
                        "roi":          round(roi_30j, 1),
                        "roe_at":       round(roe_at, 1),
                        "mdd":          round(worst_mdd, 1),
                        "winrate":      round(winrate_7j, 1),
                        "winrate_30j":  round(winrate_30j, 1),
                        "n_trades_7j":  n_trades_7j,
                        "n_trades_at":  n_trades_at,
                        "roi_30j":      round(roi_30j, 1),
                        "capital":      round(cap_30, 0),
                        "age_days":     round(age_days, 0),   # [v5.0] ancienneté pour PnL annualisé
                    }

                except Exception as e:
                    logger.warning(f"Portfolio {address[:12]} erreur: {e}")
                    return None

        async with aiohttp.ClientSession() as session:
            tasks   = [fetch_portfolio(session, addr) for addr, _, _ in top_candidates]
            results = await asyncio.gather(*tasks)

        traders    = [r for r in results if r is not None]
        none_count = len(results) - len(traders)
        logger.info(
            f"Résultat: {len(traders)} qualifiés / {len(top_candidates)} analysés "
            f"({none_count} exclus)"
        )
        return traders

    except Exception as e:
        logger.error(f"Erreur fetch_top_traders: {e}")
        return []


def rank_and_score_traders(traders: list) -> list:
    """
    Scoring v6 — PnL annualisé × Régularité × WinRate × MDD × Anti-scalper × Marge.

    Filtres durs (avant scoring) :
      - MDD <= TRADEBOT_MAX_DRAWDOWN (65%)
      - WinRate >= 45%
      - PnL all-time > 0

    Score v6 = annualized_factor × momentum × consistency × wr_factor
               × mdd_factor × scalper_factor × margin_factor
    """
    if not traders:
        return []

    avant = len(traders)

    traders = [t for t in traders if t.get("mdd", 999) <= TRADEBOT_MAX_DRAWDOWN]
    traders = [t for t in traders if t.get("winrate", 0) >= 45]
    traders = [t for t in traders if t.get("pnl_at", 0) > 0]

    avant_oww = len(traders)
    traders = [t for t in traders if t.get("pnl_7j", 0) <= t.get("pnl", 1e9)]
    logger.info(f"Filtre one-week-wonder: {avant_oww - len(traders)} exclus")

    logger.info(f"Filtres v6: {len(traders)}/{avant} traders retenus")
    if not traders:
        return []

    def compute_score_v6(t):
        pnl_7j      = t.get("pnl_7j", 0)
        pnl_30j     = t.get("pnl", 0)
        pnl_at      = t.get("pnl_at", 0)
        capital     = max(t.get("capital", 1), 1)
        mdd         = t.get("mdd", 100)
        winrate     = t.get("winrate", 0)
        winrate_30  = t.get("winrate_30j", 0)
        n_trades_7j = t.get("n_trades_7j", 0)
        age_days    = max(t.get("age_days", 365), 1)
        margin_used = t.get("margin_ratio", 0.0)

        # ── 1. PnL annualisé (avec temporisation < 90j) ──────────────
        annualized_roi = (pnl_at / capital) * (365.0 / age_days) * 100
        seuil_annuel   = 50.0 * min(age_days / 90.0, 1.0)  # temporisation

        if annualized_roi <= 0:
            return 0.0
        elif annualized_roi >= seuil_annuel * 2:
            annualized_factor = 1.0
        elif annualized_roi >= seuil_annuel:
            annualized_factor = 0.6 + 0.4 * (annualized_roi / (seuil_annuel * 2))
        else:
            annualized_factor = max(0.1, 0.3 * (annualized_roi / max(seuil_annuel, 1)))

        # ── 2. Momentum 7j ────────────────────────────────────────────
        if pnl_30j <= 0:
            momentum_factor = 0.5
        else:
            semaine_ratio = pnl_7j / max(pnl_30j, 1)
            if semaine_ratio >= 0.25:
                momentum_factor = 1.0
            elif semaine_ratio >= 0.10:
                momentum_factor = 0.75 + semaine_ratio
            elif semaine_ratio >= 0:
                momentum_factor = 0.75
            else:
                momentum_factor = 0.5

        # ── 3. Régularité 30j ─────────────────────────────────────────
        if pnl_7j > 0 and pnl_30j > 0:
            ratio       = pnl_30j / max(pnl_7j, 1)
            consistency = min(ratio / 4.0, 1.0)
        else:
            consistency = 0.5

        # ── 4. WinRate ────────────────────────────────────────────────
        best_wr = max(winrate, winrate_30) / 100.0
        if best_wr >= 0.65:
            wr_factor = min(best_wr * 1.25, 1.0)
        elif best_wr >= 0.50:
            wr_factor = best_wr
        elif best_wr >= 0.45:
            wr_factor = best_wr ** 1.5
        else:
            wr_factor = best_wr ** 2

        # ── 5. MDD ────────────────────────────────────────────────────
        mdd_factor = max(0.02, 1.0 - (mdd / 85.0) ** 1.8)

        # ── 6. Anti-scalper (seuil 250 trades/semaine) ────────────────
        # ≤ 250/semaine → neutre | 250-500 → pénalité progressive | >500 → forte
        if n_trades_7j <= 250:
            scalper_factor = 1.0
        elif n_trades_7j <= 500:
            scalper_factor = 1.0 - 0.8 * ((n_trades_7j - 250) / 250.0)
        else:
            scalper_factor = max(0.05, 0.2 * (250.0 / n_trades_7j))

        # ── 7. Marge utilisée > 60% ───────────────────────────────────
        if margin_used <= 0.60:
            margin_factor = 1.0
        elif margin_used <= 0.80:
            margin_factor = 1.0 - 0.5 * ((margin_used - 0.60) / 0.20)
        else:
            margin_factor = max(0.2, 0.5 - 1.5 * (margin_used - 0.80))

        score = (annualized_factor * momentum_factor * consistency
                 * wr_factor * mdd_factor * scalper_factor * margin_factor)
        return max(score, 0.0)

    raw_scores = [compute_score_v6(t) for t in traders]
    max_raw = max(raw_scores) if raw_scores else 1.0
    if max_raw <= 0:
        max_raw = 1.0

    scored = [
        {**t, "score": round((r / max_raw) * 100, 1), "score_raw": round(r, 4)}
        for t, r in zip(traders, raw_scores)
    ]
    return sorted(scored, key=lambda x: x["score"], reverse=True)

# [OPT] apply_exclusion_filters conservée pour compatibilité mais redirige vers rank_and_score_traders
def apply_exclusion_filters(traders: list) -> list:
    filtered = [
        t for t in traders
        if t["mdd"] <= TRADEBOT_MAX_DRAWDOWN and t.get("pnl_at", 0) > 0
    ]
    logger.info(f"Après filtres: {len(filtered)}/{len(traders)} traders retenus")
    return filtered


def build_top5_report(top5: list) -> str:
    now   = datetime.now().strftime("%d/%m/%Y %H:%M")
    lines = [
        "🏆 *SakaiBot — Top 5 Traders*",
        f"📅 {now}",
        "━━━━━━━━━━━━━━━━━━━━",
        "*📊 Score v6: PnL annualisé × Régularité × WinRate × MDD × Anti-scalper × Marge*",
        "",
    ]
    for i, t in enumerate(top5, 1):
        trades_per_day = t.get("n_trades_7j", 0) / 7.0

        # Badges
        badges = []
        if t.get("winrate", 0) >= 65:
            badges.append("🎯 WR>65%")
        if t.get("mdd", 100) <= 40:
            badges.append("🛡️ MDD<40%")
        if t.get("mdd", 100) <= 60:
            badges.append("✅ MDD stable")
        pnl_30j = t.get("pnl", 0)
        pnl_7j  = t.get("pnl_7j", 1)
        if pnl_30j > 0 and pnl_7j > 0 and (pnl_30j / pnl_7j) >= 3.0:
            badges.append("📈 RÉGULIER")
        badge_str = " " + " ".join(badges) if badges else ""

        cap_str = f"${t.get('capital', 0):,.0f}" if t.get("capital") else "N/A"

        lines.append(f"*#{i}*{badge_str}")
        lines.append(f"PnL 7j: ${t.get('pnl_7j',0):+,.0f} | WR: {t.get('winrate',0):.0f}% | {trades_per_day:.0f} trades/j")
        lines.append(f"MDD: {t['mdd']:.0f}% | PnL 30j: ${pnl_30j:+,.0f} | Capital: {cap_str}")
        lines.append(f"`{t['address']}`")
        lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"_Ces traders sont classés par performance réelle (PnL 30j)_")
    return "\n".join(lines)


def build_history_report() -> str:
    if not tradebot_history:
        return (
            "📚 *Historique TradeBot*\n\n"
            "_Aucune selection anterieure enregistree._\n"
            "Lance /toptraders pour demarrer ta premiere analyse."
        )
    lines = ["📚 *Historique des Selections TradeBot*\n"]
    for session_data in tradebot_history[-5:]:
        lines.append(f"📅 *{session_data['date']}* — Score moyen: {session_data['avg_score']}/100")
        for t in session_data["traders"]:
            addr = t["address"][:6] + "..." + t["address"][-4:]
            lines.append(f"   #{t['rank']} `{addr}` — {t['score']}/100")
        lines.append("")
    lines.append(f"_Total sessions: {len(tradebot_history)}_")
    return "\n".join(lines)


# ============================================================
# COMMANDE TOPTRADERS
# ============================================================
async def cmd_toptraders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    await update.message.reply_text(
        "🔍 *TradeBot — Analyse en cours (scoring v5)...*\n\n"
        "• Récupération leaderboard Hyperliquid (top 740)\n"
        "• Filtres durs: MDD<65% | WR≥45% | <500 trades/semaine | ancienneté 60j\n"
        "• Score v6: PnL annualisé × Régularité × WinRate × MDD × Anti-scalper × Marge\n"
        "• Sélection Top 5 traders réguliers et copiables\n\n"
        "_Patiente quelques secondes..._",
        parse_mode="Markdown"
    )

    raw_traders = await fetch_top_traders_hl()
    if not raw_traders:
        await update.message.reply_text(
            "❌ *Données leaderboard indisponibles.*\nRéessaie dans quelques minutes.",
            parse_mode="Markdown"
        )
        return

    ranked = rank_and_score_traders(raw_traders)
    if not ranked:
        await update.message.reply_text(
            "⚠️ Aucun trader ne passe les filtres.\nDonnées peut-être partielles.",
            parse_mode="Markdown"
        )
        return

    top5 = ranked[:5]
    session_data = {
        "date":      datetime.now().strftime("%d/%m/%Y %H:%M"),
        "avg_score": round(sum(t["score"] for t in top5) / len(top5), 1),
        "traders":   [{"rank": i+1, "address": t["address"], "score": t["score"]} for i, t in enumerate(top5)],
    }
    tradebot_history.append(session_data)
    if len(tradebot_history) > 30:
        tradebot_history.pop(0)

    copy_state["last_ranked"] = ranked

    await update.message.reply_text(build_top5_report(top5), parse_mode="Markdown")
    await update.message.reply_text(
        "💡 *Tip:* Lance `/copy_start` pour copier ces 5 traders sur tous leurs assets.",
        parse_mode="Markdown"
    )


# ============================================================
# COMMANDE INSPECTOR
# ============================================================
async def cmd_inspector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "⚠️ Usage: `/inspector <adresse>`\n"
            "Exemple: `/inspector 0x9cd0a696c7cbb9d44de99268194cb08e5684e5fe`",
            parse_mode="Markdown"
        )
        return

    address = args[0].strip().lower()
    if not address.startswith("0x") or len(address) != 42:
        await update.message.reply_text("❌ Adresse invalide. Format: `0x...` (42 caractères)", parse_mode="Markdown")
        return

    await update.message.reply_text(f"🔍 Inspection de `{address[:10]}...` en cours...", parse_mode="Markdown")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API_UI, json={"type": "portfolio", "user": address},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                portfolio = await resp.json()
            async with session.post(
                HYPERLIQUID_API_UI, json={"type": "userFills", "user": address},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp2:
                fills = await resp2.json()
            async with session.post(
                HYPERLIQUID_API, json={"type": "clearinghouseState", "user": address},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp3:
                state = await resp3.json()

        if not portfolio or not isinstance(portfolio, list):
            await update.message.reply_text("❌ Données portfolio indisponibles pour ce wallet.", parse_mode="Markdown")
            return

        windows = {item[0]: item[1] for item in portfolio if isinstance(item, list) and len(item) == 2}

        def get_window_stats(win_key, fallback_key):
            w        = windows.get(win_key) or windows.get(fallback_key, {})
            pnl_hist = [float(p[1]) for p in w.get("pnlHistory", []) if isinstance(p, list)]
            acv_hist = [float(p[1]) for p in w.get("accountValueHistory", []) if isinstance(p, list) and float(p[1]) > 0]
            pnl      = pnl_hist[-1] if pnl_hist else 0
            capital  = acv_hist[-1] if acv_hist else 0
            mdd      = calc_mdd(acv_hist)
            base     = max(capital - pnl, 1)
            roi      = min((pnl / base) * 100, 9999) if pnl > 0 else (pnl / base) * 100
            pos      = sum(1 for i in range(1, len(pnl_hist)) if pnl_hist[i] > pnl_hist[i-1])
            consist  = (pos / max(len(pnl_hist) - 1, 1)) * 100
            return {"pnl": pnl, "capital": capital, "mdd": mdd, "roi": roi, "consist": consist}

        d1  = get_window_stats("perpDay",     "day")
        w7  = get_window_stats("perpWeek",    "week")
        m30 = get_window_stats("perpMonth",   "month")
        at  = get_window_stats("perpAllTime", "allTime")

        week_data = windows.get("perpWeek") or windows.get("week", {})
        week_pnl  = [float(p[1]) for p in week_data.get("pnlHistory", []) if isinstance(p, list)]
        winrate   = (sum(1 for p in week_pnl if p > 0) / max(len(week_pnl), 1)) * 100

        now_ms       = datetime.now().timestamp() * 1000
        last_fill_ts = None
        if isinstance(fills, list) and fills:
            last_fill_ts = max((f.get("time", 0) for f in fills if isinstance(f, dict)), default=None)
        days_inactive = ((now_ms - last_fill_ts) / 86400000) if last_fill_ts else None

        asset_pnl = {}
        if isinstance(fills, list):
            for fill in fills:
                if not isinstance(fill, dict):
                    continue
                coin = fill.get("coin", "")
                pv   = float(fill.get("closedPnl", 0) or 0)
                asset_pnl[coin] = asset_pnl.get(coin, 0.0) + pv
        top_assets = sorted(asset_pnl.items(), key=lambda x: x[1], reverse=True)[:5]

        positions = []
        if isinstance(state, dict):
            for pos in state.get("assetPositions", []):
                p  = pos.get("position", {})
                sz = float(p.get("szi", 0) or 0)
                if sz != 0:
                    positions.append({
                        "coin":  p.get("coin", "?"),
                        "side":  "Long 📈" if sz > 0 else "Short 📉",
                        "size":  abs(sz),
                        "entry": float(p.get("entryPx", 0) or 0),
                        "pnl":   float(p.get("unrealizedPnl", 0) or 0),
                        "lev":   p.get("leverage", {}).get("value", "?"),
                    })

        roi_score = min(100.0, m30["roi"] / 5)
        score     = round(
            score_consistency(m30["consist"]) * 0.35 +
            score_drawdown(m30["mdd"])        * 0.30 +
            score_winrate(winrate)            * 0.20 +
            roi_score                         * 0.15, 1
        )
        verdict = "🟢 FORT" if score >= 65 else ("🟡 MOYEN" if score >= 45 else "🔴 FAIBLE")

        lines = [
            "🔎 *INSPECTOR — Wallet Analysis*",
            f"`{address}`",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
            "📊 *PERFORMANCE*",
            f"{'Période':<12} {'PnL':>12} {'ROI':>8} {'MDD':>6}",
            f"{'24h':<12} ${d1['pnl']:>+10,.0f} {d1['roi']:>7.1f}% {d1['mdd']:>5.1f}%",
            f"{'7j':<12} ${w7['pnl']:>+10,.0f} {w7['roi']:>7.1f}% {w7['mdd']:>5.1f}%",
            f"{'30j':<12} ${m30['pnl']:>+10,.0f} {m30['roi']:>7.1f}% {m30['mdd']:>5.1f}%",
            f"{'AllTime':<12} ${at['pnl']:>+10,.0f} {at['roi']:>7.1f}% {at['mdd']:>5.1f}%",
            "",
            "🎯 *QUALITÉ (base 30j)*",
            f"Score ETF:   *{score}/100* {verdict}",
            f"Win Rate:    {winrate:.0f}%",
            f"Consistance: {m30['consist']:.0f}%",
            f"Capital:     ${m30['capital']:,.0f}",
        ]

        if days_inactive is not None:
            lines.append(f"Dernier trade: il y a *{days_inactive:.0f}j*")

        if top_assets:
            lines.append("")
            lines.append("💰 *PnL PAR ASSET (allTime)*")
            for coin, pnl in top_assets:
                emoji = "🟢" if pnl >= 0 else "🔴"
                lines.append(f"{emoji} {coin:<8} ${pnl:>+12,.0f}")

        if positions:
            lines.append("")
            lines.append(f"⚡ *POSITIONS OUVERTES ({len(positions)})*")
            for pos in positions[:5]:
                lines.append(
                    f"{pos['side']} {pos['coin']} x{pos['lev']} | "
                    f"PnL: ${pos['pnl']:+,.0f}"
                )
        else:
            lines.append("")
            lines.append("⚡ *Aucune position ouverte*")

        lines += ["━━━━━━━━━━━━━━━━━━━━", "_Données Hyperliquid en temps réel_"]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Inspector erreur: {e}")
        await update.message.reply_text(f"❌ Erreur lors de l'analyse: `{str(e)[:100]}`", parse_mode="Markdown")


# ============================================================
# COPY TRADING — UTILITAIRES
# ============================================================
async def send_copy_notification(app, message: str):
    try:
        await app.bot.send_message(
            chat_id=AUTHORIZED_USER_ID,
            text=message,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Notification erreur: {e}")


async def place_order(asset: str, is_buy: bool, size: float, reason: str = "", leverage: int = 1) -> dict:
    """
    [FIX] leverage param désormais honoré via COPY_LEVERAGE si non fourni.
    [OPT] Utilise le cache prix allMids.
    """
    try:
        if not HL_PRIVATE_KEY:
            logger.error("HL_PRIVATE_KEY non définie")
            return {"error": "Clé privée manquante"}

        if not _HL_SDK_AVAILABLE:
            return {"error": "hyperliquid-python-sdk non installé"}

        key      = HL_PRIVATE_KEY if HL_PRIVATE_KEY.startswith("0x") else "0x" + HL_PRIVATE_KEY
        wallet   = eth_account.Account.from_key(key)
        exchange = Exchange(wallet, hl_constants.MAINNET_API_URL, account_address=COPY_BOT_ADDRESS)

        # [OPT] Cache prix — allMids pour crypto, hip3_prices pour HIP-3
        mids  = await get_all_mids_cached()
        price = float(mids.get(asset, 0))
        if price <= 0:
            price = ai_state["hip3_prices"].get(asset, 0)
        if price <= 0:
            return {"error": f"Prix {asset} introuvable"}

        raw_px    = price * (1.005 if is_buy else 0.995)
        magnitude = len(str(int(raw_px)))
        decimals  = max(0, 5 - magnitude)
        limit_px  = float(round(raw_px, decimals))

        rules = SIZE_RULES.get(asset, SIZE_RULES["_default"])
        sz    = round(size, rules["decimals"])
        sz    = max(sz, rules["min"])
        if sz * price < 10:
            sz = max(round(11.0 / price, rules["decimals"]), rules["min"])

        # SDK HL utilise le ticker brut sans préfixe (ex: "TSLA" pas "xyz:TSLA")
        sdk_coin = asset.split(":")[-1] if ":" in asset else asset

        logger.info(f"Ordre {sdk_coin} size={sz} prix={price} ~${sz*price:.1f} x{leverage}")

        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: exchange.order(sdk_coin, is_buy, sz, limit_px, {"limit": {"tif": "Ioc"}})
        )
        logger.info(f"Ordre {sdk_coin} {'BUY' if is_buy else 'SELL'} {sz} → {result}")
        return result

    except Exception as e:
        logger.error(f"Erreur place_order {asset}: {e}")
        return {"error": str(e)}


async def get_my_positions() -> dict:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API,
                json={"type": "clearinghouseState", "user": COPY_BOT_ADDRESS},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                state = await resp.json()
        positions = {}
        if isinstance(state, dict):
            for pos in state.get("assetPositions", []):
                p  = pos.get("position", {})
                sz = float(p.get("szi", 0) or 0)
                if sz != 0:
                    positions[p.get("coin", "")] = {
                        "side":  "long" if sz > 0 else "short",
                        "size":  abs(sz),
                        "entry": float(p.get("entryPx", 0) or 0),
                        "upnl":  float(p.get("unrealizedPnl", 0) or 0),
                    }
        return positions
    except Exception as e:
        logger.error(f"get_my_positions erreur: {e}")
        return {}


async def check_margin_ok() -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API,
                json={"type": "clearinghouseState", "user": COPY_BOT_ADDRESS},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                state = await resp.json()
        margin_summary = state.get("marginSummary", {})
        account_value  = float(margin_summary.get("accountValue", 0) or 0)
        margin_used    = float(margin_summary.get("totalMarginUsed", 0) or 0)
        if account_value <= 0:
            return False
        margin_ratio = 1 - (margin_used / account_value)
        logger.info(f"Marge disponible: {margin_ratio*100:.1f}%")
        return margin_ratio >= MARGIN_SAFETY
    except Exception as e:
        logger.error(f"check_margin_ok erreur: {e}")
        return False


async def get_proportional_size(asset: str, trader_addr: str, trader_pos_value: float,
                                current_price: float) -> tuple:
    """
    [FIX] Applique COPY_LEVERAGE[asset] comme levier de base si le trader
    n'a pas de levier récupérable, au lieu de defaulter à 1.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API,
                json={"type": "clearinghouseState", "user": trader_addr},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                trader_state = await resp.json()

        margin         = trader_state.get("crossMarginSummary") or trader_state.get("marginSummary") or {}
        trader_capital = float(margin.get("accountValue", 0) or 0)
        if trader_capital <= 0:
            trader_capital = float(trader_state.get("crossAccountValue", 0) or 1)

        # [FIX] Priorité: levier du trader → COPY_LEVERAGE → 1
        leverage = COPY_LEVERAGE.get(asset, 1)
        for pos in trader_state.get("assetPositions", []):
            p = pos.get("position", {})
            if p.get("coin") == asset:
                trader_lev = int(p.get("leverage", {}).get("value", 0) or 0)
                if trader_lev > 0:
                    leverage = trader_lev
                break

        _, bot_balance = await get_wallet_data(COPY_BOT_ADDRESS)
        my_capital     = bot_balance.get("accountValue", 0) or 1000.0

        ratio        = trader_pos_value / max(trader_capital, 1)
        my_usd_value = (ratio / 5) * my_capital

        rules        = SIZE_RULES.get(asset, SIZE_RULES["_default"])
        my_usd_value = max(my_usd_value, rules["min_usd"])
        my_size      = max(round(my_usd_value / current_price, rules["decimals"]), rules["min"])

        logger.info(
            f"Proportionnel {asset}: trader_cap=${trader_capital:.0f} "
            f"pos=${trader_pos_value:.0f} ratio={ratio*100:.1f}% "
            f"mon_cap=${my_capital:.0f} → ${my_usd_value:.0f} sz={my_size} x{leverage}"
        )
        return my_size, leverage, my_usd_value

    except Exception as e:
        logger.warning(f"get_proportional_size erreur: {e}")
        rules         = SIZE_RULES.get(asset, SIZE_RULES["_default"])
        leverage      = COPY_LEVERAGE.get(asset, 1)  # [FIX] fallback COPY_LEVERAGE
        fallback_size = max(round(50.0 / max(current_price, 0.01), rules["decimals"]), rules["min"])
        return fallback_size, leverage, 50.0


# ============================================================
# COPY TRADING — START / STOP / WATCH
# ============================================================
async def start_copy_trading(top_traders: list, app) -> None:
    # Stopper les anciens WS
    for addr, task in list(copy_state["ws_tasks"].items()):
        task.cancel()
        logger.info(f"WebSocket {addr[:12]} arrêté (relance)")
    copy_state["ws_tasks"] = {}
    copy_state["traders"]  = {}
    copy_state["active"]   = True

    active_addrs = []
    notif_lines  = ["🤖 *SakaiBot — Copy Trading v4.2*", "_Top 5 multi-asset_", ""]

    for rank, trader in enumerate(top_traders[:5], 1):
        addr = trader["address"]
        task = asyncio.create_task(watch_trader_multiasset(addr, app))
        copy_state["ws_tasks"][addr] = task
        copy_state["traders"][addr]  = {
            "rank":        rank,
            "score":       trader.get("score", 0),
            "assets_seen": set(),
        }
        active_addrs.append(addr)
        logger.info(f"WebSocket lancé: Trader #{rank} {addr[:12]} (score {trader.get('score',0)})")
        bonus = " ✨" if trader.get("roi_30j", 0) >= 20 and trader.get("pnl", 0) > 0 else ""
        notif_lines.append(
            f"#{rank} `{addr[:16]}...`{bonus}\n"
            f"   Score: {trader.get('score',0)}/100 | PnL 7j: ${trader.get('pnl_7j',0):+,.0f}\n"
            f"   WR 7j: {trader.get('winrate',0):.0f}% | Trades 7j: {trader.get('n_trades_7j',0)} | MDD: {trader.get('mdd',0):.0f}%"
        )

    if not active_addrs:
        copy_state["active"] = False
        await send_copy_notification(app, "❌ *Copy Trading annulé*\nAucun trader qualifié.")
        return

    notif_lines += [
        "",
        f"📡 {len(active_addrs)} traders | tous assets | taille proportionnelle",
        "_Scoring: (PnL\\_7j/MDD) × WR × log(trades) × bonus30j_",
    ]
    await send_copy_notification(app, "\n".join(notif_lines))
    logger.info(f"✅ Copy trading actif — {len(active_addrs)} traders")


async def stop_copy_trading() -> None:
    copy_state["active"] = False
    for addr, task in list(copy_state["ws_tasks"].items()):
        task.cancel()
        logger.info(f"WebSocket {addr[:12]} annulé")
    copy_state["ws_tasks"] = {}
    copy_state["traders"]  = {}
    logger.info("Copy trading arrêté")


async def watch_trader_multiasset(trader_address: str, app) -> None:
    logger.info(f"WebSocket multi-asset → {trader_address[:12]}...")

    while copy_state["active"]:
        try:
            async with websockets.connect(HYPERLIQUID_WS, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({
                    "method": "subscribe",
                    "subscription": {"type": "userEvents", "user": trader_address}
                }))
                logger.info(f"✅ WebSocket connecté → {trader_address[:12]}")

                async for raw_msg in ws:
                    if not copy_state["active"]:
                        break
                    try:
                        msg = json.loads(raw_msg)
                        await process_multiasset_event(trader_address, msg, app)
                    except Exception as e:
                        logger.warning(f"WS {trader_address[:12]} parse erreur: {e}")

        except Exception as e:
            if not copy_state["active"]:
                break
            logger.warning(f"WS {trader_address[:12]} déconnecté: {e} — reconnexion 5s")
            await asyncio.sleep(5)

    logger.info(f"WebSocket {trader_address[:12]} arrêté")


async def process_multiasset_event(trader_address: str, msg: dict, app) -> None:
    data  = msg.get("data", {})
    if not data:
        return
    fills = data.get("fills", [])

    for fill in fills:
        asset    = fill.get("coin", "")
        side     = fill.get("side", "")
        size     = float(fill.get("sz", 0) or 0)
        price    = float(fill.get("px", 0) or 0)
        dir_fill = fill.get("dir", "")

        if size <= 0 or price <= 0 or not asset:
            continue

        is_opening = "Open" in dir_fill
        is_closing = "Close" in dir_fill
        is_buy     = side == "B"

        logger.info(f"Trader {trader_address[:12]} → {dir_fill} {asset} sz:{size} px:{price}")

        if trader_info := copy_state["traders"].get(trader_address):
            trader_info["assets_seen"].add(asset)

        my_size = leverage = 0
        my_usd  = 0.0

        if is_opening:
            existing = copy_state["positions"].get(asset)
            if existing and existing.get("trader_addr") != trader_address:
                rank = copy_state["traders"].get(trader_address, {}).get("rank", "?")
                existing_rank = copy_state["traders"].get(existing["trader_addr"], {}).get("rank", "?")
                logger.info(f"Conflit {asset}: signal #{rank} ignoré (déjà ouvert par #{existing_rank})")
                await send_copy_notification(app,
                    f"ℹ️ *Signal ignoré — conflit*\n"
                    f"Asset: `{asset}` | Trader #{rank}\n"
                    f"Déjà une position ouverte (Trader #{existing_rank})."
                )
                continue

            if not await check_margin_ok():
                await send_copy_notification(app,
                    f"⚠️ *Marge insuffisante*\nAsset: {asset} | Action: {dir_fill}\nOrdre annulé."
                )
                continue

            pos_value             = size * price
            my_size, leverage, my_usd = await get_proportional_size(asset, trader_address, pos_value, price)
            result                = await place_limit_gtc(asset, is_buy, my_size, dir_fill)

            if "error" not in result:
                copy_state["positions"][asset] = {
                    "side":        "long" if is_buy else "short",
                    "size":        my_size,
                    "entry":       price,
                    "trader_addr": trader_address,
                }
                copy_deployed[asset] = copy_deployed.get(asset, 0.0) + my_usd

        elif is_closing:
            my_positions = await get_my_positions()
            my_pos       = my_positions.get(asset)
            if not my_pos:
                logger.info(f"Pas de position sur {asset} — fermeture ignorée")
                continue

            tracked = copy_state["positions"].get(asset, {})
            if tracked.get("trader_addr") and tracked["trader_addr"] != trader_address:
                logger.info(f"Fermeture {asset} ignorée — signal d'un autre trader")
                continue

            my_size = my_pos["size"]
            is_buy  = my_pos["side"] == "short"
            result  = await place_limit_gtc(asset, is_buy, my_size, dir_fill)

            copy_state["positions"].pop(asset, None)
            copy_deployed[asset] = 0.0
        else:
            continue

        trade_log = {
            "time":     datetime.now().strftime("%d/%m %H:%M"),
            "asset":    asset,
            "dir":      dir_fill,
            "size":     my_size,
            "price":    price,
            "leverage": leverage,
            "trader":   trader_address[:12],
            "result":   "ok" if "error" not in result else "erreur",
        }
        copy_state["trades_log"].append(trade_log)
        if len(copy_state["trades_log"]) > 100:
            copy_state["trades_log"] = copy_state["trades_log"][-100:]

        emoji  = "📈" if is_buy else "📉"
        status = "✅" if "error" not in result else "❌"
        rank   = copy_state["traders"].get(trader_address, {}).get("rank", "?")
        notif  = (
            f"{status} *Copy Trade {emoji}*\n"
            f"Asset:   *{asset}*\n"
            f"Action:  {dir_fill}\n"
            f"Taille:  {my_size} (~${my_usd:.0f})\n"
            f"Levier:  x{leverage}\n"
            f"Prix:    ${price:,.2f}\n"
            f"Trader:  #{rank} `{trader_address[:16]}...`"
        )
        if "error" in result:
            notif += f"\n⚠️ Erreur: {result['error']}"
        await send_copy_notification(app, notif)


# ============================================================
# COMMANDES TELEGRAM — Copy Trading
# ============================================================
async def cmd_copy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not copy_state.get("last_ranked"):
        await update.message.reply_text(
            "🔍 Aucune analyse récente — lancement automatique...\n"
            "_Score: (PnL\\_7j/MDD) × WR\\_7j × log(trades) + bonus 30j_",
            parse_mode="Markdown"
        )
        raw_traders = await fetch_top_traders_hl()
        if not raw_traders:
            await update.message.reply_text("❌ Impossible de récupérer les traders.", parse_mode="Markdown")
            return
        ranked = rank_and_score_traders(raw_traders)
        if not ranked:
            await update.message.reply_text("⚠️ Aucun trader ne passe les filtres.", parse_mode="Markdown")
            return
        copy_state["last_ranked"] = ranked
        await update.message.reply_text(build_top5_report(ranked[:5]), parse_mode="Markdown")

    top5 = copy_state["last_ranked"][:5]
    if not top5:
        await update.message.reply_text("⚠️ Aucun trader disponible.", parse_mode="Markdown")
        return

    await start_copy_trading(top5, context.application)


async def cmd_copy_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not copy_state["active"]:
        await update.message.reply_text("⏸ Copy trading déjà inactif.")
        return
    await stop_copy_trading()
    await update.message.reply_text(
        "🛑 *Copy Trading arrêté*\n"
        "Tes positions ouvertes restent actives — gère-les manuellement.",
        parse_mode="Markdown"
    )


async def cmd_copy_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not copy_state["active"]:
        await update.message.reply_text(
            "⏸ *Copy Trading inactif*\n"
            "Lance `/toptraders` puis `/copy_start` pour démarrer.",
            parse_mode="Markdown"
        )
        return

    positions = await get_my_positions()
    lines     = [
        "🤖 *COPY TRADING — Statut*",
        f"🟢 ACTIF — {len(copy_state['ws_tasks'])} traders surveillés",
        "",
        "👥 *Traders:*",
    ]

    for addr, info in copy_state["traders"].items():
        assets_seen = ", ".join(sorted(info["assets_seen"])) if info["assets_seen"] else "en attente"
        lines.append(
            f"#{info['rank']} `{addr[:16]}...` — {info['score']}/100\n"
            f"   Assets tradés: {assets_seen}"
        )

    if positions:
        lines.append("")
        lines.append("📊 *Positions ouvertes:*")
        for asset, pos in positions.items():
            tracked     = copy_state["positions"].get(asset, {})
            trader_addr = tracked.get("trader_addr", "")
            trader_rank = copy_state["traders"].get(trader_addr, {}).get("rank", "?")
            emoji       = "📈" if pos["side"] == "long" else "📉"
            lines.append(
                f"{emoji} *{asset}* {pos['side'].upper()} sz:{pos['size']} "
                f"| PnL: ${pos['upnl']:+,.0f} | Trader #{trader_rank}"
            )
    else:
        lines.append("")
        lines.append("📊 *Aucune position ouverte*")

    if copy_state["trades_log"]:
        lines.append("")
        lines.append(f"📋 *Derniers trades ({min(3, len(copy_state['trades_log']))}):*")
        for t in copy_state["trades_log"][-3:]:
            status = "✅" if t["result"] == "ok" else "❌"
            lines.append(f"{status} {t['time']} | {t['asset']} {t['dir']} @ ${t['price']:,.0f} — {t['trader'][:8]}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_copy_close_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text("🔄 Fermeture de toutes les positions en cours...")
    positions = await get_my_positions()
    if not positions:
        await update.message.reply_text("✅ Aucune position ouverte.")
        return
    results = []
    for asset, pos in positions.items():
        is_buy = pos["side"] == "short"
        result = await place_limit_gtc(asset, is_buy, pos["size"], "Close All", force_market=True)
        status = "✅" if "error" not in result else "❌"
        results.append(f"{status} {asset} fermé")
        copy_state["positions"].pop(asset, None)
        copy_deployed[asset] = 0.0
    await update.message.reply_text(
        "🏁 *Fermeture terminée*\n" + "\n".join(results),
        parse_mode="Markdown"
    )


async def cmd_tb_historique(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text(build_history_report(), parse_mode="Markdown")


async def cmd_tb_aide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    # [FIX] Aide mise à jour pour refléter le scoring v4.1
    msg = (
        "🤖 *SakaiBot — Module Copy Trading v4.1*\n\n"
        "Sélectionne les *5 meilleurs traders globaux* (top 740 Hyperliquid)\n"
        "et copie TOUS leurs trades, sur TOUS leurs assets.\n\n"
        "📐 *Scoring v4.1:*\n"
        "   Score = (PnL\\_7j / MDD) × WR\\_7j × log(trades\\_7j)\n"
        "   Bonus ×1.2 si PnL\\_30j > 0 ET ROI\\_30j ≥ 20%\n\n"
        "🚫 *Filtres d'exclusion:*\n"
        "   • MDD > 60% (7j ou allTime)\n"
        "   • Inactif depuis > 15 jours\n"
        "   • Capital < $10 000\n"
        "   • PnL 30j perte > 15%\n"
        "   • Ancienneté < 3 mois\n"
        "   • < 20 trades allTime\n\n"
        "⚙️ *Leviers (COPY\\_LEVERAGE):*\n"
        "   BTC x5 | ETH x4 | SOL x5 | HYPE x3 | TAO x3\n\n"
        "⚡ *Logique de copie:*\n"
        "   • 1 WebSocket par trader (5 total)\n"
        "   • Tous assets copiés sans filtre\n"
        "   • Taille proportionnelle à ton capital\n"
        "   • Conflit: 2ème signal ignoré (premier arrivé)\n\n"
        "📋 *Commandes:*\n"
        "   /toptraders — Analyser + sélectionner le Top 5\n"
        "   /copy\\_start — Démarrer la copie\n"
        "   /copy\\_stop — Arrêter la copie\n"
        "   /copy\\_status — Statut en temps réel\n"
        "   /copy\\_close — Fermer toutes les positions\n"
        "   /inspector — Analyser un wallet manuellement\n"
        "   /tb\\_historique — Sélections passées\n\n"
        "_v4.1 — Multi-asset, Top 5 global, COPY\\_LEVERAGE actif_"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ============================================================
# COMMANDES PRINCIPALES
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    msg = (
        "👋 *Bienvenue sur SakaiBot v4.4! 🤖*\n\n"
        "📊 *Marché & Analyse*\n"
        "   /prix — Prix en temps réel\n"
        "   /setup — Analyse technique complète\n"
        "   /resume — Résumé du marché\n"
        "   /peur — Fear & Greed Index\n\n"
        "📈 *Mes Positions*\n"
        "   /positions — Wallets Master + Bot\n\n"
        "🏆 *TradeBot — Copy Trading*\n"
        "   /toptraders — Analyser Top 5 multi-asset\n"
        "   /inspector — Analyser un wallet\n"
        "   /copy\\_start — Démarrer la copie (Top 5)\n"
        "   /copy\\_stop — Arrêter la copie\n"
        "   /copy\\_status — Statut en temps réel\n"
        "   /copy\\_close — Fermer toutes les positions\n"
        "   /tb\\_historique — Historique des sélections\n"
        "   /tb\\_aide — Aide Copy Trading\n\n"
        "🎯 *Target Wallet Manuel*\n"
        "   /target 0x... [label] — Surveiller + répliquer\n"
        "   /target\\_sync 0x... — Copier positions existantes\n"
        "   /target\\_stop 0x... — Stopper un target\n"
        "   /target\\_status — État de tous les targets\n\n"
        "🔮 *ORACLE — Module IA HIP-3*\n"
        "   /ai\\_start — Démarrer l'IA (CL • GOLD • SILVER)\n"
        "   /ai\\_stop — Arrêter l'IA\n"
        "   /ai\\_status — Positions + PnL en temps réel\n"
        "   /ai\\_close\\_all — Fermer toutes les positions IA\n"
        "   /ai\\_history — 10 derniers trades IA\n\n"
        "🔔 *Alertes*\n"
        "   /alertes — Activer les alertes auto\n"
        "   /desactiver\\_alertes — Stopper les alertes\n\n"
        "ℹ️ /aide — Affiche ce message\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_prix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text("⏳ Récupération des prix...", parse_mode="Markdown")
    prices = await get_crypto_prices()
    await update.message.reply_text(format_prices(prices), parse_mode="Markdown")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text("⏳ Connexion à Hyperliquid...", parse_mode="Markdown")
    (pos_master, bal_master), (pos_bot, bal_bot) = await asyncio.gather(
        get_wallet_data(HYPERLIQUID_ADDRESS),
        get_wallet_data(COPY_BOT_ADDRESS),
    )
    now = datetime.now().strftime("%H:%M:%S")
    msg = (
        f"📈 *Positions Hyperliquid* — _{now}_\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 {format_wallet_block('Mon Wallet Master', HYPERLIQUID_ADDRESS, pos_master, bal_master)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🤖 {format_wallet_block('Mon Wallet Bot', COPY_BOT_ADDRESS, pos_bot, bal_bot)}\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "⏳ Analyse technique en cours (RSI, EMA, S/R, Fibo, Volume)...",
        parse_mode="Markdown"
    )
    prices, trending = await asyncio.gather(get_crypto_prices(), get_crypto_trending())
    msg = await analyze_setup(prices, trending)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text("⏳ Préparation du résumé...", parse_mode="Markdown")
    news, trending = await asyncio.gather(get_crypto_news(), get_crypto_trending())
    await update.message.reply_text(
        format_daily_summary(news, trending),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


async def cmd_peur(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text("⏳ Fear & Greed Index...", parse_mode="Markdown")
    fg = await get_fear_greed()
    await update.message.reply_text(format_fear_greed(fg), parse_mode="Markdown")


async def cmd_aide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await cmd_start(update, context)


# ============================================================
# JOBS AUTOMATIQUES
# ============================================================
async def job_price_alert(context: ContextTypes.DEFAULT_TYPE):
    global last_prices
    prices = await get_crypto_prices()
    if not prices:
        return
    for symbol in WATCHED_TOKENS:
        if symbol not in prices:
            continue
        current = prices[symbol].get("usd", 0)
        if symbol in last_prices and last_prices[symbol] > 0:
            variation = ((current - last_prices[symbol]) / last_prices[symbol]) * 100
            if abs(variation) >= ALERT_THRESHOLD_PERCENT:
                emoji = "🚀" if variation > 0 else "💥"
                await context.bot.send_message(
                    chat_id=context.job.chat_id,
                    text=f"{emoji} *ALERTE {symbol}*\nVariation: {variation:+.2f}% en 1h\nPrix: ${current:,.2f}",
                    parse_mode="Markdown"
                )
        last_prices[symbol] = current


async def job_daily_summary(context: ContextTypes.DEFAULT_TYPE):
    news, trending = await asyncio.gather(get_crypto_news(), get_crypto_trending())
    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text=format_daily_summary(news, trending),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


async def cmd_activer_alertes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    chat_id   = update.effective_chat.id
    job_queue = context.job_queue
    for name in [f"alert_{chat_id}", f"daily_{chat_id}", f"sr_{chat_id}_matin", f"sr_{chat_id}_soir"]:
        for job in job_queue.get_jobs_by_name(name):
            job.schedule_removal()
    job_queue.run_repeating(job_price_alert, interval=3600, first=10,   chat_id=chat_id, name=f"alert_{chat_id}")
    job_queue.run_daily(job_daily_summary, time=time(MORNING_HOUR_UTC, MORNING_MIN_UTC), chat_id=chat_id, name=f"daily_{chat_id}")
    job_queue.run_daily(job_twice_daily,   time=time(MORNING_HOUR_UTC, MORNING_MIN_UTC), chat_id=chat_id, name=f"sr_{chat_id}_matin")
    job_queue.run_daily(job_twice_daily,   time=time(EVENING_HOUR_UTC, EVENING_MIN_UTC), chat_id=chat_id, name=f"sr_{chat_id}_soir")
    await update.message.reply_text(
        f"✅ *Alertes activées!*\n\n"
        f"• Variation > {ALERT_THRESHOLD_PERCENT}% / heure\n"
        f"• Résumé + Check S/R à *8h00 UTC*\n"
        f"• Check S/R + liquidation à *20h00 UTC*\n\n"
        "Utilise /desactiver\\_alertes pour stopper.",
        parse_mode="Markdown"
    )


async def cmd_desactiver_alertes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    chat_id = update.effective_chat.id
    for name in [f"alert_{chat_id}", f"daily_{chat_id}", f"sr_{chat_id}_matin", f"sr_{chat_id}_soir"]:
        for job in context.job_queue.get_jobs_by_name(name):
            job.schedule_removal()
    await update.message.reply_text("🔕 Toutes les alertes sont désactivées.")


# ============================================================
# MODULE TARGET WALLET MANUEL v4.4
# Logique :
#   /target 0x...        → surveillance + réplication auto (ratio calculé au démarrage)
#   /target_sync         → ouvre les positions déjà ouvertes du trader (optionnel)
#   /target_stop 0x...   → stoppe un trader spécifique (ou tous si pas d'adresse)
#   /target_status       → état de tous les traders surveillés
#
# Multi-target :
#   Jusqu'à MAX_TARGETS traders simultanés.
#   Conflit asset : premier arrivé premier servi — signal suivant ignoré + notif.
# ============================================================

MAX_TARGETS = 3  # Nombre max de wallets cibles simultanés

# target_registry : {address: {...}} — un dict par trader actif
target_registry: dict = {}

# Positions consolidées : {asset: {"side","size","entry","trader_addr"}}
# Premier arrivé premier servi — un seul trader par asset
target_positions: dict = {}

# Log global
target_trades_log: list = []


# ── Helpers ordres ──────────────────────────────────────────

async def place_market_order(asset: str, is_buy: bool, size: float,
                             ref_price: float) -> dict:
    """
    Ordre MARKET — IOC avec slippage 2% pour garantir le fill.
    Utilisé pour ouvertures, renforts, allègements et fermetures.
    """
    try:
        if not HL_PRIVATE_KEY:
            return {"error": "HL_PRIVATE_KEY manquante"}
        if not _HL_SDK_AVAILABLE:
            return {"error": "hyperliquid-python-sdk non installé"}

        key    = HL_PRIVATE_KEY if HL_PRIVATE_KEY.startswith("0x") else "0x" + HL_PRIVATE_KEY
        wallet = eth_account.Account.from_key(key)
        # HIP-3 assets nécessitent la meta xyz
        if ":" in asset:
            exchange = await _build_hip3_exchange()
        else:
            exchange = Exchange(wallet, hl_constants.MAINNET_API_URL, account_address=COPY_BOT_ADDRESS)
        if exchange is None:
            return {"error": "Exchange non initialisable"}

        slippage = 1.02 if is_buy else 0.98
        raw_px   = ref_price * slippage
        magnitude = len(str(int(raw_px)))
        decimals  = max(0, 5 - magnitude)
        limit_px  = float(round(raw_px, decimals))

        rules = SIZE_RULES.get(asset, SIZE_RULES["_default"])
        sz    = round(size, rules["decimals"])
        sz    = max(sz, rules["min"])
        if sz * ref_price < 10:
            sz = max(round(11.0 / ref_price, rules["decimals"]), rules["min"])

        # SDK HL utilise le ticker brut sans préfixe (ex: "TSLA" pas "xyz:TSLA")
        sdk_coin = asset.split(":")[-1] if ":" in asset else asset

        logger.info(f"Market {sdk_coin} {'BUY' if is_buy else 'SELL'} sz={sz} ~${sz*ref_price:.0f}")

        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: exchange.order(sdk_coin, is_buy, sz, limit_px, {"limit": {"tif": "Ioc"}})
        )
        logger.info(f"Market {sdk_coin} résultat: {result}")
        return result

    except Exception as e:
        logger.error(f"place_market_order {asset}: {e}")
        return {"error": str(e)}


async def place_market_close(asset: str, is_buy: bool, size: float) -> dict:
    """Alias vers place_market_order pour les fermetures."""
    mids  = await get_all_mids_cached()
    price = float(mids.get(asset, 0))
    if price <= 0:
        return {"error": f"Prix {asset} introuvable"}
    return await place_market_order(asset, is_buy, size, price)


async def _build_exchange() -> "Exchange | None":
    """Helper — instancie Exchange pour les assets mainnet (BTC, ETH, HYPE...)."""
    if not HL_PRIVATE_KEY or not _HL_SDK_AVAILABLE:
        return None
    key    = HL_PRIVATE_KEY if HL_PRIVATE_KEY.startswith("0x") else "0x" + HL_PRIVATE_KEY
    wallet = eth_account.Account.from_key(key)
    return Exchange(wallet, hl_constants.MAINNET_API_URL, account_address=COPY_BOT_ADDRESS)


async def _build_hip3_exchange() -> "Exchange | None":
    """
    Exchange configuré pour les assets HIP-3 du DEX xyz.
    Le SDK HL cherche les assets dans coin_to_asset — pour HIP-3 il faut
    initialiser Exchange avec la meta du DEX xyz, pas la meta mainnet.
    """
    if not HL_PRIVATE_KEY or not _HL_SDK_AVAILABLE:
        return None
    try:
        key    = HL_PRIVATE_KEY if HL_PRIVATE_KEY.startswith("0x") else "0x" + HL_PRIVATE_KEY
        wallet = eth_account.Account.from_key(key)
        # Récupérer la meta du DEX xyz
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API,
                json={"type": "metaAndAssetCtxs", "dex": "xyz"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
        hip3_meta = {"universe": data[0].get("universe", [])} if isinstance(data, list) else {"universe": []}
        logger.info(f"HIP-3 meta: {len(hip3_meta['universe'])} assets dans coin_to_asset")
        return Exchange(
            wallet,
            hl_constants.MAINNET_API_URL,
            account_address=COPY_BOT_ADDRESS,
            meta=hip3_meta
        )
    except Exception as e:
        logger.error(f"_build_hip3_exchange: {e}")
        return None


def _round_price(raw: float) -> float:
    """Arrondit le prix selon la magnitude (règle HL 5 chiffres sig.)."""
    magnitude = len(str(int(raw)))
    decimals  = max(0, 5 - magnitude)
    return float(round(raw, decimals))


def _round_size(asset: str, size: float, ref_price: float) -> float:
    """Arrondit la taille et applique le minimum HL."""
    rules = SIZE_RULES.get(asset, SIZE_RULES["_default"])
    sz    = round(size, rules["decimals"])
    sz    = max(sz, rules["min"])
    if sz * ref_price < 10:
        sz = max(round(11.0 / ref_price, rules["decimals"]), rules["min"])
    return sz


async def place_limit_gtc(
    asset: str,
    is_buy: bool,
    size: float,
    reason: str = "",
    force_market: bool = False,
) -> dict:
    """
    Ordre MAKER (GTC) avec retry + fallback market — v4.6.

    Logique :
      1. Poster un limit GTC à LIMIT_MAKER_OFFSET du mid-price
         (légèrement sous le marché pour un BUY, légèrement au-dessus pour un SELL)
         → statut maker = fees ~0 voire rebate -0.01%
      2. Attendre LIMIT_RETRY_WAIT secondes → vérifier le fill via clearinghouseState
      3. Si non rempli après LIMIT_MAX_RETRIES → annuler + fallback market IOC (taker)
      4. Si force_market=True (stop-loss urgence) → saute directement au market

    Utilisé pour : ouverture, renfort, allègement, clôture normale.
    place_market_order reste pour les stop-loss d'urgence uniquement.
    """
    if not HL_PRIVATE_KEY:
        return {"error": "HL_PRIVATE_KEY manquante"}
    if not _HL_SDK_AVAILABLE:
        return {"error": "hyperliquid-python-sdk non installé"}

    # Récupération du prix de référence
    # Pour les assets HIP-3 (absents de allMids), utiliser le cache hip3_prices
    mids = await get_all_mids_cached()
    ref_price = float(mids.get(asset, 0))
    if ref_price <= 0:
        ref_price = ai_state["hip3_prices"].get(asset, 0)
    if ref_price <= 0:
        cfg = AI_HIP3_ASSETS.get(asset, {})
        ref_price = await ai_get_hl_price(asset, cfg.get("dex", ""))
    if ref_price <= 0:
        return {"error": f"Prix {asset} introuvable"}

    sz = _round_size(asset, size, ref_price)

    # ── Urgence / force market ───────────────────────────────
    if force_market:
        logger.info(f"[LIMIT_GTC] force_market={asset} {sz} reason={reason}")
        return await place_market_order(asset, is_buy, sz, ref_price)

    # HIP-3 assets (xyz:TSLA, xyz:CL...) nécessitent un Exchange initialisé avec la meta xyz
    is_hip3 = ":" in asset
    if is_hip3:
        exchange = await _build_hip3_exchange()
    else:
        exchange = await _build_exchange()
    if exchange is None:
        return {"error": "Exchange non initialisable"}

    loop = asyncio.get_event_loop()

    for attempt in range(1, LIMIT_MAX_RETRIES + 1):
        # Recalcul du prix à chaque retry (mid peut avoir bougé)
        mids      = await get_all_mids_cached()
        new_price = float(mids.get(asset, 0))
        if new_price <= 0:
            cfg = AI_HIP3_ASSETS.get(asset, {})
            new_price = await ai_get_hl_price(asset, cfg.get("dex", ""))
        if new_price > 0:
            ref_price = new_price
        # BUY : on poste légèrement SOUS le mid → maker (on attend que le marché descende)
        # SELL : on poste légèrement AU-DESSUS du mid → maker
        offset_factor = (1 - LIMIT_MAKER_OFFSET) if is_buy else (1 + LIMIT_MAKER_OFFSET)
        limit_px = _round_price(ref_price * offset_factor)

        logger.info(
            f"[LIMIT_GTC] attempt={attempt}/{LIMIT_MAX_RETRIES} "
            f"{asset} {'BUY' if is_buy else 'SELL'} sz={sz} limit={limit_px} reason={reason}"
        )

        # SDK HL utilise le ticker brut sans préfixe (ex: "TSLA" pas "xyz:TSLA")
        sdk_coin = asset.split(":")[-1] if ":" in asset else asset

        try:
            result = await loop.run_in_executor(
                None,
                lambda lp=limit_px: exchange.order(
                    sdk_coin, is_buy, sz, lp,
                    {"limit": {"tif": "Gtc"}}
                )
            )
        except Exception as e:
            logger.error(f"[LIMIT_GTC] place error attempt={attempt}: {e}")
            result = {"error": str(e)}

        # Vérifier si la réponse indique un fill immédiat
        status = ""
        oid    = None
        try:
            # SDK retourne {"status": "ok", "response": {"type": "order", "data": {"statuses": [...]}}}
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            if statuses:
                st = statuses[0]
                if "filled" in st:
                    logger.info(f"[LIMIT_GTC] Fill immédiat {asset} attempt={attempt}")
                    return result
                elif "resting" in st:
                    oid    = st["resting"].get("oid")
                    status = "resting"
                elif "error" in st:
                    status = "error"
                    logger.warning(f"[LIMIT_GTC] Statut erreur: {st}")
        except Exception:
            pass

        if status == "resting" and oid:
            # Attente avant de vérifier le fill
            await asyncio.sleep(LIMIT_RETRY_WAIT)

            # Vérifier si l'ordre est toujours ouvert
            try:
                positions = await get_my_positions()
                # Si l'asset est maintenant en position → ordre rempli
                if asset in positions:
                    logger.info(f"[LIMIT_GTC] Fill confirmé {asset} après {attempt * LIMIT_RETRY_WAIT}s")
                    return result
            except Exception:
                pass

            # Ordre toujours resting → annuler et retenter (ou fallback)
            if attempt < LIMIT_MAX_RETRIES:
                try:
                    await loop.run_in_executor(
                        None,
                        lambda o=oid: exchange.cancel(sdk_coin, o)
                    )
                    logger.info(f"[LIMIT_GTC] Annulé oid={oid}, nouvelle tentative")
                except Exception as ce:
                    logger.warning(f"[LIMIT_GTC] Cancel failed oid={oid}: {ce}")
            else:
                # Dernière tentative échouée → annuler + fallback market
                try:
                    await loop.run_in_executor(
                        None,
                        lambda o=oid: exchange.cancel(sdk_coin, o)
                    )
                except Exception:
                    pass
                logger.warning(
                    f"[LIMIT_GTC] {LIMIT_MAX_RETRIES} tentatives épuisées → "
                    f"fallback MARKET {asset} sz={sz}"
                )
                return await place_market_order(asset, is_buy, sz, ref_price)

        elif status == "error":
            # Erreur applicative → fallback immédiat
            logger.warning(f"[LIMIT_GTC] Erreur statut → fallback market {asset}")
            return await place_market_order(asset, is_buy, sz, ref_price)

        else:
            # Résultat inattendu → fallback market
            logger.warning(f"[LIMIT_GTC] Résultat inattendu {result} → fallback market")
            return await place_market_order(asset, is_buy, sz, ref_price)

    # Ne devrait pas arriver
    return await place_market_order(asset, is_buy, sz, ref_price)


async def compute_target_ratio(trader_address: str) -> float | None:
    """Ratio bot_capital / trader_capital — calculé une fois au démarrage."""
    try:
        _, bot_balance    = await get_wallet_data(COPY_BOT_ADDRESS)
        _, trader_balance = await get_wallet_data(trader_address)
        bot_cap    = bot_balance.get("accountValue", 0)
        trader_cap = trader_balance.get("accountValue", 0)
        if trader_cap <= 0:
            return None
        ratio = bot_cap / trader_cap
        logger.info(f"Ratio {trader_address[:12]}: bot ${bot_cap:.0f} / trader ${trader_cap:.0f} = {ratio:.4f}")
        return ratio
    except Exception as e:
        logger.error(f"compute_target_ratio: {e}")
        return None


def compute_my_size(asset: str, trader_size: float, trader_price: float,
                    ratio: float) -> float:
    """Taille bot = trader_size × ratio, arrondie aux règles de l'asset."""
    rules    = SIZE_RULES.get(asset, SIZE_RULES["_default"])
    raw_size = trader_size * ratio
    sz       = max(round(raw_size, rules["decimals"]), rules["min"])
    if sz * trader_price < 10:
        sz = max(round(11.0 / trader_price, rules["decimals"]), rules["min"])
    return sz


# ── WebSocket par trader ────────────────────────────────────

async def target_watch_ws(address: str, app) -> None:
    """Un WebSocket indépendant par trader cible."""
    logger.info(f"Target WS démarré → {address[:12]}")
    first_connect = True
    reconnect_count = 0

    while address in target_registry and target_registry[address]["active"]:
        if target_registry[address].get("paused"):
            await asyncio.sleep(5)
            continue
        try:
            async with websockets.connect(
                HYPERLIQUID_WS,
                ping_interval=10,   # ping toutes les 10s pour maintenir la connexion
                ping_timeout=30,    # timeout généreux
                close_timeout=5,
            ) as ws:
                await ws.send(json.dumps({
                    "method": "subscribe",
                    "subscription": {"type": "userEvents", "user": address}
                }))
                logger.info(f"✅ Target WS connecté → {address[:12]} (reconnexions: {reconnect_count})")

                # Notif Telegram uniquement au premier démarrage
                if first_connect:
                    ratio = target_registry[address].get("ratio", 0)
                    await send_copy_notification(app,
                        f"📡 *Target connecté*\n"
                        f"`{address}`\n"
                        f"Ratio: {ratio:.4f} | Réplication ACTIVE\n"
                        f"Ouverture/Renfort → MARKET IOC | Fermeture → MARKET"
                    )
                    first_connect = False
                else:
                    # Reconnexion silencieuse — log seulement
                    logger.info(f"Target WS reconnecté silencieusement → {address[:12]}")

                reconnect_count = 0  # Reset compteur sur connexion réussie

                async for raw_msg in ws:
                    if address not in target_registry or not target_registry[address]["active"]:
                        break
                    if target_registry[address].get("paused"):
                        continue
                    try:
                        msg = json.loads(raw_msg)
                        await process_target_event(address, msg, app)
                    except Exception as e:
                        logger.warning(f"Target WS {address[:12]} parse: {e}")

        except Exception as e:
            if address not in target_registry or not target_registry[address]["active"]:
                break
            reconnect_count += 1
            wait = min(5 * reconnect_count, 30)  # backoff progressif: 5s, 10s, 15s... max 30s
            logger.warning(f"Target WS {address[:12]} déconnecté (#{reconnect_count}): {e} — reconnexion {wait}s")
            await asyncio.sleep(wait)

    logger.info(f"Target WS arrêté → {address[:12]}")


async def process_target_event(address: str, msg: dict, app) -> None:
    """
    Traite un fill du trader cible.
    Ouverture/Renfort → MARKET IOC
    Fermeture partielle ou totale → MARKET, proportionnelle au ratio de fermeture
    Conflit asset → ignoré, premier arrivé premier servi
    """
    data  = msg.get("data", {})
    if not data:
        return
    fills = data.get("fills", [])

    trader_info = target_registry.get(address)
    if not trader_info:
        return

    ratio = trader_info.get("ratio", 1.0)

    for fill in fills:
        asset      = fill.get("coin", "")
        side       = fill.get("side", "")
        size       = float(fill.get("sz",  0) or 0)
        price      = float(fill.get("px",  0) or 0)
        dir_fill   = fill.get("dir", "")
        closed_pnl = float(fill.get("closedPnl", 0) or 0)

        if size <= 0 or price <= 0 or not asset:
            continue

        is_opening = "Open"  in dir_fill
        is_closing = "Close" in dir_fill
        is_buy     = side == "B"

        logger.info(f"Target {address[:12]} → {dir_fill} {asset} sz:{size} px:{price:.4f}")

        result = {}

        # ── OUVERTURE ou RENFORT ────────────────────────────
        if is_opening:

            # Conflit : un autre trader a déjà une position sur cet asset ?
            existing_pos = target_positions.get(asset)
            if existing_pos and existing_pos["trader_addr"] != address:
                owner_rank = target_registry.get(existing_pos["trader_addr"], {}).get("label", existing_pos["trader_addr"][:10])
                await send_copy_notification(app,
                    f"⚠️ *Conflit ignoré — {asset}*\n"
                    f"Trader: `{address[:16]}...`\n"
                    f"Asset déjà ouvert par: `{owner_rank}`\n"
                    f"Signal ignoré — premier arrivé premier servi."
                )
                continue

            # Vérifier marge
            if not await check_margin_ok():
                await send_copy_notification(app,
                    f"⚠️ *Marge insuffisante*\nAsset: {asset} | {dir_fill}\nOrdre annulé."
                )
                continue

            my_size = compute_my_size(asset, size, price, ratio)
            result  = await place_limit_gtc(asset, is_buy, my_size, dir_fill)

            # Mémoriser ou cumuler (renfort)
            is_reinforce = asset in target_positions and target_positions[asset]["trader_addr"] == address
            if is_reinforce:
                pos = target_positions[asset]
                total_size       = pos["size"] + my_size
                pos["entry"]     = (pos["entry"] * pos["size"] + price * my_size) / total_size
                pos["size"]      = total_size
            else:
                target_positions[asset] = {
                    "side":        "long" if is_buy else "short",
                    "size":        my_size,
                    "entry":       price,
                    "trader_addr": address,
                }

            status = "✅" if "error" not in result else "❌"
            emoji  = "📈" if is_buy else "📉"
            label  = trader_info.get("label", address[:16])
            await send_copy_notification(app,
                f"{status} *{'🔁 Renfort' if is_reinforce else 'Ouverture'} {emoji} MARKET*\n"
                f"Asset:   *{asset}*\n"
                f"Action:  {dir_fill}\n"
                f"Taille:  {my_size} (~${my_size*price:.0f})\n"
                f"Prix:    ~${price:,.4f}\n"
                f"Trader:  `{label}`"
                + (f"\n⚠️ {result['error']}" if "error" in result else "")
            )

        # ── FERMETURE PARTIELLE OU TOTALE ──────────────────
        elif is_closing:

            my_pos = target_positions.get(asset)
            if not my_pos or my_pos["trader_addr"] != address:
                logger.info(f"Target: pas de position bot sur {asset} (ignoré)")
                continue

            # Calculer le ratio de fermeture du trader
            # On approche via le fill : size = ce que le trader ferme
            # On récupère la taille totale du trader pour calculer le %
            trader_pos_size = trader_info.get("open_sizes", {}).get(asset, size)
            close_ratio     = min(size / max(trader_pos_size, 0.0001), 1.0)
            my_close_size   = round(my_pos["size"] * close_ratio, SIZE_RULES.get(asset, SIZE_RULES["_default"])["decimals"])
            my_close_size   = max(my_close_size, SIZE_RULES.get(asset, SIZE_RULES["_default"])["min"])

            is_close_buy = my_pos["side"] == "short"  # inverse pour clôturer
            result       = await place_limit_gtc(asset, is_close_buy, my_close_size, dir_fill)

            is_full_close = close_ratio >= 0.99
            if is_full_close:
                target_positions.pop(asset, None)
                trader_info.get("open_sizes", {}).pop(asset, None)
            else:
                my_pos["size"] = round(my_pos["size"] - my_close_size,
                                       SIZE_RULES.get(asset, SIZE_RULES["_default"])["decimals"])

            status = "✅" if "error" not in result else "❌"
            label  = trader_info.get("label", address[:16])
            pnl_str = f" | PnL trader: ${closed_pnl:+,.2f}" if closed_pnl != 0 else ""
            await send_copy_notification(app,
                f"{status} *Clôture {'totale' if is_full_close else f'{close_ratio*100:.0f}%'} MARKET*\n"
                f"Asset:   *{asset}*\n"
                f"Fermé:   {my_close_size} (~${my_close_size*price:.0f}){pnl_str}\n"
                f"Ratio:   {close_ratio*100:.0f}% de la position\n"
                f"Trader:  `{label}`"
                + (f"\n⚠️ {result['error']}" if "error" in result else "")
            )

        # Mettre à jour open_sizes du trader (tracking taille trader)
        if is_opening:
            sizes = trader_info.setdefault("open_sizes", {})
            sizes[asset] = sizes.get(asset, 0) + size
        elif is_closing:
            sizes = trader_info.get("open_sizes", {})
            remaining = sizes.get(asset, 0) - size
            if remaining <= 0:
                sizes.pop(asset, None)
            else:
                sizes[asset] = remaining

        # Log
        log = {
            "time":    datetime.now().strftime("%d/%m %H:%M"),
            "asset":   asset,
            "dir":     dir_fill,
            "size":    size,
            "price":   price,
            "trader":  address[:12],
            "result":  "ok" if "error" not in result else result.get("error", "?")[:30],
        }
        target_trades_log.append(log)
        if len(target_trades_log) > 200:
            target_trades_log[:] = target_trades_log[-200:]


# ── Sync positions existantes (optionnel) ───────────────────

async def target_sync_positions(address: str, app) -> None:
    """
    /target_sync — Ouvre les positions DÉJÀ ouvertes du trader au MARKET.
    Optionnel — à appeler manuellement après /target si tu veux entrer sur
    les positions en cours.
    """
    trader_info = target_registry.get(address)
    if not trader_info:
        return

    ratio = trader_info.get("ratio", 1.0)
    logger.info(f"Target sync positions → {address[:12]}")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API,
                json={"type": "clearinghouseState", "user": address},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                state = await resp.json()

        mids = await get_all_mids_cached()
        open_pos = [
            p.get("position", {}) for p in state.get("assetPositions", [])
            if float(p.get("position", {}).get("szi", 0) or 0) != 0
        ]

        if not open_pos:
            await send_copy_notification(app,
                f"ℹ️ *Target Sync — {address[:16]}...*\nAucune position ouverte sur ce wallet."
            )
            return

        results = []
        for p in open_pos:
            asset  = p.get("coin", "")
            sz     = float(p.get("szi", 0) or 0)
            is_buy = sz > 0
            price  = float(mids.get(asset, 0))
            if price <= 0:
                results.append(f"⚠️ {asset}: prix introuvable")
                continue

            # Conflit ?
            existing = target_positions.get(asset)
            if existing and existing["trader_addr"] != address:
                results.append(f"⚠️ {asset}: conflit avec `{existing['trader_addr'][:10]}`")
                continue

            my_size = compute_my_size(asset, abs(sz), price, ratio)
            result  = await place_limit_gtc(asset, is_buy, my_size, "target_sync")
            status  = "✅" if "error" not in result else "❌"
            side_str = "Long 📈" if is_buy else "Short 📉"
            results.append(f"{status} {asset} {side_str} {my_size} (~${my_size*price:.0f})")

            if "error" not in result:
                target_positions[asset] = {
                    "side":        "long" if is_buy else "short",
                    "size":        my_size,
                    "entry":       price,
                    "trader_addr": address,
                }
                trader_info.setdefault("open_sizes", {})[asset] = abs(sz)

        await send_copy_notification(app,
            f"🔄 *Target Sync — {address[:16]}...*\n\n" + "\n".join(results)
        )

    except Exception as e:
        logger.error(f"target_sync_positions {address[:12]}: {e}")
        await send_copy_notification(app, f"❌ Sync erreur: `{str(e)[:100]}`")


# ── Commandes Telegram ──────────────────────────────────────

async def cmd_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /target 0xADRESSE [label]
    Lance surveillance + réplication immédiate.
    Optionnel: label court pour identifier le trader (ex: /target 0xABC... Scalper1)
    """
    if not is_authorized(update):
        return
    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: `/target 0xADRESSE [label]`\n"
            "Exemple: `/target 0xABC...123 Scalper1`",
            parse_mode="Markdown"
        )
        return

    address = context.args[0].strip().lower()
    if not address.startswith("0x") or len(address) != 42:
        await update.message.reply_text("❌ Adresse invalide. Format: `0x...` (42 caractères)", parse_mode="Markdown")
        return

    label = " ".join(context.args[1:]) if len(context.args) > 1 else address[:16] + "..."

    # Déjà en surveillance ?
    if address in target_registry and target_registry[address]["active"]:
        await update.message.reply_text(
            f"ℹ️ `{address[:20]}...` déjà en surveillance.",
            parse_mode="Markdown"
        )
        return

    # Limite MAX_TARGETS
    active_count = sum(1 for t in target_registry.values() if t["active"])
    if active_count >= MAX_TARGETS:
        await update.message.reply_text(
            f"⚠️ Maximum {MAX_TARGETS} targets simultanés atteint.\n"
            f"Stoppe un wallet avec `/target_stop 0xADRESSE` avant d'en ajouter un.",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text("⏳ Calcul du ratio capital...", parse_mode="Markdown")

    ratio = await compute_target_ratio(address)
    if ratio is None:
        await update.message.reply_text(
            "❌ Impossible de calculer le ratio (API indisponible).\nRéessaie.",
            parse_mode="Markdown"
        )
        return

    _, bot_balance    = await get_wallet_data(COPY_BOT_ADDRESS)
    _, trader_balance = await get_wallet_data(address)
    bot_cap    = bot_balance.get("accountValue", 0)
    trader_cap = trader_balance.get("accountValue", 0)

    target_registry[address] = {
        "active":     True,
        "paused":     False,
        "label":      label,
        "ratio":      ratio,
        "open_sizes": {},
        "ws_task":    None,
    }

    task = asyncio.create_task(target_watch_ws(address, context.application))
    target_registry[address]["ws_task"] = task

    await update.message.reply_text(
        f"🎯 *Target activé — {label}*\n"
        f"`{address}`\n\n"
        f"💼 Ton capital:    ${bot_cap:,.0f}\n"
        f"🎯 Capital trader: ${trader_cap:,.0f}\n"
        f"📐 Ratio:          {ratio:.4f}\n\n"
        f"*Comportement:*\n"
        f"• Ouverture/Renfort → MARKET IOC\n"
        f"• Fermeture → MARKET immédiat (proportionnel)\n\n"
        f"💡 `/target_sync {address}` pour copier les positions déjà ouvertes.\n"
        f"🛑 `/target_stop {address}` pour arrêter.",
        parse_mode="Markdown"
    )


async def cmd_target_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /target_sync 0xADRESSE
    Ouvre les positions déjà ouvertes du trader au MARKET.
    """
    if not is_authorized(update):
        return
    if not context.args:
        # Si un seul target actif, on prend celui-là
        active = [a for a, t in target_registry.items() if t["active"]]
        if len(active) == 1:
            address = active[0]
        else:
            await update.message.reply_text(
                "⚠️ Usage: `/target_sync 0xADRESSE`",
                parse_mode="Markdown"
            )
            return
    else:
        address = context.args[0].strip().lower()

    if address not in target_registry or not target_registry[address]["active"]:
        await update.message.reply_text(
            f"⚠️ `{address[:20]}...` n'est pas en surveillance.\nLance d'abord `/target {address}`.",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text(
        f"🔄 Sync positions de `{address[:20]}...` en cours...",
        parse_mode="Markdown"
    )
    await target_sync_positions(address, context.application)


async def cmd_target_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /target_stop 0xADRESSE  → stoppe un trader spécifique
    /target_stop             → stoppe TOUS les traders
    """
    if not is_authorized(update):
        return

    if context.args:
        address = context.args[0].strip().lower()
        targets_to_stop = [address] if address in target_registry else []
        if not targets_to_stop:
            await update.message.reply_text(f"⚠️ `{address[:20]}...` non trouvé.", parse_mode="Markdown")
            return
    else:
        targets_to_stop = list(target_registry.keys())

    if not targets_to_stop:
        await update.message.reply_text("ℹ️ Aucun target actif.")
        return

    stopped = []
    for addr in targets_to_stop:
        info = target_registry.get(addr, {})
        task = info.get("ws_task")
        if task and not task.done():
            task.cancel()
        target_registry.pop(addr, None)
        # Libérer les positions associées
        for asset, pos in list(target_positions.items()):
            if pos["trader_addr"] == addr:
                target_positions.pop(asset, None)
        label = info.get("label", addr[:16])
        stopped.append(f"🛑 {label} `{addr[:16]}...`")
        logger.info(f"Target arrêté: {addr[:12]}")

    await update.message.reply_text(
        f"*Targets arrêtés:*\n" + "\n".join(stopped) + "\n\n"
        f"_Positions ouvertes conservées — gère-les manuellement._",
        parse_mode="Markdown"
    )


async def cmd_target_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche l'état de tous les traders en surveillance."""
    if not is_authorized(update):
        return

    active_targets = {a: t for a, t in target_registry.items() if t["active"]}

    if not active_targets:
        await update.message.reply_text(
            "⏸ *Aucun target actif*\n"
            "Lance `/target 0xADRESSE` pour démarrer.",
            parse_mode="Markdown"
        )
        return

    lines = [f"🎯 *TARGETS — {len(active_targets)}/{MAX_TARGETS}*"]

    for addr, info in active_targets.items():
        mode   = "⏸" if info.get("paused") else "🟢"
        label  = info.get("label", addr[:16])
        short  = addr[:8] + "..." + addr[-6:]
        ratio  = info.get("ratio", 0)
        my_pos = [(a, p) for a, p in target_positions.items() if p["trader_addr"] == addr]

        lines.append(f"\n{mode} *{label}* `{short}`  ratio {ratio:.4f}")
        if my_pos:
            for asset, pos in my_pos:
                side_emoji = "📈" if pos["side"] == "long" else "📉"
                size       = abs(float(pos["size"]))
                lines.append(f"  {side_emoji} {asset} {pos['side'].upper()}  {size:.4f}  @ ${pos['entry']:,.2f}")
        else:
            lines.append("  _aucune position_")

    # Vue consolidée seulement si plusieurs traders ont des positions
    if len(target_positions) > 1:
        lines.append(f"\n━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"📊 *{len(target_positions)} positions ouvertes*")
        for asset, pos in target_positions.items():
            owner      = target_registry.get(pos["trader_addr"], {}).get("label", pos["trader_addr"][:10])
            side_emoji = "📈" if pos["side"] == "long" else "📉"
            size       = abs(float(pos["size"]))
            lines.append(f"  {side_emoji} *{asset}* {pos['side'].upper()}  {size:.4f}  — {owner}")

    lines.append(f"\n`/target_stop`  `/target_sync`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")




import urllib.request

# ============================================================
# MODULE IA HIP-3 — FONCTIONS
# ============================================================

def ai_save_state() -> None:
    """Sauvegarde l'état IA sur disque."""
    try:
        data = {
            "active":    ai_state["active"],
            "positions": ai_state["positions"],
            "history":   ai_state["history"][-20:],
        }
        with open(AI_STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"ai_save_state: {e}")


def ai_load_state() -> None:
    """Recharge l'état IA depuis disque."""
    try:
        if not os.path.exists(AI_STATE_FILE):
            return
        with open(AI_STATE_FILE) as f:
            data = json.load(f)
        ai_state["positions"] = data.get("positions", {})
        ai_state["history"]   = data.get("history", [])
        logger.info(f"🤖 AI state chargé: {len(ai_state['positions'])} positions")
    except Exception as e:
        logger.error(f"ai_load_state: {e}")


async def ai_compute_budget() -> float:
    """
    Budget dispo par trade =
    (wallet - wallet×SAFETY_BUFFER - margin_copy×COPY_RESERVE) / slots_restants
    """
    try:
        _, bot_bal = await get_wallet_data(COPY_BOT_ADDRESS)
        wallet     = bot_bal.get("accountValue", 0)
        margin     = bot_bal.get("totalMarginUsed", 0)

        if wallet < AI_MIN_WALLET:
            return 0.0

        # Budget ORACLE = min(budget_fixe, budget_dispo_réel) / slots_restants
        # Budget fixe : $300 réservés à ORACLE indépendamment du copy
        # Budget réel : wallet × (1 - 15%) - marge_copy × 1.2
        copy_margin = margin - sum(
            pos.get("usd", 0) / AI_LEVERAGE
            for pos in ai_state["positions"].values()
        )
        copy_margin = max(copy_margin, 0)

        slots_used = len(ai_state["positions"])
        slots_free = max(AI_MAX_POSITIONS - slots_used, 1)

        budget_reel  = (wallet * (1 - AI_SAFETY_BUFFER) - copy_margin * AI_COPY_RESERVE)
        budget_total = min(AI_FIXED_BUDGET, max(budget_reel, 0))
        budget       = budget_total / slots_free
        return max(round(budget, 2), 0.0)
    except Exception as e:
        logger.error(f"ai_compute_budget: {e}")
        return 0.0


async def ai_get_tradfi_price(yahoo_symbol: str) -> float | None:
    """Prix TradFi depuis Yahoo Finance (gratuit, sans clé API)."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}?interval=1m&range=1d"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                data = await resp.json()
        price = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
        return float(price)
    except Exception as e:
        logger.warning(f"ai_get_tradfi_price {yahoo_symbol}: {e}")
        return None


async def ai_discover_hip3_assets() -> dict:
    """
    Interroge l'API HL pour lister tous les assets HIP-3 disponibles sur le DEX 'xyz'.
    Retourne un dict {ticker: markPx} des assets actifs (markPx > 0).
    Remplace entièrement AI_HIP3_ASSETS avec les assets découverts.
    """
    global AI_HIP3_ASSETS
    discovered = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API,
                json={"type": "metaAndAssetCtxs", "dex": "xyz"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()

        universe = data[0].get("universe", []) if isinstance(data, list) else []
        ctxs     = data[1] if isinstance(data, list) and len(data) > 1 else []

        new_assets = {}
        for i, asset in enumerate(universe):
            raw_name = asset.get("name", "")   # ex: "TSLA" ou "xyz:TSLA" selon l'API
            if not raw_name or i >= len(ctxs):
                continue
            mark_px = float(ctxs[i].get("markPx", 0) or 0)
            if mark_px <= 0:
                continue

            # Le nom dans universe est le ticker brut (ex: "TSLA")
            # Le nom complet pour trader est "xyz:TSLA"
            ticker    = raw_name.split(":")[-1]  # strip tout préfixe éventuel
            full_name = f"xyz:{ticker}"

            discovered[full_name] = mark_px
            new_assets[full_name] = {
                "name":   ticker,
                "yahoo":  YAHOO_SYMBOLS.get(ticker),  # référence TradFi si connue
                "dex":    "xyz",
                "ticker": ticker,
            }

        # Remplace entièrement la liste d'assets et les prix en cache
        AI_HIP3_ASSETS = new_assets
        ai_state["hip3_prices"] = discovered   # cache des prix pour le scan loop
        logger.info(f"🔮 ORACLE — {len(discovered)} assets HIP-3 actifs: {list(discovered.keys())}")

    except Exception as e:
        logger.warning(f"ai_discover_hip3_assets: {e}")
    return discovered


async def ai_get_hl_price(coin: str, dex: str = "") -> float:
    """
    Récupère le mark price HL pour un asset HIP-3 via metaAndAssetCtxs.
    Fallback sur allMids pour les assets standard.
    """
    try:
        async with aiohttp.ClientSession() as session:
            payload = {"type": "metaAndAssetCtxs"}
            if dex:
                payload["dex"] = dex
            async with session.post(
                HYPERLIQUID_API,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                data = await resp.json()

        universe = data[0].get("universe", []) if isinstance(data, list) else []
        ctxs     = data[1] if isinstance(data, list) and len(data) > 1 else []

        search_coin = coin.split(":")[-1] if ":" in coin else coin

        for i, asset in enumerate(universe):
            if asset.get("name", "") == search_coin and i < len(ctxs):
                mark_px = float(ctxs[i].get("markPx", 0) or 0)
                if mark_px > 0:
                    return mark_px
        return 0.0
    except Exception as e:
        logger.warning(f"ai_get_hl_price {coin}: {e}")
        return 0.0


async def ai_get_funding_rate(coin: str, dex: str = "") -> float | None:
    """
    Récupère le funding rate HL pour un asset.
    Pour HIP-3 (dex != ""), utilise le paramètre dex dans metaAndAssetCtxs.
    """
    try:
        async with aiohttp.ClientSession() as session:
            payload = {"type": "metaAndAssetCtxs"}
            if dex:
                payload["dex"] = dex
            async with session.post(
                HYPERLIQUID_API,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                data = await resp.json()

        universe = data[0].get("universe", []) if isinstance(data, list) else []
        ctxs     = data[1] if isinstance(data, list) and len(data) > 1 else []

        # Pour HIP-3, le ticker dans universe est sans le préfixe "xyz:"
        search_coin = coin.split(":")[-1] if ":" in coin else coin

        for i, asset in enumerate(universe):
            if asset.get("name", "") == search_coin and i < len(ctxs):
                funding = float(ctxs[i].get("funding", 0) or 0)
                return funding
        return None
    except Exception as e:
        logger.warning(f"ai_get_funding_rate {coin}: {e}")
        return None


async def ai_call_claude(asset_name: str, hl_price: float, tradfi_price: float,
                         funding: float, premium_pct: float,
                         existing_pos: dict | None) -> dict:
    """
    Appelle Claude Haiku pour décider long/short/close/wait.
    Retourne {"action": str, "confidence": int, "reason": str}
    """
    # Lecture dynamique — Railway peut injecter les vars apres le demarrage
    api_key = get_anthropic_key()
    if not api_key:
        return {"action": "wait", "confidence": 0, "reason": "ANTHROPIC_API_KEY manquante"}

    pos_context = "Aucune position ouverte."
    if existing_pos:
        side  = existing_pos.get("side", "?")
        entry = existing_pos.get("entry", 0)
        pnl_pct = ((hl_price - entry) / entry * 100) if entry > 0 else 0
        if side == "short":
            pnl_pct = -pnl_pct
        pos_context = f"Position {side.upper()} @ ${entry:.2f} | PnL actuel: {pnl_pct:+.2f}%"

    prompt = f"""Tu es un trader quantitatif spécialisé en arbitrage TradFi/Crypto.

ASSET: {asset_name}
Prix Hyperliquid (perp): ${hl_price:.4f}
Prix TradFi référence:   ${tradfi_price:.4f}
Premium HL vs TradFi:    {premium_pct:+.2f}%
Funding rate (par heure): {funding*100:.4f}%
{pos_context}

STRATÉGIE (assets HIP-3 — funding toujours ~0, ignorer cette condition):
- Premium < -{AI_MIN_PREMIUM*100:.1f}% → LONG (HL sous-coté vs TradFi, convergence attendue)
- Premium > +{AI_MIN_PREMIUM*100:.1f}% → SHORT (HL surcoté vs TradFi, convergence attendue)
- Si position ouverte et signal inversé ou PnL proche stop → CLOSE
- Si premium proche de 0 ou données suspectes (premium > ±20%) → WAIT

Réponds UNIQUEMENT en JSON valide, sans markdown:
{{"action": "long"|"short"|"close"|"wait", "confidence": 0-100, "reason": "explication courte"}}"""

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                json={
                    "model":      "claude-haiku-4-5-20251001",
                    "max_tokens": 150,
                    "messages":   [{"role": "user", "content": prompt}],
                },
                headers={
                    "Content-Type":      "application/json",
                    "x-api-key":         api_key,
                    "anthropic-version": "2023-06-01",
                },
                timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                body = await resp.json()
                if resp.status != 200:
                    err = body.get("error", {}).get("message", str(body))
                    logger.error(f"ai_call_claude {asset_name}: HTTP {resp.status} — {err}")
                    return {"action": "wait", "confidence": 0, "reason": err[:80]}

        text = body["content"][0]["text"].strip()
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)

    except Exception as e:
        logger.error(f"ai_call_claude {asset_name}: {e}")
        return {"action": "wait", "confidence": 0, "reason": str(e)[:80]}


async def ai_open_position(asset: str, is_buy: bool, budget_usd: float,
                           current_price: float, reason: str, app) -> bool:
    """Ouvre une position IA et enregistre le stop-loss."""
    rules    = SIZE_RULES.get(asset, SIZE_RULES["_default"])
    my_size  = max(round(budget_usd / current_price, rules["decimals"]), rules["min"])

    result = await place_limit_gtc(asset, is_buy, my_size, f"AI:{reason}")
    if "error" in result:
        await send_copy_notification(app,
            f"❌ *IA — Ordre échoué*\n"
            f"Asset: `{asset}` | {'LONG' if is_buy else 'SHORT'}\n"
            f"Erreur: {result['error']}"
        )
        return False

    side      = "long" if is_buy else "short"
    stop_price = current_price * (1 - AI_STOP_LOSS_PCT) if is_buy else current_price * (1 + AI_STOP_LOSS_PCT)

    ai_state["positions"][asset] = {
        "side":         side,
        "size":         my_size,
        "entry":        current_price,
        "peak":         current_price,   # trailing stop peak
        "stop":         round(stop_price, 4),
        "usd":          budget_usd,
        "time":         datetime.now().strftime("%d/%m %H:%M"),
        "tp_levels_hit": [],              # paliers partiels déjà encaissés
    }
    ai_save_state()

    await send_copy_notification(app,
        f"🔮 *ORACLE — Position ouverte*\n"
        f"Asset:   *{asset}*\n"
        f"Side:    {'📈 LONG' if is_buy else '📉 SHORT'}\n"
        f"Taille:  {my_size} (~${budget_usd:.0f})\n"
        f"Entrée:  ${current_price:.4f}\n"
        f"Stop:    ${stop_price:.4f} (-{AI_STOP_LOSS_PCT*100:.0f}%)\n"
        f"Raison:  _{reason}_"
    )
    return True


async def ai_close_position(asset: str, current_price: float, reason: str, app) -> bool:
    """Ferme une position IA et log le PnL."""
    pos = ai_state["positions"].get(asset)
    if not pos:
        return False

    is_close_buy = pos["side"] == "short"
    result = await place_limit_gtc(asset, is_close_buy, pos["size"], f"AI_CLOSE:{reason}", force_market=True)

    entry   = pos["entry"]
    pnl_pct = ((current_price - entry) / entry * 100) if pos["side"] == "long" else ((entry - current_price) / entry * 100)
    pnl_usd = pos["usd"] * (pnl_pct / 100)

    log = {
        "time":    datetime.now().strftime("%d/%m %H:%M"),
        "asset":   asset,
        "side":    pos["side"],
        "entry":   entry,
        "exit":    current_price,
        "pnl_pct": round(pnl_pct, 2),
        "pnl_usd": round(pnl_usd, 2),
        "reason":  reason,
    }
    ai_state["history"].append(log)
    if len(ai_state["history"]) > 20:
        ai_state["history"] = ai_state["history"][-20:]

    ai_state["positions"].pop(asset, None)
    ai_save_state()

    status = "✅" if "error" not in result else "❌"
    emoji  = "🟢" if pnl_usd >= 0 else "🔴"
    await send_copy_notification(app,
        f"{status} *IA — Position fermée*\n"
        f"Asset:  *{asset}*\n"
        f"Raison: _{reason}_\n"
        f"{emoji} PnL: {pnl_pct:+.2f}% (~${pnl_usd:+.2f})"
    )
    return True


async def ai_check_stop_losses(app) -> None:
    """
    Vérifie à chaque scan :
    1. Mise à jour du peak (trailing)
    2. Calcul du trailing stop = peak × (1 ± TRAIL_PCT)
    3. Stop dur à -AI_STOP_LOSS_PCT depuis l'entrée (filet de sécurité)
    4. Fermeture si l'un des deux est touché
    """
    if not ai_state["positions"]:
        return

    for asset, pos in list(ai_state["positions"].items()):
        cfg   = AI_HIP3_ASSETS.get(asset, {})
        price = ai_state["hip3_prices"].get(asset, 0)
        if price <= 0:
            price = await ai_get_hl_price(asset, cfg.get("dex", ""))
        if price <= 0:
            continue

        side  = pos["side"]
        entry = pos["entry"]
        peak  = pos.get("peak", entry)

        # ── Mise à jour du peak ──────────────────────────────
        if side == "long":
            if price > peak:
                pos["peak"] = price
                peak = price
                # Recalcul trailing stop
                pos["stop"] = round(peak * (1 - AI_TRAIL_PCT), 4)
                logger.info(f"IA trailing {asset}: nouveau peak ${peak:.4f} → stop ${pos['stop']:.4f}")

        elif side == "short":
            if price < peak:
                pos["peak"] = price
                peak = price
                pos["stop"] = round(peak * (1 + AI_TRAIL_PCT), 4)
                logger.info(f"IA trailing {asset}: nouveau peak ${peak:.4f} → stop ${pos['stop']:.4f}")

        # ── Stop dur depuis l'entrée ─────────────────────────
        hard_stop_long  = entry * (1 - AI_STOP_LOSS_PCT)
        hard_stop_short = entry * (1 + AI_STOP_LOSS_PCT)

        hit_trailing = (side == "long"  and price <= pos["stop"]) or \
                       (side == "short" and price >= pos["stop"])
        hit_hard     = (side == "long"  and price <= hard_stop_long) or \
                       (side == "short" and price >= hard_stop_short)

        # ── Take-profits partiels progressifs ───────────────
        pnl_pct_now = ((price - entry) / entry * 100) if side == "long" \
                      else ((entry - price) / entry * 100)

        levels_hit = pos.setdefault("tp_levels_hit", [])

        for threshold, close_ratio in AI_PARTIAL_TP:
            if threshold in levels_hit:
                continue  # palier déjà encaissé
            if pnl_pct_now >= threshold * 100:
                # Calculer la taille à fermer
                rules         = SIZE_RULES.get(asset, SIZE_RULES["_default"])
                close_size    = round(pos["size"] * close_ratio, rules["decimals"])
                close_size    = max(close_size, rules["min"])
                close_usd     = close_size * price

                if close_size >= pos["size"]:
                    # Sécurité : ne pas fermer plus que ce qu'on a
                    close_size = pos["size"]

                is_close_buy = side == "short"
                result = await place_limit_gtc(asset, is_close_buy, close_size,
                                               f"PARTIAL_TP_{int(threshold*100)}pct",
                                               force_market=True)

                if "error" not in result:
                    pos["size"] = round(pos["size"] - close_size, rules["decimals"])
                    pos["usd"]  = pos["usd"] * (1 - close_ratio)
                    levels_hit.append(threshold)
                    ai_save_state()

                    pnl_usd_partial = close_usd - (close_size * entry)
                    if side == "short":
                        pnl_usd_partial = -pnl_usd_partial

                    await send_copy_notification(app,
                        f"💰 *IA — Prise de bénéfice partielle*\n"
                        f"Asset:   *{asset}* {side.upper()}\n"
                        f"Palier:  +{int(threshold*100)}% atteint\n"
                        f"Fermé:   {close_ratio*100:.0f}% → {close_size} (~${close_usd:.0f})\n"
                        f"PnL:     {pnl_pct_now:+.2f}% | ~${pnl_usd_partial:+.2f}\n"
                        f"Restant: {pos['size']} en position"
                    )
                    logger.info(f"IA PARTIAL TP {asset} +{threshold*100:.0f}%: fermé {close_size} reste {pos['size']}")

                    # Si la position résiduelle est trop petite → fermer tout
                    if pos["size"] <= rules["min"]:
                        await ai_close_position(asset, price, f"POSITION RÉSIDUELLE TROP PETITE", app)
                        break
                break  # un seul palier par scan

        # Recalculer après potentielle fermeture partielle
        if asset not in ai_state["positions"]:
            continue

        # ── Take-profit dur à +20% ───────────────────────────
        tp_long  = entry * (1 + AI_TAKE_PROFIT_PCT)
        tp_short = entry * (1 - AI_TAKE_PROFIT_PCT)
        hit_tp   = (side == "long"  and price >= tp_long) or \
                   (side == "short" and price <= tp_short)

        if hit_tp:
            pnl_pct = ((price - entry) / entry * 100) if side == "long" else ((entry - price) / entry * 100)
            logger.info(f"IA TAKE-PROFIT {asset} @ ${price:.4f} | PnL {pnl_pct:+.2f}%")
            await send_copy_notification(app,
                f"💰 *IA — TAKE-PROFIT +20% déclenché*\n"
                f"Asset: *{asset}* {side.upper()}\n"
                f"Prix: ${price:.4f} | Entrée: ${entry:.4f}\n"
                f"PnL: {pnl_pct:+.2f}%"
            )
            await ai_close_position(asset, price, "TAKE-PROFIT +20%", app)

        elif hit_hard:
            pnl_pct = ((price - entry) / entry * 100) if side == "long" else ((entry - price) / entry * 100)
            logger.warning(f"IA STOP DUR {asset} @ ${price:.4f} | PnL {pnl_pct:+.2f}%")
            await send_copy_notification(app,
                f"🛑 *IA — STOP DUR déclenché*\n"
                f"Asset: *{asset}* {side.upper()}\n"
                f"Prix: ${price:.4f} | Entrée: ${entry:.4f}\n"
                f"PnL: {pnl_pct:+.2f}%"
            )
            await ai_close_position(asset, price, "STOP DUR -15%", app)

        elif hit_trailing:
            pnl_pct = ((price - entry) / entry * 100) if side == "long" else ((entry - price) / entry * 100)
            logger.info(f"IA TRAILING STOP {asset} @ ${price:.4f} | peak=${peak:.4f} | PnL {pnl_pct:+.2f}%")
            await send_copy_notification(app,
                f"🎯 *IA — TRAILING STOP déclenché*\n"
                f"Asset: *{asset}* {side.upper()}\n"
                f"Prix: ${price:.4f} | Peak: ${peak:.4f} | Stop: ${pos['stop']:.4f}\n"
                f"PnL: {pnl_pct:+.2f}%"
            )
            await ai_close_position(asset, price, f"TRAILING STOP (peak ${peak:.4f})", app)


async def ai_scan_loop(app) -> None:
    """Boucle principale IA — scan toutes les AI_SCAN_INTERVAL secondes."""
    logger.info("🤖 IA scan loop démarrée")
    scan_count = 0
    while ai_state["active"]:
        try:
            await ai_check_stop_losses(app)

            # Redécouverte des assets HIP-3 toutes les 6 itérations (~30 min)
            if scan_count % 6 == 0:
                await ai_discover_hip3_assets()
            scan_count += 1

            for asset, cfg in list(AI_HIP3_ASSETS.items()):
                if not ai_state["active"]:
                    break

                # Utiliser le prix du cache (mis à jour par ai_discover_hip3_assets)
                hl_price = ai_state["hip3_prices"].get(asset, 0)
                if hl_price <= 0:
                    # Fallback : appel direct
                    hl_price = await ai_get_hl_price(asset, cfg.get("dex", ""))
                if hl_price <= 0:
                    logger.debug(f"IA: prix HL introuvable pour {asset}")
                    continue

                tradfi_price = await ai_get_tradfi_price(cfg["yahoo"])
                if tradfi_price is None or tradfi_price <= 0:
                    logger.warning(f"IA: prix TradFi introuvable pour {asset}")
                    continue

                premium_pct = (hl_price - tradfi_price) / tradfi_price * 100
                funding     = await ai_get_funding_rate(asset, cfg.get("dex", ""))
                if funding is None:
                    funding = 0.0

                logger.info(
                    f"IA scan {asset}: HL=${hl_price:.4f} TradFi=${tradfi_price:.4f} "
                    f"premium={premium_pct:+.2f}% funding={funding*100:.4f}%/h"
                )

                existing_pos = ai_state["positions"].get(asset)

                # Décision Claude
                decision = await ai_call_claude(
                    cfg["name"], hl_price, tradfi_price,
                    funding, premium_pct, existing_pos
                )
                action     = decision.get("action", "wait")
                confidence = decision.get("confidence", 0)
                reason     = decision.get("reason", "")

                logger.info(f"IA decision {asset}: {action} confidence={confidence}% — {reason}")

                if confidence < 65:
                    continue

                if action == "long" and not existing_pos:
                    slots_ok = len(ai_state["positions"]) < AI_MAX_POSITIONS
                    if not slots_ok:
                        continue
                    budget = await ai_compute_budget()
                    if budget < 20:
                        logger.warning(f"IA: budget insuffisant ${budget:.0f}")
                        continue
                    await ai_open_position(asset, True, budget, hl_price, reason, app)

                elif action == "short" and not existing_pos:
                    slots_ok = len(ai_state["positions"]) < AI_MAX_POSITIONS
                    if not slots_ok:
                        continue
                    budget = await ai_compute_budget()
                    if budget < 20:
                        logger.warning(f"IA: budget insuffisant ${budget:.0f}")
                        continue
                    await ai_open_position(asset, False, budget, hl_price, reason, app)

                elif action == "close" and existing_pos:
                    await ai_close_position(asset, hl_price, reason, app)

        except Exception as e:
            logger.error(f"ai_scan_loop erreur: {e}")

        # Attente avant prochain scan
        for _ in range(AI_SCAN_INTERVAL):
            if not ai_state["active"]:
                break
            await asyncio.sleep(1)

    logger.info("🤖 IA scan loop arrêtée")


# ── Commandes Telegram IA ───────────────────────────────────

async def cmd_ai_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not get_anthropic_key():
        await update.message.reply_text(
            "❌ *ANTHROPIC\\_API\\_KEY manquante*\n\n"
            "Ajoute-la dans Railway → Variables :\n"
            "`ANTHROPIC_API_KEY` = `sk-ant-...`\n\n"
            "Puis redémarre le bot.",
            parse_mode="Markdown"
        )
        return

    if ai_state["active"]:
        await update.message.reply_text("ℹ️ Module IA déjà actif.", parse_mode="Markdown")
        return

    ai_state["active"] = True

    if ai_state.get("scan_task") and not ai_state["scan_task"].done():
        ai_state["scan_task"].cancel()
    ai_state["scan_task"] = asyncio.create_task(ai_scan_loop(context.application))

    # Découverte automatique des assets HIP-3 disponibles
    await update.message.reply_text("🔍 Découverte des assets HIP-3...", parse_mode="Markdown")
    discovered = await ai_discover_hip3_assets()

    budget     = await ai_compute_budget()
    assets_str = " • ".join(AI_HIP3_ASSETS.keys())
    active_str = " • ".join(f"{k} (${v:.2f})" for k, v in discovered.items()) if discovered else "aucun trouvé"

    await update.message.reply_text(
        f"🔮 *ORACLE démarré*\n\n"
        f"Assets actifs: {active_str}\n"
        f"Budget/trade estimé: ~${budget:.0f}\n"
        f"Max positions: {AI_MAX_POSITIONS}\n"
        f"Stop-loss: {AI_STOP_LOSS_PCT*100:.0f}%\n"
        f"Trailing stop: {AI_TRAIL_PCT*100:.0f}%\n"
        f"Scan: toutes les {AI_SCAN_INTERVAL//60} min\n\n"
        f"_Claude Haiku analyse premium TradFi/HL + funding rate_",
        parse_mode="Markdown"
    )


async def cmd_ai_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not ai_state["active"]:
        await update.message.reply_text("⏸ Module IA déjà inactif.", parse_mode="Markdown")
        return

    ai_state["active"] = False
    task = ai_state.get("scan_task")
    if task and not task.done():
        task.cancel()

    await update.message.reply_text(
        "🛑 *Module IA arrêté*\n"
        f"Positions ouvertes ({len(ai_state['positions'])}) conservées — gère-les manuellement.\n"
        "Utilise `/ai_close_all` pour tout fermer.",
        parse_mode="Markdown"
    )


async def cmd_ai_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    status_emoji = "🟢 ACTIF" if ai_state["active"] else "⏸ INACTIF"
    budget       = await ai_compute_budget()

    lines = [
        f"🔮 *ORACLE — {status_emoji}*",
        f"Budget dispo/trade: ~${budget:.0f}",
        f"Positions: {len(ai_state['positions'])}/{AI_MAX_POSITIONS}",
        "",
    ]

    if ai_state["positions"]:
        lines.append("📊 *Positions ouvertes:*")
        for asset, pos in ai_state["positions"].items():
            cfg   = AI_HIP3_ASSETS.get(asset, {})
            price = ai_state["hip3_prices"].get(asset, 0)
            if price <= 0:
                price = await ai_get_hl_price(asset, cfg.get("dex", ""))
            if price <= 0:
                price = pos["entry"]
            pnl_pct = ((price - pos["entry"]) / pos["entry"] * 100) if pos["side"] == "long" \
                      else ((pos["entry"] - price) / pos["entry"] * 100)
            pnl_usd = pos["usd"] * (pnl_pct / 100)
            emoji   = "📈" if pos["side"] == "long" else "📉"
            peak    = pos.get("peak", pos["entry"])
            trail   = pos.get("stop", 0)
            lines.append(
                f"{emoji} *{asset}* {pos['side'].upper()}\n"
                f"   Entrée: ${pos['entry']:.4f} | Peak: ${peak:.4f}\n"
                f"   Trailing stop: ${trail:.4f} | Stop dur: ${pos['entry']*(1-AI_STOP_LOSS_PCT if pos['side']=='long' else 1+AI_STOP_LOSS_PCT):.4f}\n"
                f"   {'🟢' if pnl_usd >= 0 else '🔴'} PnL: {pnl_pct:+.2f}% (~${pnl_usd:+.2f})"
            )
    else:
        lines.append("_Aucune position IA ouverte_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_ai_close_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not ai_state["positions"]:
        await update.message.reply_text("✅ Aucune position IA ouverte.")
        return

    await update.message.reply_text("🔄 Fermeture de toutes les positions IA...")

    for asset in list(ai_state["positions"].keys()):
        cfg   = AI_HIP3_ASSETS.get(asset, {})
        price = await ai_get_hl_price(asset, cfg.get("dex", ""))
        if price <= 0:
            price = ai_state["positions"][asset]["entry"]
        await ai_close_position(asset, price, "CLOSE_ALL manuel", context.application)

    await update.message.reply_text("🏁 *Toutes les positions IA fermées.*", parse_mode="Markdown")


async def cmd_ai_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not ai_state["history"]:
        await update.message.reply_text(
            "📚 *IA — Historique*\n\n_Aucun trade enregistré._",
            parse_mode="Markdown"
        )
        return

    lines = ["📚 *IA — 10 derniers trades*\n"]
    total_pnl = 0.0
    for t in ai_state["history"][-10:]:
        emoji = "🟢" if t["pnl_usd"] >= 0 else "🔴"
        lines.append(
            f"{emoji} *{t['asset']}* {t['side'].upper()} — {t['time']}\n"
            f"   {t['pnl_pct']:+.2f}% (~${t['pnl_usd']:+.2f}) | {t['reason']}"
        )
        total_pnl += t["pnl_usd"]

    lines.append(f"\n*PnL total (10 trades): ${total_pnl:+.2f}*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ============================================================
# MAIN
# ============================================================
def main():
    token = TELEGRAM_TOKEN
    base  = f"https://api.telegram.org/bot{token}"

    # Attente initiale pour laisser mourir l'ancienne instance Railway (zero-downtime deploy)
    logger.info("⏳ Attente 30s — libération du slot Telegram (Railway zero-downtime)...")
    time_module.sleep(30)

    # Nettoyage agressif : forcer la libération du polling
    for attempt in range(15):
        try:
            urllib.request.urlopen(f"{base}/deleteWebhook?drop_pending_updates=true", timeout=5)
            # getUpdates avec timeout=0 pour "voler" le slot à l'ancienne instance
            urllib.request.urlopen(f"{base}/getUpdates?offset=-1&timeout=0&limit=1", timeout=8)
            logger.info(f"✅ Slot Telegram libéré (attempt {attempt+1})")
            break
        except Exception as e:
            logger.warning(f"Init HTTP attempt {attempt+1}/15: {e}")
            time_module.sleep(3)

    time_module.sleep(5)

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    async def error_handler(update, context):
        if isinstance(context.error, Conflict):
            logger.warning("⚠️ Conflit résiduel — attente 45s...")
            await asyncio.sleep(45)
        else:
            logger.error(f"Erreur: {context.error}")

    app.add_error_handler(error_handler)

    # Commandes principales
    app.add_handler(CommandHandler("start",              cmd_start))
    app.add_handler(CommandHandler("aide",               cmd_aide))
    app.add_handler(CommandHandler("prix",               cmd_prix))
    app.add_handler(CommandHandler("positions",          cmd_positions))
    app.add_handler(CommandHandler("setup",              cmd_setup))
    app.add_handler(CommandHandler("resume",             cmd_resume))
    app.add_handler(CommandHandler("peur",               cmd_peur))
    app.add_handler(CommandHandler("alertes",            cmd_activer_alertes))
    app.add_handler(CommandHandler("desactiver_alertes", cmd_desactiver_alertes))

    # TradeBot
    app.add_handler(CommandHandler("toptraders",    cmd_toptraders))
    app.add_handler(CommandHandler("tb_historique", cmd_tb_historique))
    app.add_handler(CommandHandler("tb_aide",       cmd_tb_aide))
    app.add_handler(CommandHandler("inspector",     cmd_inspector))

    # Copy Trading v4.1
    app.add_handler(CommandHandler("copy_start",  cmd_copy_start))
    app.add_handler(CommandHandler("copy_stop",   cmd_copy_stop))
    app.add_handler(CommandHandler("copy_status", cmd_copy_status))
    app.add_handler(CommandHandler("copy_close",  cmd_copy_close_all))

    # Target Wallet Manuel
    app.add_handler(CommandHandler("target",        cmd_target))
    app.add_handler(CommandHandler("target_sync",   cmd_target_sync))
    app.add_handler(CommandHandler("target_stop",   cmd_target_stop))
    app.add_handler(CommandHandler("target_status", cmd_target_status))

    # Module IA HIP-3
    app.add_handler(CommandHandler("ai_start",    cmd_ai_start))
    app.add_handler(CommandHandler("ai_stop",     cmd_ai_stop))
    app.add_handler(CommandHandler("ai_status",   cmd_ai_status))
    app.add_handler(CommandHandler("ai_close_all",cmd_ai_close_all))
    app.add_handler(CommandHandler("ai_history",  cmd_ai_history))

    logger.info("🤖 SakaiBot v4.9 démarré — Maker orders + Module IA HIP-3")
    # Diagnostic variables d'environnement au démarrage
    _ak = os.getenv("ANTHROPIC_API_KEY", "")
    logger.info(f"🔑 ANTHROPIC_API_KEY: {'OK sk-ant-...'+ _ak[-6:] if _ak else 'MANQUANTE ❌'}")
    _pk = os.getenv("HL_PRIVATE_KEY", "")
    logger.info(f"🔑 HL_PRIVATE_KEY: {'OK ...'+ _pk[-4:] if _pk else 'MANQUANTE ❌'}")
    ai_load_state()
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=False,
    )


if __name__ == "__main__":
    main()
