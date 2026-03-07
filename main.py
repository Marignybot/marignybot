#!/usr/bin/env python3
"""
MarignyCryptoBot - Bot Telegram pour suivi crypto & Hyperliquid
"""

import asyncio
import os
import json
import logging
import re
import time as time_module
from datetime import datetime, time
import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import Conflict

# ============================================================
# CONFIGURATION — variables chargees depuis .env
# ============================================================

TELEGRAM_TOKEN = "8413363300:AAEldjYE3nqAoF9-tZdYurwH1PNfUWJbZEQ"
HYPERLIQUID_ADDRESS = "0x6e89b986FBB4B985AcCC9B3CfEE4c7B5301D9a5C"
AUTHORIZED_USER_ID = 1429797974

# ============================================================
# COPY TRADING — SakaiBot
# ============================================================
COPY_BOT_ADDRESS   = "0xd849f8E96d7BE1A1fc7CA5291Dc6603a47dF8dFD"
HL_PRIVATE_KEY     = os.getenv("HL_PRIVATE_KEY", "")
COPY_CAPITAL       = 1000.0   # capital total
COPY_ASSETS        = ["BTC", "ETH", "HYPE"]   # assets surveillés phase 1
COPY_ALLOC         = 100.0    # $100 par asset (test)
COPY_MAX_SIZE      = 50.0     # $50 max par trade (test)
MARGIN_SAFETY      = 0.5      # marge de sécurité 50%

# Levier max par asset
COPY_LEVERAGE = {
    "BTC":  5,   # x5 → liquidation à -20%
    "ETH":  4,   # x4 → liquidation à -25%
    "HYPE": 3,   # x3 → liquidation à -33%
}

# Suivi du capital déployé par asset (évite de dépasser l'allocation)
copy_deployed = {
    "BTC":  0.0,
    "ETH":  0.0,
    "HYPE": 0.0,
}

# State global du copy trading
copy_state = {
    "active":        False,          # copy trading actif ou non
    "watched":       {},             # {asset: adresse_trader}
    "positions":     {},             # {asset: {side, size, entry}}
    "ws_tasks":      {},             # {asset: asyncio.Task}
    "last_update":   {},             # {asset: timestamp}
    "total_pnl":     0.0,
    "trades_log":    [],
    "last_ranked":   [],             # derniers traders classés par /toptraders
}

HYPERLIQUID_API            = "https://api.hyperliquid.xyz/info"
WATCHED_TOKENS             = ["BTC", "ETH", "HYPE"]
ALERT_THRESHOLD_PERCENT    = 5.0
LIQUIDATION_ALERT_PERCENT  = 15.0
MORNING_HOUR_UTC           = 8
MORNING_MIN_UTC            = 0
EVENING_HOUR_UTC           = 20
EVENING_MIN_UTC            = 0

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
last_prices = {}

def is_authorized(update) -> bool:
    return update.effective_user.id == AUTHORIZED_USER_ID


