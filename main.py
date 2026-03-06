#!/usr/bin/env python3
"""
MarignyCryptoBot - Bot Telegram pour suivi crypto & Hyperliquid
"""

import asyncio
import logging
import re
import time as time_module
from datetime import datetime, time
import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# CONFIGURATION
# ============================================================
TELEGRAM_TOKEN = "8413363300:AAEldjYE3nqAoF9-tZdYurwH1PNfUWJbZEQ"
HYPERLIQUID_ADDRESS = "0x6e89b986FBB4B985AcCC9B3CfEE4c7B5301D9a5C"
HYPERLIQUID_API = "https://api.hyperliquid.xyz/info"

WATCHED_TOKENS = ["BTC", "ETH", "HYPE"]

ALERT_THRESHOLD_PERCENT = 5.0
LIQUIDATION_ALERT_PERCENT = 15.0  # alerte si prix a moins de 15% du prix de liquidation
DAILY_SUMMARY_HOUR = 8
DAILY_SUMMARY_MIN = 0
AUTHORIZED_USER_ID = 1429797974

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

last_prices = {}

def is_authorized(update) -> bool:
    return update.effective_user.id == AUTHORIZED_USER_ID


# ============================================================
# HYPERLIQUID — PRIX EN TEMPS REEL
# ============================================================
async def get_crypto_prices() -> dict:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API,
                json={"type": "allMids"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                all_mids = await resp.json()

            async with session.post(
                HYPERLIQUID_API,
                json={"type": "metaAndAssetCtxs"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp2:
                meta_data = await resp2.json()

        result = {}
        universe = meta_data[0].get("universe", []) if isinstance(meta_data, list) else []
        ctxs     = meta_data[1] if isinstance(meta_data, list) and len(meta_data) > 1 else []
        ctx_by_name = {}
        for i, asset in enumerate(universe):
            name = asset.get("name", "")
            if i < len(ctxs):
                ctx_by_name[name] = ctxs[i]

        for symbol in WATCHED_TOKENS:
            mid = all_mids.get(symbol)
            if mid is None:
                continue
            usd  = float(mid)
            ctx  = ctx_by_name.get(symbol, {})
            prev = float(ctx.get("prevDayPx", usd) or usd)
            change = ((usd - prev) / prev * 100) if prev else 0
            volume = float(ctx.get("dayNtlVlm", 0) or 0)
            result[symbol] = {
                "usd": usd,
                "eur": usd * 0.92,
                "usd_24h_change": round(change, 2),
                "volume": volume,
            }
        return result
    except Exception as e:
        logger.error(f"Erreur Hyperliquid prix: {e}")
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
        closes  = [float(c["c"]) for c in candles]
        highs   = [float(c["h"]) for c in candles]
        lows    = [float(c["l"]) for c in candles]
        volumes = [float(c["v"]) for c in candles]
        return {"closes": closes, "highs": highs, "lows": lows, "volumes": volumes}
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
        tendance, conseil, t_emoji = "Possible rebond — chute importante", "Zone de support potentielle. Surveille un retournement haussier.", "🔥"
    elif change >= 8:
        tendance, conseil, t_emoji = "Momentum haussier fort", "Breakout en cours. Attention au retrace avant continuation.", "⚡"
    elif 3 < change < 8:
        tendance, conseil, t_emoji = "Tendance haussiere moderee", "Momentum positif. Attends confirmation au-dessus de la resistance.", "📈"
    elif -8 < change <= -3:
        tendance, conseil, t_emoji = "Tendance baissiere moderee", "Prudence. Surveille le support. Pas de signal fort.", "📉"
    else:
        tendance, conseil, t_emoji = "Consolidation / Range", "Range serre. Attends une cassure claire pour trader.", "⏸"

    lines.append(f"{t_emoji} *{symbol}* — {tendance}")
    lines.append(f"   Variation 24h: {change:+.2f}%")

    if ohlcv:
        closes  = ohlcv.get("closes", [])
        highs   = ohlcv.get("highs", [])
        lows    = ohlcv.get("lows", [])
        volumes = ohlcv.get("volumes", [])

        rsi = compute_rsi(closes)
        rsi_label = f"{rsi} — Surachat ⚠️" if rsi >= 70 else (f"{rsi} — Survendu 🛒" if rsi <= 30 else f"{rsi} — Neutre")
        lines.append(f"   RSI(14): {rsi_label}")

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
                lines.append(f"   🚨 CASSURE RESISTANCE IMMINENTE")
            elif cp <= sr['support'] * 1.01:
                lines.append(f"   🚨 TEST DU SUPPORT EN COURS")

        vol       = volume_signal(volumes)
        vol_emoji = "🔊" if vol == "fort" else ("🔇" if vol == "faible" else "📊")
        lines.append(f"   Volume: {vol} {vol_emoji}")
    else:
        lines.append(f"   _Donnees techniques indisponibles_")

    lines.append(f"   ➡️ {conseil}")
    return "\n".join(lines)


# ============================================================
# FEAR & GREED INDEX
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
                return {
                    "value":      int(item["value"]),
                    "label":      item["value_classification"],
                    "timestamp":  item["timestamp"]
                }
    except Exception as e:
        logger.error(f"Erreur Fear & Greed: {e}")
        return {}


def format_fear_greed(fg: dict) -> str:
    if not fg:
        return "❌ Impossible de recuperer le Fear & Greed Index."
    value = fg["value"]
    label = fg["label"]

    if value <= 20:
        emoji = "😱"
        comment = "Panique extreme — souvent une opportunite d'achat historique."
    elif value <= 40:
        emoji = "😨"
        comment = "Peur sur le marche — les mains faibles vendent."
    elif value <= 60:
        emoji = "😐"
        comment = "Marche neutre — pas de signal fort."
    elif value <= 80:
        emoji = "😏"
        comment = "Cupidite — attention aux retournements."
    else:
        emoji = "🤑"
        comment = "Cupidite extreme — le marche est euphorique, sois prudent."

    bar_filled = round(value / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    return (
        f"🧠 *Fear & Greed Index*\n\n"
        f"   {emoji} *{value}/100 — {label}*\n"
        f"   [{bar}]\n\n"
        f"   ➡️ {comment}\n\n"
        f"_Mis a jour: {datetime.now().strftime('%H:%M:%S')}_"
    )


# ============================================================
# ALERTES SUPPORT / RESISTANCE
# ============================================================
async def check_sr_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Verifie si BTC/ETH/HYPE cassent un support ou une resistance."""
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

        # Cassure resistance
        if current >= sr["resistance"] * 0.995:
            alerts.append(
                f"🚀 *{symbol} — CASSURE RESISTANCE*\n"
                f"   Prix: ${current:,.2f}\n"
                f"   Resistance: ${sr['resistance']:,.2f}\n"
                f"   ➡️ Breakout potentiel a la hausse !"
            )
        # Cassure support
        elif current <= sr["support"] * 1.005:
            alerts.append(
                f"💥 *{symbol} — CASSURE SUPPORT*\n"
                f"   Prix: ${current:,.2f}\n"
                f"   Support: ${sr['support']:,.2f}\n"
                f"   ➡️ Risque de continuation baissiere !"
            )

    if alerts:
        msg = "⚠️ *Alertes Niveaux Cles*\n\n" + "\n\n".join(alerts)
        await context.bot.send_message(
            chat_id=context.job.chat_id,
            text=msg,
            parse_mode="Markdown"
        )
    else:
        logger.info("Check S/R: aucune cassure detectee.")


# ============================================================
# ALERTE LIQUIDATION
# ============================================================
async def check_liquidation_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Alerte si une position ouverte approche son prix de liquidation."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API,
                json={"type": "clearinghouseState", "user": HYPERLIQUID_ADDRESS},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()

        positions = data.get("assetPositions", [])
        open_pos  = [p for p in positions if float(p.get("position", {}).get("szi", 0)) != 0]

        if not open_pos:
            return

        prices  = await get_crypto_prices()
        alerts  = []

        for p in open_pos:
            pos        = p.get("position", {})
            coin       = pos.get("coin", "?")
            liq_px     = float(pos.get("liquidationPx") or 0)
            size       = float(pos.get("szi", 0))
            current    = prices.get(coin, {}).get("usd", 0)

            if liq_px == 0 or current == 0:
                continue

            dist_pct = abs((current - liq_px) / current * 100)
            direction = "LONG" if size > 0 else "SHORT"

            if dist_pct <= LIQUIDATION_ALERT_PERCENT:
                alerts.append(
                    f"🔴 *{coin} {direction} — DANGER LIQUIDATION*\n"
                    f"   Prix actuel:    ${current:,.2f}\n"
                    f"   Prix liquidation: ${liq_px:,.2f}\n"
                    f"   Distance: {dist_pct:.1f}% — AGIS VITE !"
                )

        if alerts:
            msg = "🚨 *ALERTE LIQUIDATION*\n\n" + "\n\n".join(alerts)
            await context.bot.send_message(
                chat_id=context.job.chat_id,
                text=msg,
                parse_mode="Markdown"
            )

    except Exception as e:
        logger.error(f"Erreur check liquidation: {e}")


# ============================================================
# JOB COMBINE 8H ET 20H
# ============================================================
async def job_twice_daily(context: ContextTypes.DEFAULT_TYPE):
    """Lance les checks S/R et liquidation a 8h et 20h."""
    await check_sr_alerts(context)
    await check_liquidation_alerts(context)


# ============================================================
# API NEWS
# ============================================================
async def get_crypto_news() -> list:
    url     = "https://www.coindesk.com/arc/outboundfeeds/rss/"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MarignyCryptoBot/1.0)"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                text      = await resp.text()
                items_raw = re.findall(r'<item>(.*?)</item>', text, re.DOTALL)
                results   = []
                for item in items_raw[:3]:
                    title_match = re.search(r'<title><!\[CDATA\[(.*?)\]\]></title>', item)
                    link_match  = re.search(r'<link>(.*?)</link>', item)
                    if title_match and link_match:
                        results.append({"title": title_match.group(1).strip(), "url": link_match.group(1).strip()})
                return results
    except Exception as e:
        logger.error(f"Erreur news: {e}")
        return []


# ============================================================
# API TRENDING
# ============================================================
async def get_crypto_trending() -> list:
    url     = "https://api.coingecko.com/api/v3/search/trending"
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data  = await resp.json()
                coins = data.get("coins", [])[:3]
                return [
                    {
                        "name":   c["item"].get("name", ""),
                        "symbol": c["item"].get("symbol", ""),
                        "rank":   c["item"].get("market_cap_rank", "?"),
                        "change": c["item"].get("data", {}).get("price_change_percentage_24h", {}).get("usd", 0) or 0
                    }
                    for c in coins
                ]
    except Exception as e:
        logger.error(f"Erreur trending: {e}")
        return []


# ============================================================
# HYPERLIQUID — POSITIONS & BALANCE
# ============================================================
async def get_hyperliquid_positions() -> list:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API,
                json={"type": "clearinghouseState", "user": HYPERLIQUID_ADDRESS},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                return data.get("assetPositions", [])
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
        return "❌ Impossible de recuperer les prix."
    lines = ["📊 *Prix en temps reel*\n"]
    for symbol in WATCHED_TOKENS:
        if symbol in prices:
            d      = prices[symbol]
            emoji  = "🟢" if d["usd_24h_change"] >= 0 else "🔴"
            lines.append(
                f"{emoji} *{symbol}*\n"
                f"   💵 ${d['usd']:,.2f}  |  💶 €{d['eur']:,.2f}\n"
                f"   24h: {d['usd_24h_change']:+.2f}%\n"
            )
    lines.append(f"_Mis a jour: {datetime.now().strftime('%H:%M:%S')}_")
    return "\n".join(lines)


def format_positions(positions: list, balance: dict) -> str:
    lines = ["📈 *Positions Hyperliquid*\n"]
    if balance:
        pnl   = balance.get("totalUnrealizedPnl", 0)
        av    = balance.get("accountValue", 0)
        mu    = balance.get("totalMarginUsed", 0)
        lines.append("💼 *Compte*")
        lines.append(f"   Valeur totale: ${av:,.2f}" if av > 0 else "   Valeur totale: _non disponible_")
        if mu > 0:
            lines.append(f"   Marge utilisee: ${mu:,.2f}")
        lines.append(f"   {'🟢' if pnl >= 0 else '🔴'} PnL non realise: ${pnl:+,.2f}\n")

    open_pos = [p for p in positions if float(p.get("position", {}).get("szi", 0)) != 0]
    if not open_pos:
        lines.append("_Aucune position ouverte en ce moment._")
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
                f"   Taille: {abs(size)}\n"
                f"   Entree: ${entry:,.4f}\n"
                f"   {'✅' if upnl >= 0 else '❌'} PnL: ${upnl:+,.2f}\n"
                + (f"   ⚠️ Liquidation: ${liq_px:,.2f}\n" if liq_px > 0 else "")
            )
    lines.append(f"_Mis a jour: {datetime.now().strftime('%H:%M:%S')}_")
    return "\n".join(lines)


def format_daily_summary(news: list, trending: list) -> str:
    now   = datetime.now().strftime("%d/%m/%Y %H:%M")
    lines = [f"🌅 *Resume Crypto — {now}*\n", "━━━━━━━━━━━━━━━━━━━━", "📰 *3 Actus Crypto du Jour*\n"]
    if news:
        for i, n in enumerate(news, 1):
            lines.append(f"{i}. [{n['title']}]({n['url']})\n")
    else:
        lines.append("_Actualites indisponibles._\n")
    lines += ["━━━━━━━━━━━━━━━━━━━━", "🔥 *Top 3 Tokens Trending (24h)*\n"]
    if trending:
        for i, t in enumerate(trending, 1):
            change = t.get("change", 0) or 0
            lines.append(f"{i}. *{t['name']}* (${t['symbol']}) — Rank #{t['rank']} {'🟢' if change >= 0 else '🔴'} {change:+.1f}%\n")
    else:
        lines.append("_Trending indisponible._\n")
    lines += ["━━━━━━━━━━━━━━━━━━━━", "_Bonne journee depuis Vallauris! 🌴_"]
    return "\n".join(lines)


async def analyze_setup(prices: dict, trending: list) -> str:
    lines = ["🎯 *Analyse Setups de Trade*\n"]
    if not prices:
        return "❌ Donnees de prix indisponibles."
    for symbol in WATCHED_TOKENS:
        if symbol not in prices:
            lines.append(f"_⚠️ {symbol} indisponible_\n")
            continue
        change = prices[symbol].get("usd_24h_change", 0)
        ohlcv  = await get_ohlcv(symbol, interval="4h", count=50)
        lines.append(build_token_analysis(symbol, change, ohlcv))
        lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("🔥 *Top 3 Tokens Trending (24h)*\n")
    if trending:
        for i, t in enumerate(trending, 1):
            change = t.get("change", 0) or 0
            lines.append(f"{i}. *{t['name']}* (${t['symbol']}) — Rank #{t['rank']} {'🟢' if change >= 0 else '🔴'} {change:+.1f}%\n")
    else:
        lines.append("_Trending indisponible._\n")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


# ============================================================
# COMMANDES BOT
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    msg = (
        "👋 *Bienvenue sur MarignyCryptoBot!*\n\n"
        "Voici les commandes disponibles:\n\n"
        "📊 /prix — Prix en temps reel\n"
        "📈 /positions — Tes positions Hyperliquid\n"
        "🎯 /setup — Analyse technique complete\n"
        "📋 /resume — Resume complet du marche\n"
        "🧠 /peur — Fear & Greed Index\n"
        "🔔 /alertes — Activer les alertes auto\n"
        "🔕 /desactiver\\_alertes — Stopper les alertes\n"
        "ℹ️ /aide — Affiche ce message\n\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_prix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    await update.message.reply_text("⏳ Recuperation des prix...", parse_mode="Markdown")
    prices = await get_crypto_prices()
    await update.message.reply_text(format_prices(prices), parse_mode="Markdown")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    await update.message.reply_text("⏳ Connexion a Hyperliquid...", parse_mode="Markdown")
    positions, balance = await asyncio.gather(get_hyperliquid_positions(), get_hyperliquid_balance())
    await update.message.reply_text(format_positions(positions, balance), parse_mode="Markdown")


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    await update.message.reply_text("⏳ Analyse technique en cours (RSI, EMA, S/R, Fibo, Volume)...", parse_mode="Markdown")
    prices, trending = await asyncio.gather(get_crypto_prices(), get_crypto_trending())
    msg = await analyze_setup(prices, trending)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    await update.message.reply_text("⏳ Preparation du resume...", parse_mode="Markdown")
    news, trending = await asyncio.gather(get_crypto_news(), get_crypto_trending())
    await update.message.reply_text(format_daily_summary(news, trending), parse_mode="Markdown", disable_web_page_preview=True)


async def cmd_peur(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    await update.message.reply_text("⏳ Recuperation du Fear & Greed Index...", parse_mode="Markdown")
    fg  = await get_fear_greed()
    msg = format_fear_greed(fg)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_aide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
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
                    text=f"{emoji} *ALERTE {symbol}*\nVariation: {variation:+.2f}% en 1h\nPrix actuel: ${current:,.2f}",
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
    if not is_authorized(update): return
    chat_id   = update.effective_chat.id
    job_queue = context.job_queue

    # Supprime les anciens jobs
    for name in [f"alert_{chat_id}", f"daily_{chat_id}", f"sr_{chat_id}_8", f"sr_{chat_id}_20"]:
        for job in job_queue.get_jobs_by_name(name):
            job.schedule_removal()

    # Alerte prix toutes les heures
    job_queue.run_repeating(job_price_alert, interval=3600, first=10, chat_id=chat_id, name=f"alert_{chat_id}")

    # Resume quotidien 8h
    job_queue.run_daily(job_daily_summary, time=time(7, 35), chat_id=chat_id, name=f"daily_{chat_id}")

    # Check S/R + liquidation a 8h heure francaise (UTC+1 = 7h UTC)
    job_queue.run_daily(job_twice_daily, time=time(7, 35), chat_id=chat_id, name=f"sr_{chat_id}_8")

    # Check S/R + liquidation a 20h heure francaise (UTC+1 = 19h UTC)
    job_queue.run_daily(job_twice_daily, time=time(19, 0), chat_id=chat_id, name=f"sr_{chat_id}_20")

    await update.message.reply_text(
        "✅ *Alertes activees!*\n\n"
        f"• Alerte prix si variation > {ALERT_THRESHOLD_PERCENT}% / heure\n"
        f"• Resume quotidien a 8h\n"
        f"• Check S/R + liquidation a 8h et 20h (heure francaise)\n\n"
        "Utilise /desactiver\\_alertes pour stopper.",
        parse_mode="Markdown"
    )


async def cmd_desactiver_alertes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    chat_id = update.effective_chat.id
    for name in [f"alert_{chat_id}", f"daily_{chat_id}", f"sr_{chat_id}_8", f"sr_{chat_id}_20"]:
        for job in context.job_queue.get_jobs_by_name(name):
            job.schedule_removal()
    await update.message.reply_text("🔕 Toutes les alertes sont desactivees.")


# ============================================================
# MAIN
# ============================================================
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",              cmd_start))
    app.add_handler(CommandHandler("aide",               cmd_aide))
    app.add_handler(CommandHandler("prix",               cmd_prix))
    app.add_handler(CommandHandler("positions",          cmd_positions))
    app.add_handler(CommandHandler("setup",              cmd_setup))
    app.add_handler(CommandHandler("resume",             cmd_resume))
    app.add_handler(CommandHandler("peur",               cmd_peur))
    app.add_handler(CommandHandler("alertes",            cmd_activer_alertes))
    app.add_handler(CommandHandler("desactiver_alertes", cmd_desactiver_alertes))
    logger.info("🤖 MarignyCryptoBot demarre!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
