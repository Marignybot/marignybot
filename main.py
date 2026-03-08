#!/usr/bin/env python3
"""
SakaiBot v4.0 — Bot Telegram Hyperliquid
Nouvelle logique : Top 5 traders multi-asset (plus de spécialiste par asset)
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
# CONFIGURATION
# ============================================================

TELEGRAM_TOKEN     = "8413363300:AAEldjYE3nqAoF9-tZdYurwH1PNfUWJbZEQ"
HYPERLIQUID_ADDRESS = "0x6e89b986FBB4B985AcCC9B3CfEE4c7B5301D9a5C"
AUTHORIZED_USER_ID = 1429797974

# ============================================================
# COPY TRADING — SakaiBot
# ============================================================
COPY_BOT_ADDRESS = "0xd849f8E96d7BE1A1fc7CA5291Dc6603a47dF8dFD"
HL_PRIVATE_KEY   = os.getenv("HL_PRIVATE_KEY", "")
COPY_CAPITAL     = 1000.0
COPY_ASSETS      = ["BTC", "ETH", "HYPE", "SOL", "TAO"]
COPY_ALLOC       = 100.0
COPY_MAX_SIZE    = 50.0
MARGIN_SAFETY    = 0.5

COPY_LEVERAGE = {
    "BTC":  5,
    "ETH":  4,
    "HYPE": 3,
    "SOL":  5,
    "TAO":  3,
}

copy_deployed = {asset: 0.0 for asset in COPY_ASSETS}

# ============================================================
# copy_state v4 — structure multi-traders
# ============================================================
copy_state = {
    "active":      False,
    "traders":     {},      # {address: {"rank": int, "score": float, "assets_seen": set()}}
    "positions":   {},      # {asset: {"side", "size", "entry", "trader_addr"}}
    "ws_tasks":    {},      # {address: asyncio.Task}
    "last_update": {},
    "total_pnl":   0.0,
    "trades_log":  [],
    "last_ranked": [],
}

HYPERLIQUID_API           = "https://api.hyperliquid.xyz/info"
WATCHED_TOKENS            = ["BTC", "ETH", "SOL", "HYPE", "TAO"]
ALERT_THRESHOLD_PERCENT   = 5.0
LIQUIDATION_ALERT_PERCENT = 15.0
MORNING_HOUR_UTC          = 8
MORNING_MIN_UTC           = 0
EVENING_HOUR_UTC          = 20
EVENING_MIN_UTC           = 0

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
        result      = {}
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
                "usd":            usd,
                "eur":            usd * 0.92,
                "usd_24h_change": round(change, 2),
                "volume":         float(ctx.get("dayNtlVlm", 0) or 0)
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
                headers={"User-Agent": "SakaiBot/4.0"},
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
        balance = {"accountValue": av, "totalMarginUsed": mu, "totalUnrealizedPnl": pnl}
        return positions, balance
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
# MODULE TRADEBOT — ETF SYNTHETIQUE HYPERLIQUID
# ============================================================

tradebot_history = []

# ============================================================
# SCORING v4 — Formule : (PnL_7j / MDD) × WinRate_7j × log(n_trades_7j)
# Fenêtre principale : 7j | Confirmation : 30j
# Minimum : 5 trades sur 7j OU ratio PnL/MDD excellent (≥ 5.0)
# Bonus 30j : ×1.2 si PnL_30j > 0 ET ROI_30j ≥ 20%
# ============================================================

TRADEBOT_MIN_TRADES      = 5
TRADEBOT_MAX_DRAWDOWN    = 60.0
TRADEBOT_EXCELLENT_RATIO = 5.0   # PnL_7j / MDD ≥ 5 → éligible même avec < 5 trades

# Adresses bannies — jamais sélectionnées ni copiées
TRADER_BLACKLIST = {
    "0x9cd0a696c7cbb9d44de99268194cb08e5684e5fe",
}


def compute_new_score(pnl_7j: float, mdd: float, winrate_7j: float,
                      n_trades_7j: int, pnl_30j: float, roi_30j: float) -> float:
    """
    Score principal = (PnL_7j / MDD) × (WinRate_7j / 100) × log1p(n_trades_7j)
    Normalisé sur 100 par rapport au score max observé dans le batch.
    Bonus 30j appliqué ici directement (×1.2 si confirmation positive).
    Retourne le score brut (normalisation faite dans rank_and_score_traders).
    """
    import math

    # Eligibilité : 5 trades min OU ratio excellent
    pnl_mdd_ratio = pnl_7j / max(mdd, 1.0)
    if n_trades_7j < TRADEBOT_MIN_TRADES and pnl_mdd_ratio < TRADEBOT_EXCELLENT_RATIO:
        return 0.0

    # Score brut
    raw = pnl_mdd_ratio * (winrate_7j / 100.0) * math.log1p(n_trades_7j)

    # Bonus confirmation 30j
    if pnl_30j > 0 and roi_30j >= 20.0:
        raw *= 1.2

    return max(raw, 0.0)


# Gardées pour rétrocompatibilité avec cmd_inspector
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


async def fetch_top_traders_hl() -> list:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard",
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json()

        raw_rows = data.get("leaderboardRows", []) if isinstance(data, dict) else data
        logger.info(f"Leaderboard: {len(raw_rows)} traders")

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
                if address.lower() in TRADER_BLACKLIST:
                    continue

                metrics = {}
                for w in stats_dict.get("windowPerformances", []):
                    if isinstance(w, list) and len(w) == 2:
                        metrics[w[0]] = w[1]

                m30 = metrics.get("month", {})
                mat = metrics.get("allTime", {})

                roi_30d = float(m30.get("roi", 0) or 0) * 100
                pnl_30d = float(m30.get("pnl", 0) or 0)
                pnl_at  = float(mat.get("pnl", 0) or 0)

                if not (20 <= roi_30d <= 10000):
                    continue
                if pnl_30d < 5000:
                    continue
                if pnl_at < 10000:
                    continue

                candidates.append((address, roi_30d, pnl_30d))

            except Exception:
                continue

        candidates.sort(key=lambda x: x[1], reverse=True)
        top_candidates = candidates[:200]
        logger.info(f"{len(candidates)} candidats qualifiés | top 200 retenus")

        async def fetch_portfolio(session, address):
            import math
            try:
                async with session.post(
                    "https://api-ui.hyperliquid.xyz/info",
                    json={"type": "portfolio", "user": address},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    portfolio = await resp.json()

                windows = {}
                for item in portfolio:
                    if isinstance(item, list) and len(item) == 2:
                        windows[item[0]] = item[1]

                now_ms    = datetime.now().timestamp() * 1000
                cutoff_ms = now_ms - (15 * 86400 * 1000)

                # ── Activité récente (15j) ──────────────────────────────
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
                    return None

                # ── Données 7j (fenêtre principale) ────────────────────
                w7        = windows.get("perpWeek") or windows.get("week", {})
                pnl_h7    = [float(p[1]) for p in w7.get("pnlHistory", []) if isinstance(p, list)]
                acv_h7    = [float(p[1]) for p in w7.get("accountValueHistory", []) if isinstance(p, list) and float(p[1]) > 0]

                pnl_7j    = pnl_h7[-1] if pnl_h7 else 0.0

                # Win rate 7j : % de snapshots journaliers en hausse (proxy trades gagnants)
                wins_7j   = sum(1 for i in range(1, len(pnl_h7)) if pnl_h7[i] > pnl_h7[i-1])
                n_trades_7j = max(len(pnl_h7) - 1, 0)   # nb de deltas = proxy nb jours actifs
                winrate_7j  = (wins_7j / max(n_trades_7j, 1)) * 100

                # Récupérer aussi le nb de trades réels via userFills (7j)
                try:
                    cutoff_7j = now_ms - (7 * 86400 * 1000)
                    async with session.post(
                        "https://api-ui.hyperliquid.xyz/info",
                        json={"type": "userFills", "user": address},
                        timeout=aiohttp.ClientTimeout(total=8)
                    ) as resp2:
                        fills = await resp2.json()
                    if isinstance(fills, list):
                        # Trades fermés sur 7j (closedPnl != 0 = trade terminé)
                        fills_7j    = [f for f in fills if isinstance(f, dict)
                                       and f.get("time", 0) >= cutoff_7j
                                       and float(f.get("closedPnl", 0) or 0) != 0]
                        n_trades_7j = max(len(fills_7j), n_trades_7j)
                        if fills_7j:
                            wins_fills  = sum(1 for f in fills_7j if float(f.get("closedPnl", 0)) > 0)
                            winrate_7j  = (wins_fills / len(fills_7j)) * 100
                except Exception:
                    pass  # Fallback sur proxy pnlHistory

                # ── MDD — pire des deux fenêtres (7j et allTime) ───────
                def calc_mdd(hist):
                    if not hist: return 0.0
                    pk, mdd = hist[0], 0.0
                    for v in hist:
                        if v > pk: pk = v
                        dd = (pk - v) / pk * 100 if pk > 0 else 0
                        if dd > mdd: mdd = dd
                    return mdd

                mdd_7j  = calc_mdd(acv_h7)
                at_data = windows.get("perpAllTime") or windows.get("allTime", {})
                acv_hat = [float(p[1]) for p in at_data.get("accountValueHistory", []) if isinstance(p, list) and float(p[1]) > 0]
                mdd_at  = calc_mdd(acv_hat)
                worst_mdd = max(mdd_7j, mdd_at)
                if worst_mdd > TRADEBOT_MAX_DRAWDOWN:
                    return None

                # ── Données 30j (confirmation) ──────────────────────────
                m30        = windows.get("perpMonth") or windows.get("month", {})
                pnl_h30    = [float(p[1]) for p in m30.get("pnlHistory", []) if isinstance(p, list)]
                acv_h30    = [float(p[1]) for p in m30.get("accountValueHistory", []) if isinstance(p, list) and float(p[1]) > 0]

                pnl_30j    = pnl_h30[-1] if pnl_h30 else 0.0

                # Accepte un PnL 30j légèrement négatif (correction temporaire)
                # mais exclut si la perte dépasse 15% du capital
                cap_30     = acv_h30[-1] if acv_h30 else 0.0
                if cap_30 < 10000:
                    return None
                if pnl_30j < 0:
                    perte_pct = abs(pnl_30j) / max(cap_30, 1) * 100
                    if perte_pct > 15.0:
                        return None

                base_30    = max(cap_30 - pnl_30j, 1)
                roi_30j    = min((pnl_30j / base_30) * 100, 2000)

                # allTime
                pnl_hat    = [float(p[1]) for p in at_data.get("pnlHistory", []) if isinstance(p, list)]
                pnl_at     = pnl_hat[-1] if pnl_hat else 0
                peak_at    = max(acv_hat) if acv_hat else 1
                roe_at     = min((pnl_at / max(peak_at, 1)) * 100, 9999)

                # ── Ancienneté minimale : 3 mois d'activité ────────────
                # On vérifie via le premier timestamp de pnlHistory allTime
                at_pnl_raw = [p for p in at_data.get("pnlHistory", []) if isinstance(p, list) and len(p) == 2]
                if at_pnl_raw:
                    first_ts_ms  = at_pnl_raw[0][0]
                    age_days     = (now_ms - first_ts_ms) / (86400 * 1000)
                    if age_days < 90:
                        logger.debug(f"Exclu {address[:12]}: ancienneté {age_days:.0f}j < 90j")
                        return None
                else:
                    # Pas d'historique allTime → impossible de vérifier → on exclut
                    return None

                # ── Eligibilité minimum ─────────────────────────────────
                pnl_mdd_ratio = pnl_7j / max(worst_mdd, 1.0)
                if n_trades_7j < TRADEBOT_MIN_TRADES and pnl_mdd_ratio < TRADEBOT_EXCELLENT_RATIO:
                    logger.debug(f"Exclu {address[:12]}: {n_trades_7j} trades, ratio {pnl_mdd_ratio:.1f}")
                    return None

                # Win rate : plus de filtre dur — composante du score uniquement

                return {
                    "address":     address,
                    "pnl":         pnl_30j,          # affiché dans le rapport
                    "pnl_7j":      round(pnl_7j, 2),
                    "pnl_at":      pnl_at,
                    "roi":         round(roi_30j, 1),
                    "roe_at":      round(roe_at, 1),
                    "mdd":         round(worst_mdd, 1),
                    "winrate":     round(winrate_7j, 1),
                    "winrate_30j": round((sum(1 for i in range(1, len(pnl_h30)) if pnl_h30[i] > pnl_h30[i-1]) / max(len(pnl_h30)-1, 1)) * 100, 1),
                    "n_trades_7j": n_trades_7j,
                    "roi_30j":     round(roi_30j, 1),
                }
            except Exception as e:
                logger.warning(f"Portfolio {address[:12]}... erreur: {e}")
                return None

        async with aiohttp.ClientSession() as session:
            tasks   = [fetch_portfolio(session, addr) for addr, _, _ in top_candidates]
            results = await asyncio.gather(*tasks)

        traders = [r for r in results if r is not None]
        logger.info(f"Traders qualifiés après filtres: {len(traders)}")
        return traders

    except Exception as e:
        logger.error(f"Erreur fetch_top_traders: {e}")
        return []


def apply_exclusion_filters(traders: list) -> list:
    """Filtre de sécurité post-fetch : MDD ≤ 60% et PnL allTime positif."""
    filtered = [
        t for t in traders
        if t["mdd"] <= TRADEBOT_MAX_DRAWDOWN and t.get("pnl_at", 0) > 0
    ]
    logger.info(f"Après filtres: {len(filtered)}/{len(traders)} traders retenus")
    return filtered


def rank_and_score_traders(traders: list) -> list:
    """
    Calcule le score final normalisé sur 100.
    Formule : (PnL_7j / MDD) × (WinRate_7j / 100) × log1p(n_trades_7j) × bonus_30j
    Normalisé par rapport au score max du batch.
    """
    if not traders:
        return []

    raw_scores = []
    for t in traders:
        raw = compute_new_score(
            pnl_7j      = t.get("pnl_7j", 0),
            mdd         = t.get("mdd", 1),
            winrate_7j  = t.get("winrate", 50),
            n_trades_7j = t.get("n_trades_7j", 0),
            pnl_30j     = t.get("pnl", 0),
            roi_30j     = t.get("roi_30j", t.get("roi", 0)),
        )
        raw_scores.append(raw)

    max_raw = max(raw_scores) if raw_scores else 1.0
    if max_raw == 0:
        max_raw = 1.0

    scored = []
    for t, raw in zip(traders, raw_scores):
        normalized = round((raw / max_raw) * 100, 1)
        scored.append({**t, "score": normalized, "score_raw": round(raw, 4)})

    return sorted(scored, key=lambda x: x["score"], reverse=True)


def build_top5_report(top5: list) -> str:
    now   = datetime.now().strftime("%d/%m/%Y %H:%M")
    lines = [
        "🏆 *SakaiBot — Top 5 Traders*",
        f"📅 {now}",
        "━━━━━━━━━━━━━━━━━━━━",
        "*📊 Score = (PnL_7j/MDD) × WR_7j × log(trades) × bonus_30j*",
        "",
    ]
    for i, t in enumerate(top5, 1):
        verdict = "🟢 FORT" if t["score"] >= 70 else ("🟡 MOYEN" if t["score"] >= 40 else "🔴 FAIBLE")
        bonus   = " ✨+30j" if t.get("roi_30j", 0) >= 20 and t.get("pnl", 0) > 0 else ""
        lines.append(f"*#{i}* — Score: *{t['score']}/100* {verdict}{bonus}")
        lines.append(f"PnL 7j: ${t.get('pnl_7j',0):+,.0f} | WR 7j: {t.get('winrate',0):.0f}% | Trades 7j: {t.get('n_trades_7j',0)}")
        lines.append(f"MDD: {t['mdd']:.0f}% | PnL 30j: ${t.get('pnl',0):+,.0f} | ROI 30j: {t.get('roi',0):.0f}%")
        lines.append(f"`{t['address']}`")
        lines.append("")
    avg = round(sum(t["score"] for t in top5) / len(top5), 1) if top5 else 0
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"Score moyen: *{avg}/100*")
    lines.append(f"_Ces 5 traders copiés sur tous leurs assets_")
    return "\n".join(lines)


def build_history_report() -> str:
    if not tradebot_history:
        return (
            "📚 *Historique TradeBot*\n\n"
            "_Aucune selection anterieure enregistree._\n"
            "Lance /toptraders pour demarrer ta premiere analyse."
        )
    lines = ["📚 *Historique des Selections TradeBot*\n"]
    for session in tradebot_history[-5:]:
        lines.append(f"📅 *{session['date']}* — Score moyen: {session['avg_score']}/100")
        for t in session["traders"]:
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
        "🔍 *TradeBot — Analyse en cours...*\n\n"
        "• Récupération du leaderboard Hyperliquid (top 200)\n"
        "• Application des filtres: MDD<40% | WR≥60% | actif 15j\n"
        "• Calcul des scores composites\n"
        "• Sélection du Top 5 multi-asset\n\n"
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

    filtered = apply_exclusion_filters(raw_traders)
    if not filtered:
        await update.message.reply_text(
            "⚠️ Aucun trader ne passe les filtres.\nDonnées peut-être partielles.",
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

    # Stocker pour copy_start
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
                "https://api-ui.hyperliquid.xyz/info",
                json={"type": "portfolio", "user": address},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                portfolio = await resp.json()

            async with session.post(
                "https://api-ui.hyperliquid.xyz/info",
                json={"type": "userFills", "user": address},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp2:
                fills = await resp2.json()

            async with session.post(
                "https://api-ui.hyperliquid.xyz/info",
                json={"type": "clearinghouseState", "user": address},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp3:
                state = await resp3.json()

        windows = {}
        if not portfolio or not isinstance(portfolio, list):
            await update.message.reply_text("❌ Données portfolio indisponibles pour ce wallet.", parse_mode="Markdown")
            return
        for item in portfolio:
            if isinstance(item, list) and len(item) == 2:
                windows[item[0]] = item[1]

        def get_window_stats(win_key, fallback_key):
            w        = windows.get(win_key) or windows.get(fallback_key, {})
            pnl_hist = [float(p[1]) for p in w.get("pnlHistory", []) if isinstance(p, list)]
            acv_hist = [float(p[1]) for p in w.get("accountValueHistory", []) if isinstance(p, list) and float(p[1]) > 0]
            pnl      = pnl_hist[-1] if pnl_hist else 0
            capital  = acv_hist[-1] if acv_hist else 0
            mdd      = 0.0
            if acv_hist:
                pk = acv_hist[0]
                for v in acv_hist:
                    if v > pk: pk = v
                    dd = (pk - v) / pk * 100 if pk > 0 else 0
                    if dd > mdd: mdd = dd
            base = max(capital - pnl, 1)
            roi  = min((pnl / base) * 100, 9999) if pnl > 0 else (pnl / base) * 100
            pos  = sum(1 for i in range(1, len(pnl_hist)) if pnl_hist[i] > pnl_hist[i-1])
            consist = (pos / max(len(pnl_hist) - 1, 1)) * 100
            return {"pnl": pnl, "capital": capital, "mdd": mdd, "roi": roi, "consist": consist}

        d1  = get_window_stats("perpDay",    "day")
        w7  = get_window_stats("perpWeek",   "week")
        m30 = get_window_stats("perpMonth",  "month")
        at  = get_window_stats("perpAllTime","allTime")

        week_data = windows.get("perpWeek") or windows.get("week", {})
        week_pnl  = [float(p[1]) for p in week_data.get("pnlHistory", []) if isinstance(p, list)]
        winrate   = (sum(1 for p in week_pnl if p > 0) / max(len(week_pnl), 1)) * 100

        now_ms       = datetime.now().timestamp() * 1000
        last_fill_ts = None
        if isinstance(fills, list) and fills:
            last_fill_ts = max((f.get("time", 0) for f in fills if isinstance(f, dict)), default=None)
        days_inactive = ((now_ms - last_fill_ts) / 86400000) if last_fill_ts else None

        asset_pnl = {}
        COIN_MAP  = {"BTC": "BTC", "ETH": "ETH", "SOL": "SOL", "HYPE": "HYPE", "TAO": "TAO"}
        if isinstance(fills, list):
            for fill in fills:
                if not isinstance(fill, dict): continue
                coin = fill.get("coin", "")
                coin = COIN_MAP.get(coin, coin)
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

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Inspector erreur: {e}")
        await update.message.reply_text(f"❌ Erreur lors de l'analyse: `{str(e)[:100]}`", parse_mode="Markdown")


# ============================================================
# MODULE COPY TRADING — Top 5 Multi-Asset
# ============================================================

async def send_copy_notification(app, message: str):
    try:
        await app.bot.send_message(
            chat_id=AUTHORIZED_USER_ID,
            text=message,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Notification copy trading erreur: {e}")


async def place_order(asset: str, is_buy: bool, size: float, reason: str = "", leverage: int = 1) -> dict:
    try:
        if not HL_PRIVATE_KEY:
            logger.error("HL_PRIVATE_KEY non définie")
            return {"error": "Clé privée manquante"}

        import eth_account
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils import constants

        key      = HL_PRIVATE_KEY if HL_PRIVATE_KEY.startswith("0x") else "0x" + HL_PRIVATE_KEY
        wallet   = eth_account.Account.from_key(key)
        exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=COPY_BOT_ADDRESS)

        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API,
                json={"type": "allMids"},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                mids = await resp.json()

        price = float(mids.get(asset, 0))
        if price <= 0:
            return {"error": f"Prix {asset} introuvable"}

        raw_px    = price * (1.005 if is_buy else 0.995)
        magnitude = len(str(int(raw_px)))
        decimals  = max(0, 5 - magnitude)
        limit_px  = float(round(raw_px, decimals))

        SIZE_RULES = {
            "BTC":  {"min": 0.001,  "decimals": 3},
            "ETH":  {"min": 0.01,   "decimals": 2},
            "SOL":  {"min": 0.1,    "decimals": 1},
            "HYPE": {"min": 1.0,    "decimals": 0},
            "TAO":  {"min": 0.01,   "decimals": 2},
        }
        rules = SIZE_RULES.get(asset, {"min": 0.001, "decimals": 3})
        sz    = round(size, rules["decimals"])
        sz    = max(sz, rules["min"])
        if sz * price < 10:
            sz = max(round(11.0 / price, rules["decimals"]), rules["min"])
        logger.info(f"Ordre {asset} size={sz} prix={price} ~${sz*price:.1f}")

        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: exchange.order(asset, is_buy, sz, limit_px, {"limit": {"tif": "Ioc"}})
        )
        logger.info(f"Ordre {asset} {'BUY' if is_buy else 'SELL'} {sz} → {result}")
        return result

    except Exception as e:
        logger.error(f"Erreur place_order {asset}: {e}")
        return {"error": str(e)}


async def get_my_positions() -> dict:
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


async def get_proportional_size(asset: str, trader_addr: str, trader_pos_value: float, current_price: float) -> tuple:
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

        leverage = 1
        for pos in trader_state.get("assetPositions", []):
            p = pos.get("position", {})
            if p.get("coin") == asset:
                leverage = int(p.get("leverage", {}).get("value", 1) or 1)
                break

        _, bot_balance = await get_wallet_data(COPY_BOT_ADDRESS)
        my_capital     = bot_balance.get("accountValue", 0)
        if my_capital <= 0:
            my_capital = 1000.0

        ratio        = trader_pos_value / max(trader_capital, 1)
        my_usd_value = (ratio / 5) * my_capital

        SIZE_RULES = {
            "BTC":  {"min": 0.001, "decimals": 3, "min_usd": 11.0},
            "ETH":  {"min": 0.01,  "decimals": 2, "min_usd": 11.0},
            "SOL":  {"min": 0.1,   "decimals": 1, "min_usd": 11.0},
            "HYPE": {"min": 1.0,   "decimals": 0, "min_usd": 35.0},
            "TAO":  {"min": 0.01,  "decimals": 2, "min_usd": 15.0},
        }
        rules        = SIZE_RULES.get(asset, {"min": 0.001, "decimals": 3, "min_usd": 11.0})
        my_usd_value = max(my_usd_value, rules["min_usd"])
        my_size      = max(round(my_usd_value / current_price, rules["decimals"]), rules["min"])

        logger.info(
            f"Proportionnel {asset}: trader_cap=${trader_capital:.0f} "
            f"pos=${trader_pos_value:.0f} ratio={ratio*100:.1f}% "
            f"mon_cap=${my_capital:.0f} → ${my_usd_value:.0f} sz={my_size}"
        )
        return my_size, leverage, my_usd_value

    except Exception as e:
        logger.warning(f"get_proportional_size erreur: {e}")
        SIZE_RULES = {
            "BTC":  {"min": 0.001, "decimals": 3},
            "ETH":  {"min": 0.01,  "decimals": 2},
            "SOL":  {"min": 0.1,   "decimals": 1},
            "HYPE": {"min": 1.0,   "decimals": 0},
            "TAO":  {"min": 0.01,  "decimals": 2},
        }
        rules         = SIZE_RULES.get(asset, {"min": 0.001, "decimals": 3})
        fallback_size = max(round(50.0 / max(current_price, 0.01), rules["decimals"]), rules["min"])
        return fallback_size, 1, 50.0


# ============================================================
# COPY TRADING — start / stop / watch
# ============================================================

async def start_copy_trading(top_traders: list, app) -> None:
    """
    Lance 1 WebSocket par trader (top 5).
    Chaque trader est surveillé sur TOUS ses assets — pas de filtre par asset.
    """
    # Stopper les anciens WS
    for addr, task in list(copy_state["ws_tasks"].items()):
        task.cancel()
        logger.info(f"WebSocket {addr[:12]} arrêté (relance)")
    copy_state["ws_tasks"] = {}
    copy_state["traders"]  = {}
    copy_state["active"]   = True

    active_addrs = []
    notif_lines  = ["🤖 *SakaiBot — Copy Trading v4*", "_Top 5 multi-asset_", ""]

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

    notif_lines.append("")
    notif_lines.append(f"📡 {len(active_addrs)} traders | tous assets | taille proportionnelle")
    notif_lines.append(f"_Scoring: (PnL_7j/MDD) × WR × log(trades) × bonus30j_")
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
    """WebSocket — surveille TOUS les trades d'un trader, sans filtre par asset."""
    import websockets
    import json as json_mod

    ws_url = "wss://api.hyperliquid.xyz/ws"
    logger.info(f"WebSocket multi-asset → {trader_address[:12]}...")

    while copy_state["active"]:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json_mod.dumps({
                    "method": "subscribe",
                    "subscription": {"type": "userEvents", "user": trader_address}
                }))
                logger.info(f"✅ WebSocket connecté → {trader_address[:12]}")

                async for raw_msg in ws:
                    if not copy_state["active"]:
                        break
                    try:
                        msg = json_mod.loads(raw_msg)
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
    """
    Traite TOUS les trades d'un trader.
    Gestion des conflits : si 2 traders ouvrent le même asset,
    le 2ème signal est ignoré (premier arrivé, premier servi).
    """
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

        # Enregistrer les assets vus par ce trader
        if trader_info := copy_state["traders"].get(trader_address):
            trader_info["assets_seen"].add(asset)

        my_size  = 0.0
        leverage = 1
        my_usd   = 0.0

        if is_opening:
            # Conflit : un autre trader a déjà une position ouverte sur cet asset ?
            existing = copy_state["positions"].get(asset)
            if existing and existing.get("trader_addr") != trader_address:
                rank = copy_state["traders"].get(trader_address, {}).get("rank", "?")
                logger.info(f"Conflit {asset}: signal #{rank} ignoré (déjà ouvert par trader #{copy_state['traders'].get(existing['trader_addr'],{}).get('rank','?')})")
                await send_copy_notification(app,
                    f"ℹ️ *Signal ignoré — conflit*\n"
                    f"Asset: `{asset}` | Trader #{rank}\n"
                    f"Déjà une position ouverte sur cet asset."
                )
                continue

            if not await check_margin_ok():
                await send_copy_notification(app,
                    f"⚠️ *Marge insuffisante*\n"
                    f"Asset: {asset} | Action: {dir_fill}\n"
                    f"Ordre annulé."
                )
                continue

            pos_value             = size * price
            my_size, leverage, my_usd = await get_proportional_size(asset, trader_address, pos_value, price)
            result                = await place_order(asset, is_buy, my_size, dir_fill, leverage)

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

            # Vérifier que c'est le trader qui a ouvert qui ferme
            tracked = copy_state["positions"].get(asset, {})
            if tracked.get("trader_addr") and tracked["trader_addr"] != trader_address:
                logger.info(f"Fermeture {asset} ignorée — signal d'un autre trader")
                continue

            my_size = my_pos["size"]
            is_buy  = my_pos["side"] == "short"
            result  = await place_order(asset, is_buy, my_size, dir_fill, 1)

            copy_state["positions"].pop(asset, None)
            copy_deployed[asset] = 0.0

        else:
            continue

        # Log + notification
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
            "_Scoring: (PnL_7j/MDD) × WR_7j × log(trades) + bonus 30j_",
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
            tracked      = copy_state["positions"].get(asset, {})
            trader_addr  = tracked.get("trader_addr", "")
            trader_rank  = copy_state["traders"].get(trader_addr, {}).get("rank", "?")
            emoji        = "📈" if pos["side"] == "long" else "📉"
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
        result = await place_order(asset, is_buy, pos["size"], "Close All")
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
    msg = (
        "🤖 *SakaiBot — Module Copy Trading v4*\n\n"
        "Sélectionne les *5 meilleurs traders globaux* (top 200 Hyperliquid)\n"
        "et copie TOUS leurs trades, sur TOUS leurs assets.\n\n"
        "📐 *Scoring composite:*\n"
        "   🥇 Consistance 30j :  35%\n"
        "   🥈 Drawdown max :     30%\n"
        "   🥉 Win Rate hebdo :   20%\n"
        "   4️⃣ ROI 30j :          15%\n\n"
        "🚫 *Filtres d'exclusion:*\n"
        "   • MDD > 40% (30j ou allTime)\n"
        "   • Win Rate < 60%\n"
        "   • Inactif depuis > 15 jours\n"
        "   • Capital < $10 000\n"
        "   • PnL 30j < $5 000\n\n"
        "⚡ *Logique de copie:*\n"
        "   • 1 WebSocket par trader (5 total)\n"
        "   • Tous assets copiés (BTC, ETH, SOL, HYPE, TAO...)\n"
        "   • Taille proportionnelle à ton capital\n"
        "   • Conflit: si 2 traders ouvrent le même asset\n"
        "     → le 2ème signal est ignoré\n\n"
        "📋 *Commandes:*\n"
        "   /toptraders — Analyser + sélectionner le Top 5\n"
        "   /copy\\_start — Démarrer la copie\n"
        "   /copy\\_stop — Arrêter la copie\n"
        "   /copy\\_status — Statut en temps réel\n"
        "   /copy\\_close — Fermer toutes les positions\n"
        "   /inspector — Analyser un wallet manuellement\n"
        "   /tb\\_historique — Sélections passées\n\n"
        "_v4.0 — Multi-asset, Top 5 global_"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ============================================================
# COMMANDES PRINCIPALES
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    msg = (
        "👋 *Bienvenue sur SakaiBot v4! 🤖*\n\n"
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
        "   /target 0x... — Copier un wallet spécifique\n"
        "   /target\\_pause — Mettre en pause\n"
        "   /target\\_stop — Arrêter\n"
        "   /target\\_status — Statut\n\n"
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
    await update.message.reply_text("⏳ Analyse technique en cours (RSI, EMA, S/R, Fibo, Volume)...", parse_mode="Markdown")
    prices, trending = await asyncio.gather(get_crypto_prices(), get_crypto_trending())
    msg = await analyze_setup(prices, trending)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text("⏳ Préparation du résumé...", parse_mode="Markdown")
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
# MODULE TARGET WALLET MANUEL
# ============================================================

target_state = {
    "active":     False,
    "paused":     False,
    "address":    None,
    "ws_task":    None,
    "positions":  {},
    "trades_log": [],
}


async def target_sync_positions(address: str, app) -> None:
    logger.info(f"Target sync — {address[:12]}...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API,
                json={"type": "clearinghouseState", "user": address},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                state = await resp.json()

            async with session.post(
                HYPERLIQUID_API,
                json={"type": "allMids"},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp2:
                mids = await resp2.json()

        positions = [
            p.get("position", {}) for p in state.get("assetPositions", [])
            if float(p.get("position", {}).get("szi", 0) or 0) != 0
        ]

        if not positions:
            await send_copy_notification(app,
                f"🎯 *Target Wallet actif*\n`{address[:20]}...`\nAucune position ouverte — surveillance active."
            )
            return

        results = []
        for p in positions:
            asset  = p.get("coin", "")
            sz     = float(p.get("szi", 0) or 0)
            is_buy = sz > 0
            price  = float(mids.get(asset, 0))
            if price <= 0:
                results.append(f"⚠️ {asset}: prix introuvable")
                continue

            pos_value        = abs(sz) * price
            my_size, lev, my_usd = await get_proportional_size(asset, address, pos_value, price)
            result           = await place_order(asset, is_buy, my_size, "Target Sync", lev)
            status           = "✅" if "error" not in result else "❌"
            side_str         = "Long 📈" if is_buy else "Short 📉"
            results.append(f"{status} {asset} {side_str} x{lev} ~${my_usd:.0f}")

            target_state["positions"][asset] = {
                "side": "long" if is_buy else "short",
                "size": my_size, "entry": price,
            }

        await send_copy_notification(app,
            f"🎯 *Target Wallet — Sync*\n`{address[:20]}...`\n\n" + "\n".join(results)
        )

    except Exception as e:
        logger.error(f"target_sync erreur: {e}")
        await send_copy_notification(app, f"❌ Target sync erreur: `{str(e)[:100]}`")


async def target_watch_ws(address: str, app) -> None:
    import websockets
    import json as json_mod

    ws_url = "wss://api.hyperliquid.xyz/ws"
    logger.info(f"Target WebSocket → {address[:12]}...")

    while target_state["active"]:
        if target_state["paused"]:
            await asyncio.sleep(5)
            continue
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json_mod.dumps({
                    "method": "subscribe",
                    "subscription": {"type": "userEvents", "user": address}
                }))
                logger.info(f"✅ Target WebSocket connecté → {address[:12]}")

                async for raw_msg in ws:
                    if not target_state["active"]:
                        break
                    if target_state["paused"]:
                        continue
                    try:
                        msg   = json_mod.loads(raw_msg)
                        data  = msg.get("data", {})
                        fills = data.get("fills", [])

                        for fill in fills:
                            asset    = fill.get("coin", "")
                            side     = fill.get("side", "")
                            size     = float(fill.get("sz", 0) or 0)
                            price    = float(fill.get("px", 0) or 0)
                            dir_fill = fill.get("dir", "")

                            if size <= 0 or price <= 0:
                                continue

                            is_opening = "Open" in dir_fill
                            is_closing = "Close" in dir_fill
                            is_buy     = side == "B"

                            if is_opening:
                                pos_value        = size * price
                                my_size, lev, my_usd = await get_proportional_size(asset, address, pos_value, price)
                                result           = await place_order(asset, is_buy, my_size, dir_fill, lev)
                                target_state["positions"][asset] = {
                                    "side": "long" if is_buy else "short",
                                    "size": my_size, "entry": price, "usd": my_usd,
                                }
                            elif is_closing:
                                my_pos = target_state["positions"].get(asset)
                                if not my_pos:
                                    continue
                                my_size = my_pos["size"]
                                is_buy  = my_pos["side"] == "short"
                                my_usd  = 0.0
                                result  = await place_order(asset, is_buy, my_size, dir_fill, 1)
                                target_state["positions"].pop(asset, None)
                            else:
                                continue

                            status = "✅" if "error" not in result else "❌"
                            emoji  = "📈" if is_buy else "📉"
                            log    = {
                                "time": datetime.now().strftime("%d/%m %H:%M"),
                                "asset": asset, "dir": dir_fill,
                                "size": my_size, "price": price,
                                "result": "ok" if "error" not in result else "erreur",
                            }
                            target_state["trades_log"].append(log)
                            if len(target_state["trades_log"]) > 100:
                                target_state["trades_log"] = target_state["trades_log"][-100:]

                            notif = (
                                f"{status} *Target Copy {emoji}*\n"
                                f"Asset:  *{asset}*\n"
                                f"Action: {dir_fill}\n"
                                f"Taille: {my_size} (~${my_usd:.0f})\n"
                                f"Prix:   ${price:,.2f}\n"
                                f"Wallet: `{address[:16]}...`"
                            )
                            if "error" in result:
                                notif += f"\n⚠️ {result['error']}"
                            await send_copy_notification(app, notif)

                    except Exception as e:
                        logger.warning(f"Target WS parse erreur: {e}")

        except Exception as e:
            if not target_state["active"]:
                break
            logger.warning(f"Target WS déconnecté: {e} — reconnexion 5s")
            await asyncio.sleep(5)

    logger.info("Target WebSocket arrêté")


async def cmd_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: `/target 0xADRESSE`",
            parse_mode="Markdown"
        )
        return
    address = context.args[0].strip().lower()
    if not address.startswith("0x") or len(address) != 42:
        await update.message.reply_text("❌ Adresse invalide. Format: `0x...` (42 caractères)", parse_mode="Markdown")
        return
    if target_state["ws_task"] and not target_state["ws_task"].done():
        target_state["active"] = False
        target_state["ws_task"].cancel()
        await asyncio.sleep(1)
    target_state["address"]   = address
    target_state["active"]    = True
    target_state["paused"]    = False
    target_state["positions"] = {}
    await update.message.reply_text(
        f"🎯 *Target Wallet Manuel activé*\n`{address}`\n\nSynchronisation en cours...",
        parse_mode="Markdown"
    )
    await target_sync_positions(address, context.application)
    target_state["ws_task"] = asyncio.create_task(target_watch_ws(address, context.application))


async def cmd_target_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not target_state["active"]:
        await update.message.reply_text("⏸ Aucun target actif.")
        return
    target_state["paused"] = True
    await update.message.reply_text(
        f"⏸ *Target en pause*\n`{target_state['address'][:20]}...`\nLance `/target {target_state['address']}` pour reprendre.",
        parse_mode="Markdown"
    )


async def cmd_target_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not target_state["active"]:
        await update.message.reply_text("⏸ Aucun target actif.")
        return
    addr = target_state["address"]
    target_state["active"] = False
    target_state["paused"] = False
    if target_state["ws_task"] and not target_state["ws_task"].done():
        target_state["ws_task"].cancel()
    target_state.update({"ws_task": None, "address": None, "positions": {}})
    await update.message.reply_text(
        f"🛑 *Target arrêté*\n`{addr[:20]}...`\nPositions conservées — gère-les manuellement.",
        parse_mode="Markdown"
    )


async def cmd_target_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not target_state["address"]:
        await update.message.reply_text("⏸ *Target inactif*\nUtilise `/target 0x...` pour démarrer.", parse_mode="Markdown")
        return
    status_emoji = "⏸ EN PAUSE" if target_state["paused"] else ("🟢 ACTIF" if target_state["active"] else "🔴 INACTIF")
    lines = [
        f"🎯 *TARGET WALLET MANUEL*",
        f"Status: {status_emoji}",
        f"Wallet: `{target_state['address']}`",
        "",
        "📡 *Positions copiées:*",
    ]
    if target_state["positions"]:
        for asset, pos in target_state["positions"].items():
            emoji = "📈" if pos["side"] == "long" else "📉"
            lines.append(f"{emoji} {asset}: {pos['side'].upper()} sz:{pos['size']} @ ${pos['entry']:,.2f}")
    else:
        lines.append("_Aucune position active_")
    if target_state["trades_log"]:
        lines.append("")
        lines.append(f"📋 *Derniers trades ({min(3, len(target_state['trades_log']))}):*")
        for t in target_state["trades_log"][-3:]:
            s = "✅" if t["result"] == "ok" else "❌"
            lines.append(f"{s} {t['time']} | {t['asset']} {t['dir']} @ ${t['price']:,.0f}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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

    # Copy Trading v4 — simplifié
    app.add_handler(CommandHandler("copy_start",  cmd_copy_start))
    app.add_handler(CommandHandler("copy_stop",   cmd_copy_stop))
    app.add_handler(CommandHandler("copy_status", cmd_copy_status))
    app.add_handler(CommandHandler("copy_close",  cmd_copy_close_all))

    # Target Wallet Manuel
    app.add_handler(CommandHandler("target",        cmd_target))
    app.add_handler(CommandHandler("target_pause",  cmd_target_pause))
    app.add_handler(CommandHandler("target_stop",   cmd_target_stop))
    app.add_handler(CommandHandler("target_status", cmd_target_status))

    logger.info("🤖 SakaiBot v4.0 démarré — Top 5 multi-asset!")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=False,
    )


if __name__ == "__main__":
    main()