# ============================================================
# HYPERLIQUID — PRIX
# ============================================================
async def get_crypto_prices() -> dict:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(HYPERLIQUID_API, json={"type": "allMids"}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                all_mids = await resp.json()
            async with session.post(HYPERLIQUID_API, json={"type": "metaAndAssetCtxs"}, timeout=aiohttp.ClientTimeout(total=10)) as resp2:
                meta_data = await resp2.json()
        result = {}
        universe    = meta_data[0].get("universe", []) if isinstance(meta_data, list) else []
        ctxs        = meta_data[1] if isinstance(meta_data, list) and len(meta_data) > 1 else []
        ctx_by_name = {}
        for i, asset in enumerate(universe):
            if i < len(ctxs):
                ctx_by_name[asset.get("name", "")] = ctxs[i]
        for symbol in WATCHED_TOKENS:
            mid = all_mids.get(symbol)
            if mid is None:
                continue
            usd    = float(mid)
            ctx    = ctx_by_name.get(symbol, {})
            prev   = float(ctx.get("prevDayPx", usd) or usd)
            change = ((usd - prev) / prev * 100) if prev else 0
            result[symbol] = {
                "usd": usd,
                "eur": usd * 0.92,
                "usd_24h_change": round(change, 2),
                "volume": float(ctx.get("dayNtlVlm", 0) or 0)
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
                json={"type": "candleSnapshot", "req": {"coin": symbol, "interval": interval, "startTime": start_time}},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                candles = await resp.json()
        if not candles or not isinstance(candles, list):
            return {}
        return {
            "closes":  [float(c["c"]) for c in candles],
            "highs":   [float(c["h"]) for c in candles],
            "lows":    [float(c["l"]) for c in candles],
            "volumes": [float(c["v"]) for c in candles]
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
            cp = closes[-1]
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
            async with session.get("https://api.alternative.me/fng/?limit=1", timeout=aiohttp.ClientTimeout(total=10)) as resp:
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
        open_pos = [p for p in data.get("assetPositions", []) if float(p.get("position", {}).get("szi", 0)) != 0]
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
                headers={"User-Agent": "MarignyCryptoBot/1.0"},
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
                "change": c["item"].get("data", {}).get("price_change_percentage_24h", {}).get("usd", 0) or 0
            }
            for c in data.get("coins", [])[:3]
        ]
    except Exception as e:
        logger.error(f"Erreur trending: {e}")
        return []


# ============================================================
# POSITIONS & BALANCE
# ============================================================
async def get_hyperliquid_positions() -> list:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API,
                json={"type": "clearinghouseState", "user": HYPERLIQUID_ADDRESS},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                return (await resp.json()).get("assetPositions", [])
    except Exception as e:
        logger.error(f"Erreur positions: {e}")
        return []


async def get_hyperliquid_balance() -> dict:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API,
                json={"type": "clearinghouseState", "user": HYPERLIQUID_ADDRESS},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data   = await resp.json()
                margin = data.get("crossMarginSummary") or data.get("marginSummary") or {}
                av     = float(margin.get("accountValue", 0))
                mu     = float(margin.get("totalMarginUsed", 0))
                pnl    = float(margin.get("totalUnrealizedPnl", 0))
                if av == 0:
                    av = float(data.get("crossAccountValue", 0) or data.get("withdrawable", 0) or 0)
                if pnl == 0:
                    pnl = sum(float(p.get("position", {}).get("unrealizedPnl", 0)) for p in data.get("assetPositions", []))
                return {"accountValue": av, "totalMarginUsed": mu, "totalUnrealizedPnl": pnl}
    except Exception as e:
        logger.error(f"Erreur balance: {e}")
        return {}


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


def format_daily_summary(news: list, trending: list) -> str:
    now   = datetime.now().strftime("%d/%m/%Y %H:%M")
    lines = [f"🌅 *Resume Crypto — {now}*\n", "━━━━━━━━━━━━━━━━━━━━", "📰 *3 Actus du Jour*\n"]
    if news:
        for i, n in enumerate(news, 1):
            lines.append(f"{i}. [{n['title']}]({n['url']})\n")
    else:
        lines.append("_Indisponible._\n")
    lines += ["━━━━━━━━━━━━━━━━━━━━", "🔥 *Top 3 Trending*\n"]
    if trending:
        for i, t in enumerate(trending, 1):
            c = t.get("change", 0) or 0
            lines.append(f"{i}. *{t['name']}* (${t['symbol']}) — Rank #{t['rank']} {'🟢' if c >= 0 else '🔴'} {c:+.1f}%\n")
    else:
        lines.append("_Indisponible._\n")
    lines += ["━━━━━━━━━━━━━━━━━━━━", "_Bonne journee depuis Vallauris! 🌴_"]
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
# ============================================================
# MODULE TRADEBOT — ETF SYNTHETIQUE HYPERLIQUID
# ============================================================
# ============================================================

tradebot_history = []

TRADEBOT_WEIGHTS      = {"consistency": 0.35, "drawdown": 0.30, "winrate": 0.20, "pnl": 0.15}
TRADEBOT_MIN_TRADES   = 50
TRADEBOT_MAX_DRAWDOWN = 40.0


def score_consistency(v: float) -> float:
    return min(100.0, max(0.0, v))


def score_drawdown(mdd: float) -> float:
    if mdd <= 5:  return 100.0
    if mdd <= 10: return 85.0
    if mdd <= 15: return 70.0
    if mdd <= 20: return 55.0
    if mdd <= 25: return 40.0
    if mdd <= 30: return 30.0
    if mdd <= 35: return 20.0
    if mdd <= 40: return 10.0
    return 0.0


def score_winrate(wr: float) -> float:
    if wr >= 70: return 100.0
    if wr >= 60: return 80.0
    if wr >= 55: return 65.0
    if wr >= 50: return 40.0
    return 10.0


def score_pnl_relative(pnl: float, max_pnl: float) -> float:
    return min(100.0, (pnl / max_pnl) * 100) if max_pnl > 0 else 0.0


def compute_composite_score(consistency: float, drawdown: float, winrate: float, pnl_score: float) -> float:
    return round(
        score_consistency(consistency) * TRADEBOT_WEIGHTS["consistency"] +
        score_drawdown(drawdown)       * TRADEBOT_WEIGHTS["drawdown"]    +
        score_winrate(winrate)         * TRADEBOT_WEIGHTS["winrate"]      +
        pnl_score                      * TRADEBOT_WEIGHTS["pnl"],
        1
    )


def stars(value: float, max_val: float = 100) -> str:
    n = round((value / max_val) * 5)
    return "★" * n + "☆" * (5 - n)


async def fetch_top_traders_hl() -> list:
    """
    Selection basee sur metriques 30j :
    - ROI 30j > 20%
    - MDD < 40%
    - Winrate (consistance) > 40%
    - Actif dans les 15 derniers jours (window day a un PnL non nul)
    - Capital actuel > $10k
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard",
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json()

        raw_rows = data.get("leaderboardRows", []) if isinstance(data, dict) else data
        logger.info(f"Leaderboard: {len(raw_rows)} traders")

        # Pre-filtrage sur les métriques du leaderboard (window 30j)
        candidates = []
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

                # Extraire métriques par window
                metrics = {}
                for w in stats_dict.get("windowPerformances", []):
                    if isinstance(w, list) and len(w) == 2:
                        metrics[w[0]] = w[1]

                m30 = metrics.get("month", {})
                mat = metrics.get("allTime", {})

                roi_30d  = float(m30.get("roi", 0) or 0) * 100
                pnl_30d  = float(m30.get("pnl", 0) or 0)
                pnl_at   = float(mat.get("pnl", 0) or 0)

                # Filtres pre-selection :
                # - ROI 30j entre 20% et 10000% (evite aberrations)
                # - PnL 30j > $5k (trader actif et rentable)
                # - PnL allTime > $10k (trader serieux)
                if not (20 <= roi_30d <= 10000):
                    continue
                if pnl_30d < 5000:
                    continue
                if pnl_at < 10000:
                    continue

                candidates.append((address, roi_30d, pnl_30d))

            except Exception:
                continue

        # Trier par ROI 30j décroissant
        candidates.sort(key=lambda x: x[1], reverse=True)
        top_candidates = candidates[:150]
        logger.info(f"{len(candidates)} candidats qualifiés | top 150 retenus (meilleur ROI 30j: {top_candidates[0][1]:.0f}%)")

        # Appels portfolio en parallèle
        async def fetch_portfolio(session, address):
            try:
                async with session.post(
                    "https://api-ui.hyperliquid.xyz/info",
                    json={"type": "portfolio", "user": address},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    portfolio = await resp.json()

                # Parser les windows
                windows = {}
                for item in portfolio:
                    if isinstance(item, list) and len(item) == 2:
                        windows[item[0]] = item[1]

                # --- Filtre activité 15j ---
                now_ms     = datetime.now().timestamp() * 1000
                cutoff_ms  = now_ms - (15 * 86400 * 1000)
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
                        last_ts  = month_hist[-1][0]
                        days_ago = (now_ms - last_ts) / (86400 * 1000)
                        recent_activity = days_ago <= 15

                if not recent_activity:
                    return None

                # --- Métriques 30j ---
                m30          = windows.get("perpMonth") or windows.get("month", {})
                pnl_hist_30  = [float(p[1]) for p in m30.get("pnlHistory", []) if isinstance(p, list)]
                acv_hist_30  = [float(p[1]) for p in m30.get("accountValueHistory", []) if isinstance(p, list) and float(p[1]) > 0]

                if not pnl_hist_30 or not acv_hist_30:
                    return None

                pnl_30d = pnl_hist_30[-1]
                if pnl_30d <= 0:
                    return None

                current_value = acv_hist_30[-1]
                if current_value < 10000:
                    return None

                # ROI 30j
                base_30 = max(current_value - pnl_30d, 1)
                roi_30d = min((pnl_30d / base_30) * 100, 2000)

                # MDD 30j
                peak   = acv_hist_30[0]
                mdd_30 = 0.0
                for v in acv_hist_30:
                    if v > peak:
                        peak = v
                    dd = (peak - v) / peak * 100 if peak > 0 else 0
                    if dd > mdd_30:
                        mdd_30 = dd

                # --- Métriques allTime ---
                at_data     = windows.get("perpAllTime") or windows.get("allTime", {})
                pnl_hist_at = [float(p[1]) for p in at_data.get("pnlHistory", []) if isinstance(p, list)]
                acv_hist_at = [float(p[1]) for p in at_data.get("accountValueHistory", []) if isinstance(p, list) and float(p[1]) > 0]

                pnl_at  = pnl_hist_at[-1] if pnl_hist_at else 0
                peak_at = max(acv_hist_at) if acv_hist_at else 1
                roe_at  = min((pnl_at / max(peak_at, 1)) * 100, 9999)

                # MDD allTime
                mdd_at = 0.0
                if acv_hist_at:
                    pk = acv_hist_at[0]
                    for v in acv_hist_at:
                        if v > pk:
                            pk = v
                        dd = (pk - v) / pk * 100 if pk > 0 else 0
                        if dd > mdd_at:
                            mdd_at = dd

                # Filtre MDD < 40% sur 30j ET allTime
                worst_mdd = max(mdd_30, mdd_at)
                if worst_mdd > 40:
                    return None

                # Winrate hebdo
                week_data = windows.get("perpWeek") or windows.get("week", {})
                week_pnl  = [float(p[1]) for p in week_data.get("pnlHistory", []) if isinstance(p, list)]
                week_pos  = sum(1 for p in week_pnl if p > 0)
                winrate   = (week_pos / max(len(week_pnl), 1)) * 100

                # Filtre winrate >= 60%
                if winrate < 60:
                    return None

                # Consistance 30j
                pos_moves   = sum(1 for i in range(1, len(pnl_hist_30)) if pnl_hist_30[i] > pnl_hist_30[i-1])
                consistency = (pos_moves / max(len(pnl_hist_30) - 1, 1)) * 100

                return {
                    "address":     address,
                    "pnl":         pnl_30d,
                    "pnl_at":      pnl_at,
                    "roi":         round(roi_30d, 1),
                    "roe_at":      round(roe_at, 1),
                    "mdd":         round(worst_mdd, 1),
                    "consistency": round(consistency, 1),
                    "winrate":     round(winrate, 1),
                    "n_trades":    0,
                    "asset_pnl":   {},
                }
            except Exception as e:
                logger.warning(f"Portfolio {address[:12]}... erreur: {e}")
                return None

        async with aiohttp.ClientSession() as session:
            tasks = [fetch_portfolio(session, addr) for addr, _, _ in top_candidates]
            results = await asyncio.gather(*tasks)

        traders = [r for r in results if r is not None]
        logger.info(f"Traders qualifiés après tous les filtres: {len(traders)}")
        return traders

    except Exception as e:
        logger.error(f"Erreur fetch_top_traders: {e}")
        return []


def apply_exclusion_filters(traders: list) -> list:
    """Filtres : MDD > 60% exclus, le reste passe."""
    filtered = [t for t in traders if t["mdd"] <= 60 and t["pnl"] > 0]
    logger.info(f"Apres filtres: {len(filtered)}/{len(traders)} traders retenus")
    return filtered


def rank_and_score_traders(traders: list) -> list:
    """Score et classe tous les traders filtres."""
    if not traders:
        return []
    max_pnl = max(t["pnl"] for t in traders)
    scored  = []
    for t in traders:
        roi_score = min(100.0, t.get("roi", 0) / 5)  # ROI 500% -> score 100
        composite = compute_composite_score(t["consistency"], t["mdd"], t.get("week_winrate", t["winrate"]), roi_score)
        scored.append({**t, "score": composite})
    return sorted(scored, key=lambda x: x["score"], reverse=True)


def build_top5_report(top5: list) -> str:
    """Rapport Top 5 global avec adresses completes copiables."""
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    lines = [
        "🏆 *ETF SYNTHETIQUE — TradeBot*",
        f"📅 {now}",
        "━━━━━━━━━━━━━━━━━━━━",
        "*📊 TOP 5 GLOBAL* (filtres: MDD<40% | WR≥60% | actif 15j)",
        "",
    ]
    for i, t in enumerate(top5, 1):
        verdict = "🟢 FORT" if t["score"] >= 65 else ("🟡 MOYEN" if t["score"] >= 45 else "🔴 FAIBLE")
        lines.append(f"*#{i}* — Score: *{t['score']}/100* {verdict}")
        lines.append(f"ROI 30j: {t.get('roi',0):.0f}% | ROE all: {t.get('roe_at',0):.0f}%")
        lines.append(f"MDD: {t['mdd']:.0f}% | WinRate: {t['winrate']:.0f}% | PnL 30j: ${t['pnl']:+,.0f}")
        lines.append(f"`{t['address']}`")
        lines.append("")
    avg = round(sum(t["score"] for t in top5) / len(top5), 1) if top5 else 0
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"Score ETF moyen: *{avg}/100*")
    return "\n".join(lines)


async def build_asset_report(traders: list) -> str:
    """Top 2 par asset via userFills — appels paralleles rapides."""
    ASSETS = ["BTC", "ETH", "SOL", "HYPE", "TAO"]

    async def get_asset_pnl(session, trader):
        try:
            async with session.post(
                "https://api-ui.hyperliquid.xyz/info",
                json={"type": "userFills", "user": trader["address"]},
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                fills = await resp.json()
            asset_pnl = {}
            # Mapping noms Hyperliquid → noms affichés
            COIN_MAP = {
                "BTC":    "BTC", "WBTC":  "BTC",
                "ETH":    "ETH", "WETH":  "ETH",
                "SOL":    "SOL", "kSOL":  "SOL",
                "HYPE":   "HYPE","kHYPE": "HYPE",
                "TAO":    "TAO", "kTAO":  "TAO",
            }
            all_coins = set()
            if isinstance(fills, list):
                for fill in fills:
                    if not isinstance(fill, dict):
                        continue
                    coin_raw = fill.get("coin", "")
                    all_coins.add(coin_raw)
                    coin = COIN_MAP.get(coin_raw, coin_raw)
                    pnl_fill = float(fill.get("closedPnl", 0) or 0)
                    if coin:
                        asset_pnl[coin] = asset_pnl.get(coin, 0.0) + pnl_fill
            if all_coins:
                logger.info(f"Coins trouvés pour {trader['address'][:10]}: {sorted(all_coins)[:15]}")
            return {**trader, "asset_pnl": asset_pnl}
        except Exception:
            return {**trader, "asset_pnl": {}}

    async with aiohttp.ClientSession() as session:
        tasks = [get_asset_pnl(session, t) for t in traders]
        enriched = await asyncio.gather(*tasks)

    lines = ["🎯 *TOP 2 PAR ASSET* (30j)", ""]
    assigned = set()

    for asset in ASSETS:
        candidates = []
        for t in enriched:
            apnl = t.get("asset_pnl", {}).get(asset, 0.0)
            if apnl > 0:
                candidates.append((apnl, t))
        candidates.sort(key=lambda x: x[0], reverse=True)
        # Dédupliquer par adresse
        seen = set()
        top2 = []
        for apnl, t in candidates:
            if t["address"] not in seen:
                seen.add(t["address"])
                top2.append((apnl, t))
            if len(top2) == 2:
                break

        lines.append(f"— *{asset}* —")
        if not top2:
            lines.append("  _Aucun spécialiste identifié parmi le top 20_")
        else:
            for rank, (apnl, t) in enumerate(top2, 1):
                assigned.add(t["address"])
                lines.append(f"*#{rank}* PnL {asset}: ${apnl:+,.0f} | Score: {t['score']}/100")
                lines.append(f"`{t['address']}`")
        lines.append("")

    return "\n".join(lines)



def build_history_report() -> str:
    """Construit le rapport d'historique des selections."""
    if not tradebot_history:
        return (
            "📚 *Historique TradeBot*\n\n"
            "_Aucune selection anterieure enregistree._\n"
            "Lance /toptraders pour demarrer ta premiere analyse."
        )
    lines = ["📚 *Historique des Selections TradeBot*\n"]
    for session in tradebot_history[-5:]:
        lines.append(f"📅 *{session['date']}* — Score ETF moyen: {session['avg_score']}/100")
        for t in session["traders"]:
            addr = t["address"][:6] + "..." + t["address"][-4:]
            lines.append(f"   #{t['rank']} `{addr}` — {t['score']}/100")
        lines.append("")
    lines.append(f"_Total sessions enregistrees: {len(tradebot_history)}_")
    return "\n".join(lines)


# ============================================================
# COMMANDES TRADEBOT
# ============================================================
async def cmd_toptraders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande principale: analyse et selection des 5 meilleurs traders."""
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "🔍 *TradeBot en cours d'analyse...*\n\n"
        "• Recuperation du leaderboard Hyperliquid\n"
        "• Application des filtres d'exclusion\n"
        "• Calcul des scores composites\n"
        "• Selection du top 5\n\n"
        "_Patiente quelques secondes..._",
        parse_mode="Markdown"
    )
    raw_traders = await fetch_top_traders_hl()
    if not raw_traders:
        await update.message.reply_text(
            "❌ *Donnees leaderboard indisponibles.*\nReessaie dans quelques minutes.",
            parse_mode="Markdown"
        )
        return
    filtered = apply_exclusion_filters(raw_traders)
    if not filtered:
        await update.message.reply_text(
            "⚠️ Aucun trader ne passe les filtres d'exclusion.\nDonnees peut-etre partielles.",
            parse_mode="Markdown"
        )
        return
    ranked = rank_and_score_traders(filtered)
    top5   = ranked[:5]
    session_data = {
        "date":      datetime.now().strftime("%d/%m/%Y %H:%M"),
        "avg_score": round(sum(t["score"] for t in top5) / len(top5), 1),
        "traders":   [{"rank": i+1, "address": t["address"], "score": t["score"]} for i, t in enumerate(top5)],
    }
    tradebot_history.append(session_data)
    if len(tradebot_history) > 30:
        tradebot_history.pop(0)
    # Message 1 : Top 5 global immediatement
    await update.message.reply_text(build_top5_report(top5), parse_mode="Markdown")

    # Message 2 : Top 2 par asset avec userFills (appels rapides sur top 20 seulement)
    await update.message.reply_text("🔍 Analyse par asset en cours...")
    asset_report = await build_asset_report(ranked[:20])
    await update.message.reply_text(asset_report, parse_mode="Markdown")

    # Stocker les résultats pour /copy_start
    copy_state["last_ranked"] = ranked[:20]
    await update.message.reply_text(
        "💡 *Tip:* Lance `/copy\_start BTC`, `/copy\_start HYPE` ou `/copy\_start` pour démarrer le copy trading.",
        parse_mode="Markdown"
    )


async def cmd_inspector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Analyse complète d'un wallet Hyperliquid."""
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
            # Portfolio
            async with session.post(
                "https://api-ui.hyperliquid.xyz/info",
                json={"type": "portfolio", "user": address},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                portfolio = await resp.json()

            # Fills pour PnL par asset
            async with session.post(
                "https://api-ui.hyperliquid.xyz/info",
                json={"type": "userFills", "user": address},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp2:
                fills = await resp2.json()

            # Positions ouvertes
            async with session.post(
                "https://api-ui.hyperliquid.xyz/info",
                json={"type": "clearinghouseState", "user": address},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp3:
                state = await resp3.json()

        # Parser windows portfolio
        windows = {}
        for item in portfolio:
            if isinstance(item, list) and len(item) == 2:
                windows[item[0]] = item[1]

        def get_window_stats(win_key, fallback_key):
            w = windows.get(win_key) or windows.get(fallback_key, {})
            pnl_hist = [float(p[1]) for p in w.get("pnlHistory", []) if isinstance(p, list)]
            acv_hist = [float(p[1]) for p in w.get("accountValueHistory", []) if isinstance(p, list) and float(p[1]) > 0]
            pnl = pnl_hist[-1] if pnl_hist else 0
            capital = acv_hist[-1] if acv_hist else 0
            # MDD
            mdd = 0.0
            if acv_hist:
                pk = acv_hist[0]
                for v in acv_hist:
                    if v > pk: pk = v
                    dd = (pk - v) / pk * 100 if pk > 0 else 0
                    if dd > mdd: mdd = dd
            # ROI
            base = max(capital - pnl, 1)
            roi = min((pnl / base) * 100, 9999) if pnl > 0 else (pnl / base) * 100
            # Consistance
            pos = sum(1 for i in range(1, len(pnl_hist)) if pnl_hist[i] > pnl_hist[i-1])
            consist = (pos / max(len(pnl_hist) - 1, 1)) * 100
            return {"pnl": pnl, "capital": capital, "mdd": mdd, "roi": roi, "consist": consist}

        d1  = get_window_stats("perpDay",   "day")
        w7  = get_window_stats("perpWeek",  "week")
        m30 = get_window_stats("perpMonth", "month")
        at  = get_window_stats("perpAllTime","allTime")

        # Winrate depuis week
        week_data = windows.get("perpWeek") or windows.get("week", {})
        week_pnl  = [float(p[1]) for p in week_data.get("pnlHistory", []) if isinstance(p, list)]
        winrate   = (sum(1 for p in week_pnl if p > 0) / max(len(week_pnl), 1)) * 100

        # Dernier trade (timestamp)
        now_ms   = datetime.now().timestamp() * 1000
        last_fill_ts = None
        if isinstance(fills, list) and fills:
            last_fill_ts = max((f.get("time", 0) for f in fills if isinstance(f, dict)), default=None)
        days_inactive = ((now_ms - last_fill_ts) / 86400000) if last_fill_ts else None

        # PnL par asset (top 5)
        asset_pnl = {}
        COIN_MAP = {"BTC": "BTC", "ETH": "ETH", "SOL": "SOL", "HYPE": "HYPE", "TAO": "TAO"}
        if isinstance(fills, list):
            for fill in fills:
                if not isinstance(fill, dict): continue
                coin = fill.get("coin", "")
                coin = COIN_MAP.get(coin, coin)
                pv = float(fill.get("closedPnl", 0) or 0)
                asset_pnl[coin] = asset_pnl.get(coin, 0.0) + pv
        top_assets = sorted(asset_pnl.items(), key=lambda x: x[1], reverse=True)[:5]

        # Positions ouvertes
        positions = []
        if isinstance(state, dict):
            for pos in state.get("assetPositions", []):
                p = pos.get("position", {})
                sz = float(p.get("szi", 0) or 0)
                if sz != 0:
                    positions.append({
                        "coin": p.get("coin", "?"),
                        "side": "Long 📈" if sz > 0 else "Short 📉",
                        "size": abs(sz),
                        "entry": float(p.get("entryPx", 0) or 0),
                        "pnl":  float(p.get("unrealizedPnl", 0) or 0),
                        "lev":  p.get("leverage", {}).get("value", "?"),
                    })

        # Score composite
        roi_score = min(100.0, m30["roi"] / 5)
        score = round(
            score_consistency(m30["consist"]) * 0.35 +
            score_drawdown(m30["mdd"])        * 0.30 +
            score_winrate(winrate)            * 0.20 +
            roi_score                         * 0.15, 1
        )
        verdict = "🟢 FORT" if score >= 65 else ("🟡 MOYEN" if score >= 45 else "🔴 FAIBLE")

        # Construire le rapport
        lines = [
            f"🔎 *INSPECTOR — Wallet Analysis*",
            f"`{address}`",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"",
            f"📊 *PERFORMANCE*",
            f"{'Période':<12} {'PnL':>12} {'ROI':>8} {'MDD':>6}",
            f"{'24h':<12} ${d1['pnl']:>+10,.0f} {d1['roi']:>7.1f}% {d1['mdd']:>5.1f}%",
            f"{'7j':<12} ${w7['pnl']:>+10,.0f} {w7['roi']:>7.1f}% {w7['mdd']:>5.1f}%",
            f"{'30j':<12} ${m30['pnl']:>+10,.0f} {m30['roi']:>7.1f}% {m30['mdd']:>5.1f}%",
            f"{'AllTime':<12} ${at['pnl']:>+10,.0f} {at['roi']:>7.1f}% {at['mdd']:>5.1f}%",
            f"",
            f"🎯 *QUALITÉ (base 30j)*",
            f"Score ETF:   *{score}/100* {verdict}",
            f"Win Rate:    {winrate:.0f}%",
            f"Consistance: {m30['consist']:.0f}%",
            f"Capital:     ${m30['capital']:,.0f}",
        ]

        if days_inactive is not None:
            lines.append(f"Dernier trade: il y a *{days_inactive:.0f}j*")

        if top_assets:
            lines.append(f"")
            lines.append(f"💰 *PnL PAR ASSET (allTime)*")
            for coin, pnl in top_assets:
                emoji = "🟢" if pnl >= 0 else "🔴"
                lines.append(f"{emoji} {coin:<8} ${pnl:>+12,.0f}")

        if positions:
            lines.append(f"")
            lines.append(f"⚡ *POSITIONS OUVERTES ({len(positions)})*")
            for pos in positions[:5]:
                lines.append(
                    f"{pos['side']} {pos['coin']} x{pos['lev']} | "
                    f"PnL: ${pos['pnl']:+,.0f}"
                )
        else:
            lines.append(f"")
            lines.append(f"⚡ *Aucune position ouverte*")

        lines.append(f"━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"_Données Hyperliquid en temps réel_")

        report = "\n".join(lines)
        await update.message.reply_text(report, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Inspector erreur: {e}")
        await update.message.reply_text(f"❌ Erreur lors de l'analyse: `{str(e)[:100]}`", parse_mode="Markdown")


# ============================================================
# MODULE COPY TRADING — WebSocket Hyperliquid
# ============================================================

async def send_copy_notification(app, message: str):
    """Envoie une notification Telegram pour le copy trading."""
    try:
        await app.bot.send_message(
            chat_id=AUTHORIZED_USER_ID,
            text=message,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Notification copy trading erreur: {e}")


async def place_order(asset: str, is_buy: bool, size: float, reason: str = "", leverage: int = 1) -> dict:
    """
    Place un ordre sur Hyperliquid via l'API exchange.
    Utilise l'API Wallet pour signer.
    """
    try:
        if not HL_PRIVATE_KEY:
            logger.error("HL_PRIVATE_KEY non définie")
            return {"error": "Clé privée manquante"}

        from eth_account import Account
        from eth_account.messages import encode_defunct
        import hashlib, struct

        # Récupérer le prix actuel
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.hyperliquid.xyz/info",
                json={"type": "allMids"},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                mids = await resp.json()

        price = float(mids.get(asset, 0))
        if price <= 0:
            return {"error": f"Prix {asset} introuvable"}

        # Slippage 0.1% pour ordre market
        limit_px = price * (1.005 if is_buy else 0.995)
        limit_px = round(limit_px, 2)

        # Construction de l'ordre Hyperliquid
        timestamp = int(datetime.now().timestamp() * 1000)

        order_payload = {
            "action": {
                "type": "order",
                "orders": [{
                    "a": await get_asset_index(asset),
                    "b": is_buy,
                    "p": str(limit_px),
                    "s": str(round(size, 6)),
                    "r": False,  # reduce_only
                    "t": {"limit": {"tif": "Ioc"}}  # Immediate or Cancel = market
                }],
                "grouping": "na"
            },
            "nonce": timestamp,
            "vaultAddress": None
        }

        # Signature EIP-712
        account = Account.from_key(HL_PRIVATE_KEY)
        signature = sign_order(account, order_payload["action"], timestamp)

        order_payload["signature"] = signature

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.hyperliquid.xyz/exchange",
                json=order_payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                result = await resp.json()

        logger.info(f"Ordre {asset} {'BUY' if is_buy else 'SELL'} {size} → {result}")
        return result

    except Exception as e:
        logger.error(f"Erreur place_order {asset}: {e}")
        return {"error": str(e)}


async def get_asset_index(asset: str) -> int:
    """Récupère l'index de l'asset dans le meta Hyperliquid."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.hyperliquid.xyz/info",
                json={"type": "meta"},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                meta = await resp.json()
        universe = meta.get("universe", [])
        for i, asset_info in enumerate(universe):
            if asset_info.get("name") == asset:
                return i
        return -1
    except Exception as e:
        logger.error(f"get_asset_index erreur: {e}")
        return -1


def sign_order(account, action: dict, nonce: int) -> dict:
    """Signe un ordre avec EIP-712 pour Hyperliquid."""
    import json as json_mod
    from eth_account.structured_data.hashing import hash_domain, hash_message
    
    # Hash de l'action
    action_str = json_mod.dumps(action, separators=(',', ':'), sort_keys=True)
    action_hash = hashlib.sha256(action_str.encode()).hexdigest()
    
    # Message à signer
    msg = f"\x19Hyperliquid SignedAction\n{action_hash}\n{nonce}\n"
    msg_hash = encode_defunct(text=msg)
    signed = account.sign_message(msg_hash)
    
    return {
        "r": hex(signed.r),
        "s": hex(signed.s),
        "v": signed.v
    }


async def get_my_positions() -> dict:
    """Récupère les positions actuelles du compte bot."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.hyperliquid.xyz/info",
                json={"type": "clearinghouseState", "user": COPY_BOT_ADDRESS},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                state = await resp.json()

        positions = {}
        if isinstance(state, dict):
            for pos in state.get("assetPositions", []):
                p = pos.get("position", {})
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
    """Vérifie que la marge disponible est > 50%."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.hyperliquid.xyz/info",
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


async def watch_trader_positions(asset: str, trader_address: str, app) -> None:
    """
    WebSocket — surveille les positions d'un trader en temps réel.
    Dès qu'il ouvre/ferme/modifie une position sur l'asset, on copie.
    """
    import websockets
    import json as json_mod

    ws_url = "wss://api.hyperliquid.xyz/ws"
    logger.info(f"WebSocket démarré — surveillance {asset} → {trader_address[:12]}...")

    while copy_state["active"]:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                # Souscription aux positions du trader
                subscribe_msg = json_mod.dumps({
                    "method": "subscribe",
                    "subscription": {
                        "type": "userEvents",
                        "user": trader_address
                    }
                })
                await ws.send(subscribe_msg)
                logger.info(f"✅ WebSocket {asset} connecté")

                async for raw_msg in ws:
                    if not copy_state["active"]:
                        break

                    try:
                        msg = json_mod.loads(raw_msg)
                        await process_trader_event(asset, trader_address, msg, app)
                    except Exception as e:
                        logger.warning(f"WebSocket {asset} parse erreur: {e}")

        except Exception as e:
            if not copy_state["active"]:
                break
            logger.warning(f"WebSocket {asset} déconnecté: {e} — reconnexion dans 5s")
            await asyncio.sleep(5)

    logger.info(f"WebSocket {asset} arrêté")


async def process_trader_event(asset: str, trader_address: str, msg: dict, app) -> None:
    """
    Traite un événement WebSocket du trader.
    - Ouverture : taille = COPY_MAX_SIZE ($100) / prix | levier = copié du trader
    - Fermeture : ferme notre position complète
    """
    data = msg.get("data", {})
    if not data:
        return

    fills = data.get("fills", [])
    for fill in fills:
        coin = fill.get("coin", "")
        if coin != asset:
            continue

        side     = fill.get("side", "")
        size     = float(fill.get("sz", 0) or 0)
        price    = float(fill.get("px", 0) or 0)
        dir_fill = fill.get("dir", "")

        logger.info(f"Trader {trader_address[:12]} → {dir_fill} {asset} sz:{size} px:{price}")

        if size <= 0 or price <= 0:
            continue

        is_opening = "Open" in dir_fill
        is_closing = "Close" in dir_fill
        is_buy     = side == "B"
        leverage   = 1

        if is_opening:
            # 1. Vérifier si allocation encore disponible
            deployed = copy_deployed.get(asset, 0.0)
            if deployed + COPY_MAX_SIZE > COPY_ALLOC:
                await send_copy_notification(app,
                    f"⚠️ *Allocation {asset} pleine*\n"
                    f"Déployé: ${deployed:.0f} / ${COPY_ALLOC:.0f}\n"
                    f"Renfort du trader ignoré."
                )
                continue

            # 2. Vérifier la marge globale
            margin_ok = await check_margin_ok()
            if not margin_ok:
                await send_copy_notification(app,
                    f"⚠️ *Copy Trading — Marge insuffisante*\n"
                    f"Asset: {asset} | Action: {dir_fill}\n"
                    f"Marge < {int(MARGIN_SAFETY*100)}% — ordre annulé"
                )
                continue

            # 3. Taille en unités : $100 / prix actuel
            my_size = round(COPY_MAX_SIZE / price, 6)

            # 4. Levier : copier le trader mais capé par COPY_LEVERAGE
            max_lev = COPY_LEVERAGE.get(asset, 3)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        "https://api.hyperliquid.xyz/info",
                        json={"type": "clearinghouseState", "user": trader_address},
                        timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        trader_state = await resp.json()
                for pos in trader_state.get("assetPositions", []):
                    p = pos.get("position", {})
                    if p.get("coin") == asset:
                        trader_lev = int(p.get("leverage", {}).get("value", 1) or 1)
                        leverage   = min(trader_lev, max_lev)
                        break
            except Exception:
                leverage = max_lev

            # 5. Enregistrer le capital déployé
            copy_deployed[asset] = deployed + COPY_MAX_SIZE

        elif is_closing:
            # Fermer notre position complète
            my_positions = await get_my_positions()
            my_pos = my_positions.get(asset)
            if not my_pos:
                logger.info(f"Pas de position ouverte sur {asset} — fermeture ignorée")
                continue
            my_size  = my_pos["size"]
            is_buy   = my_pos["side"] == "short"  # inverser pour fermer
            leverage = 1
            # Reset capital déployé
            copy_deployed[asset] = 0.0
            logger.info(f"Capital {asset} reset → $0")
        else:
            continue

        # Passer l'ordre
        result = await place_order(asset, is_buy, my_size, dir_fill, leverage)

        # Log
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

        # Notification
        emoji  = "📈" if is_buy else "📉"
        status = "✅" if "error" not in result else "❌"
        notif  = (
            f"{status} *Copy Trade {emoji}*\n"
            f"Asset:   *{asset}*\n"
            f"Action:  {dir_fill}\n"
            f"Taille:  {my_size} unités (~${COPY_MAX_SIZE:.0f})\n"
            f"Levier:  x{leverage}\n"
            f"Prix:    ${price:,.2f}\n"
            f"Trader:  `{trader_address[:16]}...`"
        )
        if "error" in result:
            notif += f"\n⚠️ Erreur: {result['error']}"

        await send_copy_notification(app, notif)


async def sync_existing_positions(watched: dict, app) -> None:
    """
    Au démarrage : vérifie si les traders ont déjà des positions ouvertes
    et les copie immédiatement sans attendre un événement WebSocket.
    """
    logger.info("Synchronisation des positions existantes...")

    for asset, trader_addr in watched.items():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.hyperliquid.xyz/info",
                    json={"type": "clearinghouseState", "user": trader_addr},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    state = await resp.json()

            for pos in state.get("assetPositions", []):
                p = pos.get("position", {})
                if p.get("coin") != asset:
                    continue
                sz = float(p.get("szi", 0) or 0)
                if sz == 0:
                    continue

                # Vérifier qu'on n'a pas déjà cette position
                my_positions = await get_my_positions()
                if asset in my_positions:
                    logger.info(f"Position {asset} déjà présente sur notre compte")
                    continue

                # Vérifier allocation et marge
                deployed = copy_deployed.get(asset, 0.0)
                if deployed + COPY_MAX_SIZE > COPY_ALLOC:
                    continue
                if not await check_margin_ok():
                    continue

                # Prix actuel
                async with aiohttp.ClientSession() as session2:
                    async with session2.post(
                        "https://api.hyperliquid.xyz/info",
                        json={"type": "allMids"},
                        timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp2:
                        mids = await resp2.json()
                current_price = float(mids.get(asset, 0))
                if current_price <= 0:
                    continue

                is_buy     = sz > 0
                trader_lev = int(p.get("leverage", {}).get("value", 1) or 1)
                leverage   = min(trader_lev, COPY_LEVERAGE.get(asset, 3))
                my_size    = round(COPY_MAX_SIZE / current_price, 6)

                result = await place_order(asset, is_buy, my_size, "Sync Open", leverage)
                copy_deployed[asset] = deployed + COPY_MAX_SIZE

                side_str = "Long 📈" if is_buy else "Short 📉"
                status   = "✅" if "error" not in result else "❌"
                notif_lines = [
                    f"{status} *Position existante synchronisée*",
                    f"Asset:  *{asset}*",
                    f"Action: {side_str}",
                    f"Taille: {my_size} (~${COPY_MAX_SIZE:.0f})",
                    f"Levier: x{leverage}",
                    f"Prix:   ${current_price:,.2f}",
                    f"Trader: `{trader_addr[:16]}...`",
                    f"_(Ouverte avant démarrage du bot)_",
                ]
                await send_copy_notification(app, "\n".join(notif_lines))
                logger.info(f"Sync {asset} {side_str} OK")

        except Exception as e:
            logger.warning(f"Sync {asset} erreur: {e}")

    logger.info("Synchronisation terminée")


async def start_copy_trading(watched: dict, app) -> None:
    """
    Lance les WebSockets pour chaque asset/trader.
    Si déjà actif et trader change :
      - Arrête l'ancien WebSocket
      - Lance le nouveau
      - Garde les positions ouvertes existantes
    """
    old_watched  = copy_state.get("watched", {})
    my_positions = await get_my_positions()

    copy_state["active"]  = True
    copy_state["watched"] = watched
    if "ws_tasks" not in copy_state:
        copy_state["ws_tasks"] = {}

    active_assets = []
    notif_lines   = ["🤖 *SakaiBot — Copy Trading*", ""]

    for asset, trader_addr in watched.items():
        if not trader_addr:
            notif_lines.append(f"⬜ {asset}: aucun trader qualifié")
            continue

        old_trader     = old_watched.get(asset)
        trader_changed = old_trader and old_trader != trader_addr

        # Arrêter l'ancien WebSocket si trader changé
        if trader_changed and asset in copy_state["ws_tasks"]:
            copy_state["ws_tasks"][asset].cancel()
            logger.info(f"WebSocket {asset} ancien trader arrêté")

        # Lancer le nouveau WebSocket
        task = asyncio.create_task(
            watch_trader_positions(asset, trader_addr, app)
        )
        copy_state["ws_tasks"][asset] = task
        active_assets.append(asset)
        logger.info(f"WebSocket lancé: {asset} → {trader_addr[:12]}...")

        # Gérer le capital déployé
        if asset in my_positions:
            copy_deployed[asset] = COPY_MAX_SIZE
            if trader_changed:
                notif_lines.append(
                    f"🔁 {asset}: nouveau trader assigné\n"
                    f"   `{trader_addr[:16]}...`\n"
                    f"   Position existante conservée ✅"
                )
            else:
                notif_lines.append(
                    f"✅ {asset}: `{trader_addr[:16]}...`\n"
                    f"   Position conservée"
                )
        else:
            copy_deployed[asset] = 0.0
            notif_lines.append(
                f"👀 {asset}: `{trader_addr[:16]}...`\n"
                f"   En surveillance"
            )

    if not active_assets:
        copy_state["active"] = False
        await send_copy_notification(app, "❌ *Copy Trading annulé*\nAucun trader qualifié.")
        return

    notif_lines.append("")
    notif_lines.append(f"📡 Assets: {', '.join(active_assets)}")
    notif_lines.append(
        f"💰 BTC x{COPY_LEVERAGE['BTC']} | ETH x{COPY_LEVERAGE['ETH']} | HYPE x{COPY_LEVERAGE['HYPE']} — ${COPY_MAX_SIZE:.0f} max/trade"
    )
    await send_copy_notification(app, "\n".join(notif_lines))
    logger.info(f"✅ Copy trading actif — assets: {active_assets}")

    # Sync uniquement les assets sans position ouverte
    new_watched = {a: t for a, t in watched.items() if a not in my_positions and t}
    if new_watched:
        await sync_existing_positions(new_watched, app)


async def stop_copy_trading() -> None:
    """Arrête tous les WebSockets de copy trading."""
    copy_state["active"] = False
    for asset, task in copy_state["ws_tasks"].items():
        task.cancel()
        logger.info(f"WebSocket {asset} annulé")
    copy_state["ws_tasks"] = {}
    logger.info("Copy trading arrêté")


# ============================================================
# COMMANDES TELEGRAM — Copy Trading
# ============================================================

async def cmd_copy_start_asset(update: Update, context: ContextTypes.DEFAULT_TYPE, asset: str):
    """Shortcut: démarre le copy trading sur un asset spécifique."""
    context.args = [asset]
    await cmd_copy_start(update, context)


async def cmd_copy_stop_asset_direct(update: Update, context: ContextTypes.DEFAULT_TYPE, asset: str):
    """Shortcut: arrête le copy trading sur un asset spécifique."""
    context.args = [asset]
    await cmd_copy_stop_asset(update, context)


async def cmd_copy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Lance le copy trading sur un ou plusieurs assets.
    Usage:
      /copy_start       → tous les assets configurés
      /copy_start BTC   → BTC uniquement
      /copy_start HYPE  → HYPE uniquement
    Si /toptraders n'a pas été lancé, le fait automatiquement.
    """
    if not is_authorized(update):
        return

    args    = context.args
    assets  = []

    if args:
        asset_req = args[0].upper()
        if asset_req not in COPY_ASSETS:
            await update.message.reply_text(
                f"❌ Asset `{asset_req}` non supporté.\n"
                f"Assets disponibles: {', '.join(COPY_ASSETS)}",
                parse_mode="Markdown"
            )
            return
        assets = [asset_req]
    else:
        assets = COPY_ASSETS

    # Si pas de données /toptraders → lancer l'analyse d'abord
    if not copy_state.get("last_ranked"):
        await update.message.reply_text(
            "🔍 Aucune analyse récente — lancement de `/toptraders` automatiquement...",
            parse_mode="Markdown"
        )
        raw_traders = await fetch_top_traders_hl()
        if not raw_traders:
            await update.message.reply_text("❌ Impossible de récupérer les traders. Réessaie.", parse_mode="Markdown")
            return
        filtered = apply_exclusion_filters(raw_traders)
        if not filtered:
            await update.message.reply_text("⚠️ Aucun trader ne passe les filtres.", parse_mode="Markdown")
            return
        ranked = rank_and_score_traders(filtered)
        copy_state["last_ranked"] = ranked[:20]
        # Envoyer le rapport top 5
        await update.message.reply_text(build_top5_report(ranked[:5]), parse_mode="Markdown")

    ranked = copy_state["last_ranked"]

    # Construire le mapping asset → meilleur trader
    watched      = {}
    assigned_set = set()
    for asset in assets:
        for t in ranked:
            apnl = t.get("asset_pnl", {}).get(asset, 0.0)
            if apnl > 0 and t["address"] not in assigned_set:
                watched[asset] = t["address"]
                assigned_set.add(t["address"])
                break
        if asset not in watched:
            # Fallback : meilleur score global si pas de spécialiste asset
            for t in ranked:
                if t["address"] not in assigned_set:
                    watched[asset] = t["address"]
                    assigned_set.add(t["address"])
                    break

    if not watched:
        await update.message.reply_text("⚠️ Aucun trader disponible pour ces assets.", parse_mode="Markdown")
        return

    # Arrêter uniquement les WebSockets des assets concernés
    for asset in assets:
        if asset in copy_state.get("ws_tasks", {}):
            copy_state["ws_tasks"][asset].cancel()
            logger.info(f"WebSocket {asset} arrêté avant relance")

    await start_copy_trading(watched, context.application)


async def cmd_copy_stop_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Arrête le copy trading sur un asset ou tout.
    Usage:
      /copy_stop        → stoppe tout
      /copy_stop BTC    → stoppe BTC uniquement
    """
    if not is_authorized(update):
        return

    args = context.args

    if not args:
        # Stopper tout
        if not copy_state["active"]:
            await update.message.reply_text("⏸ Copy trading déjà inactif.")
            return
        await stop_copy_trading()
        await update.message.reply_text(
            "🛑 *Copy Trading arrêté*\n"
            "Tes positions ouvertes restent actives — gère-les manuellement.",
            parse_mode="Markdown"
        )
        return

    # Stopper un asset spécifique
    asset = args[0].upper()
    if asset not in COPY_ASSETS:
        await update.message.reply_text(f"❌ Asset `{asset}` non reconnu.", parse_mode="Markdown")
        return

    task = copy_state.get("ws_tasks", {}).get(asset)
    if task:
        task.cancel()
        del copy_state["ws_tasks"][asset]
        if asset in copy_state["watched"]:
            del copy_state["watched"][asset]
        # Si plus aucun asset actif → désactiver complètement
        if not copy_state["ws_tasks"]:
            copy_state["active"] = False
        await update.message.reply_text(
            f"🛑 *Copy Trading {asset} arrêté*\n"
            f"Position ouverte conservée — gère-la manuellement.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"⏸ Pas de WebSocket actif sur {asset}.")


async def cmd_copy_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Force la synchronisation immédiate des positions ouvertes des traders surveillés."""
    if not is_authorized(update):
        return

    if not copy_state["active"] or not copy_state["watched"]:
        await update.message.reply_text(
            "⏸ Aucun trader surveillé.\nLance d'abord `/start_hype`, `/start_btc` ou `/copy_start`.",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text(
        f"🔄 *Sync forcée en cours...*\nTraders surveillés: {len(copy_state['watched'])}",
        parse_mode="Markdown"
    )

    await sync_existing_positions(copy_state["watched"], context.application)

    await update.message.reply_text(
        "✅ *Sync terminée*\nVérifie `/copy_status` pour voir les positions.",
        parse_mode="Markdown"
    )


async def cmd_copy_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le statut du copy trading."""
    if not is_authorized(update):
        return

    if not copy_state["active"]:
        await update.message.reply_text(
            "⏸ *Copy Trading inactif*\n"
            "Lance `/toptraders` pour démarrer.",
            parse_mode="Markdown"
        )
        return

    # Récupérer les positions actuelles
    positions = await get_my_positions()

    lines = [
        "🤖 *COPY TRADING — Statut*",
        f"Status: {'🟢 ACTIF' if copy_state['active'] else '🔴 INACTIF'}",
        f"Traders suivis: {len(copy_state['watched'])}",
        "",
        "📡 *Assets surveillés:*",
    ]

    for asset in COPY_ASSETS:
        trader = copy_state["watched"].get(asset, None)
        if trader:
            pos = positions.get(asset)
            if pos:
                emoji = "📈" if pos["side"] == "long" else "📉"
                lines.append(
                    f"{emoji} {asset}: {pos['side'].upper()} "
                    f"sz:{pos['size']} | PnL: ${pos['upnl']:+,.0f}"
                )
            else:
                lines.append(f"👀 {asset}: en surveillance (pas de position)")
        else:
            lines.append(f"⬜ {asset}: aucun trader assigné")

    if copy_state["trades_log"]:
        lines.append("")
        lines.append(f"📋 *Derniers trades ({min(3, len(copy_state['trades_log']))}) :*")
        for t in copy_state["trades_log"][-3:]:
            status = "✅" if t["result"] == "ok" else "❌"
            lines.append(f"{status} {t['time']} | {t['asset']} {t['dir']} @ ${t['price']:,.0f}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_copy_close_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ferme toutes les positions du bot."""
    if not is_authorized(update):
        return

    await update.message.reply_text("🔄 Fermeture de toutes les positions en cours...")

    positions = await get_my_positions()
    if not positions:
        await update.message.reply_text("✅ Aucune position ouverte.")
        return

    results = []
    for asset, pos in positions.items():
        is_buy = pos["side"] == "short"  # pour fermer un short on achète
        result = await place_order(asset, is_buy, pos["size"], "Close All")
        status = "✅" if "error" not in result else "❌"
        results.append(f"{status} {asset} fermé")

    await update.message.reply_text(
        "🏁 *Fermeture terminée*\n" + "\n".join(results),
        parse_mode="Markdown"
    )



async def cmd_tb_historique(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche l'historique des selections precedentes."""
    if not is_authorized(update):
        return
    await update.message.reply_text(build_history_report(), parse_mode="Markdown")


async def cmd_tb_aide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Aide specifique au module TradeBot."""
    if not is_authorized(update):
        return
    msg = (
        "🤖 *TradeBot — Module ETF Hyperliquid*\n\n"
        "Selectionne les 5 meilleurs traders sur les *30 derniers jours*.\n\n"
        "📐 *Scoring composite:*\n"
        "   🥇 Consistance 30j :  35%\n"
        "   🥈 Drawdown max 40% : 30%\n"
        "   🥉 Win Rate hebdo :   20%\n"
        "   4️⃣ ROI 30j :          15%\n\n"
        "🚫 *Filtres d'exclusion:*\n"
        "   • MDD > 40% (30j ou allTime)\n"
        "   • Win Rate < 60%\n"
        "   • Inactif depuis > 15 jours\n"
        "   • Capital < $10 000\n"
        "   • PnL 30j negatif ou < $5 000\n\n"
        "📊 *Rapport inclut:*\n"
        "   ROI 30j | ROE allTime | MDD | WinRate | PnL 30j\n"
        "   + Top 2 par asset (BTC, ETH, SOL, HYPE, TAO)\n\n"
        "📋 *Commandes:*\n"
        "   /toptraders — Lancer une analyse\n"
        "   /inspector — Analyser un wallet manuellement\n"
        "   /tb\\_historique — Selections passees\n"
        "   /tb\\_aide — Cette aide\n\n"
        "_v2.0 — Analyse | v3.0 prevue: copy trading auto_"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ============================================================
# COMMANDES PRINCIPALES
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    msg = (
        "👋 *Bienvenue sur SakaiBot! 🤖*\n\n"
        "📊 *Marche & Analyse*\n"
        "   /prix — Prix en temps reel\n"
        "   /setup — Analyse technique complete\n"
        "   /resume — Resume du marche\n"
        "   /peur — Fear & Greed Index\n\n"
        "📈 *Mes Positions*\n"
        "   /positions — Positions Hyperliquid\n\n"
        "🏆 *TradeBot — ETF Hyperliquid*\n"
        "   /toptraders — Analyser les meilleurs traders\n"
        "   /inspector — Analyser un wallet\n\n"
        "🟢 *Start Copy Trading*\n"
        "   /start\\_btc — Démarrer BTC\n"
        "   /start\\_eth — Démarrer ETH\n"
        "   /start\\_hype — Démarrer HYPE\n"
        "   /copy\\_start — Démarrer tous\n\n"
        "🔴 *Stop Copy Trading*\n"
        "   /stop\\_btc — Arrêter BTC\n"
        "   /stop\\_eth — Arrêter ETH\n"
        "   /stop\\_hype — Arrêter HYPE\n"
        "   /copy\\_stop — Arrêter tout\n\n"
        "📊 *Statut & Gestion*\n"
        "   /copy\\_sync — Forcer sync positions ouvertes\n"
        "   /copy\\_status — Statut en temps réel\n"
        "   /copy\\_close — Fermer toutes les positions\n"
        "   /tb\\_historique — Historique des sélections\n"
        "   /tb\\_aide — Aide TradeBot\n\n"
        "🔔 *Alertes*\n"
        "   /alertes — Activer les alertes auto\n"
        "   /desactiver\\_alertes — Stopper les alertes\n\n"
        "ℹ️ /aide — Affiche ce message\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_prix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text("⏳ Recuperation des prix...", parse_mode="Markdown")
    prices = await get_crypto_prices()
    await update.message.reply_text(format_prices(prices), parse_mode="Markdown")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text("⏳ Connexion a Hyperliquid...", parse_mode="Markdown")
    positions, balance = await asyncio.gather(get_hyperliquid_positions(), get_hyperliquid_balance())
    await update.message.reply_text(format_positions(positions, balance), parse_mode="Markdown")


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text("⏳ Analyse technique en cours (RSI, EMA, S/R, Fibo, Volume)...", parse_mode="Markdown")
    prices, trending = await asyncio.gather(get_crypto_prices(), get_crypto_trending())
    msg = await analyze_setup(prices, trending)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text("⏳ Preparation du resume...", parse_mode="Markdown")
    news, trending = await asyncio.gather(get_crypto_news(), get_crypto_trending())
    await update.message.reply_text(format_daily_summary(news, trending), parse_mode="Markdown", disable_web_page_preview=True)


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
    job_queue.run_repeating(job_price_alert, interval=3600, first=10, chat_id=chat_id, name=f"alert_{chat_id}")
    job_queue.run_daily(job_daily_summary, time=time(MORNING_HOUR_UTC, MORNING_MIN_UTC), chat_id=chat_id, name=f"daily_{chat_id}")
    job_queue.run_daily(job_twice_daily,   time=time(MORNING_HOUR_UTC, MORNING_MIN_UTC), chat_id=chat_id, name=f"sr_{chat_id}_matin")
    job_queue.run_daily(job_twice_daily,   time=time(EVENING_HOUR_UTC, EVENING_MIN_UTC), chat_id=chat_id, name=f"sr_{chat_id}_soir")
    await update.message.reply_text(
        f"✅ *Alertes activees!*\n\n"
        f"• Variation > {ALERT_THRESHOLD_PERCENT}% / heure\n"
        f"• Resume + Check S/R a *9h00*\n"
        f"• Check S/R + liquidation a *21h00*\n\n"
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
    await update.message.reply_text("🔕 Toutes les alertes sont desactivees.")


# ============================================================
# MAIN
# ============================================================
def main():

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    async def error_handler(update, context):
        if isinstance(context.error, Conflict):
            logger.warning("⚠️ Conflit détecté (autre instance) — attente 10s...")
            await asyncio.sleep(10)
        else:
            logger.error(f"Erreur: {context.error}")

    app.add_error_handler(error_handler)

    # Commandes existantes
    app.add_handler(CommandHandler("start",              cmd_start))
    app.add_handler(CommandHandler("aide",               cmd_aide))
    app.add_handler(CommandHandler("prix",               cmd_prix))
    app.add_handler(CommandHandler("positions",          cmd_positions))
    app.add_handler(CommandHandler("setup",              cmd_setup))
    app.add_handler(CommandHandler("resume",             cmd_resume))
    app.add_handler(CommandHandler("peur",               cmd_peur))
    app.add_handler(CommandHandler("alertes",            cmd_activer_alertes))
    app.add_handler(CommandHandler("desactiver_alertes", cmd_desactiver_alertes))

    # Nouvelles commandes TradeBot
    app.add_handler(CommandHandler("toptraders",    cmd_toptraders))
    app.add_handler(CommandHandler("tb_historique", cmd_tb_historique))
    app.add_handler(CommandHandler("tb_aide",       cmd_tb_aide))
    app.add_handler(CommandHandler("inspector",     cmd_inspector))
    # Shortcuts par asset
    app.add_handler(CommandHandler("start_btc",     lambda u,ctx: cmd_copy_start_asset(u,ctx,"BTC")))
    app.add_handler(CommandHandler("start_eth",     lambda u,ctx: cmd_copy_start_asset(u,ctx,"ETH")))
    app.add_handler(CommandHandler("start_hype",    lambda u,ctx: cmd_copy_start_asset(u,ctx,"HYPE")))
    app.add_handler(CommandHandler("stop_btc",      lambda u,ctx: cmd_copy_stop_asset_direct(u,ctx,"BTC")))
    app.add_handler(CommandHandler("stop_eth",      lambda u,ctx: cmd_copy_stop_asset_direct(u,ctx,"ETH")))
    app.add_handler(CommandHandler("stop_hype",     lambda u,ctx: cmd_copy_stop_asset_direct(u,ctx,"HYPE")))
    app.add_handler(CommandHandler("copy_start",    cmd_copy_start))
    app.add_handler(CommandHandler("copy_sync",     cmd_copy_sync))
    app.add_handler(CommandHandler("copy_status",   cmd_copy_status))
    app.add_handler(CommandHandler("copy_stop",     cmd_copy_stop_asset))
    app.add_handler(CommandHandler("copy_close",    cmd_copy_close_all))

    logger.info("🤖 SakaiBot demarre avec module TradeBot!")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=False,
    )


if __name__ == "__main__":
    main()
