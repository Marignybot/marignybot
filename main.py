#!/usr/bin/env python3
"""
MarignyCryptoBot - Bot Telegram pour suivi crypto & Hyperliquid
Auteur: Pour Tabac Le Marigny, Vallauris
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
    """Prix mid de tous les tokens via Hyperliquid allMids."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API,
                json={"type": "allMids"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                all_mids = await resp.json()
                # all_mids = {"BTC": "84500.0", "ETH": "2100.0", ...}

            # Variation 24h via metaAndAssetCtxs
            async with session.post(
                HYPERLIQUID_API,
                json={"type": "metaAndAssetCtxs"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp2:
                meta_data = await resp2.json()

        result = {}
        # meta_data[0] = meta (liste des assets avec nom)
        # meta_data[1] = liste des ctxs (prix, volume, funding, etc.)
        universe = meta_data[0].get("universe", []) if isinstance(meta_data, list) else []
        ctxs     = meta_data[1] if isinstance(meta_data, list) and len(meta_data) > 1 else []

        # Construire un index nom -> ctx
        ctx_by_name = {}
        for i, asset in enumerate(universe):
            name = asset.get("name", "")
            if i < len(ctxs):
                ctx_by_name[name] = ctxs[i]

        for symbol in WATCHED_TOKENS:
            mid = all_mids.get(symbol)
            if mid is None:
                continue
            usd = float(mid)
            ctx = ctx_by_name.get(symbol, {})
            # prevDayPx = prix il y a 24h
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
# HYPERLIQUID — OHLCV (BOUGIES) POUR ANALYSE TECHNIQUE
# ============================================================
async def get_ohlcv(symbol: str, interval: str = "4h", count: int = 50) -> dict:
    """Recupere les bougies OHLCV depuis Hyperliquid candleSnapshot."""
    # startTime = maintenant - count * interval en ms
    interval_ms = {
        "1h": 3600000, "4h": 14400000, "1d": 86400000
    }.get(interval, 14400000)
    start_time = int(time_module.time() * 1000) - (count * interval_ms)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API,
                json={
                    "type": "candleSnapshot",
                    "req": {
                        "coin": symbol,
                        "interval": interval,
                        "startTime": start_time
                    }
                },
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                candles = await resp.json()

        if not candles or not isinstance(candles, list):
            return {}

        # Hyperliquid candle: {"t": timestamp, "o": open, "h": high, "l": low, "c": close, "v": volume}
        closes  = [float(c["c"]) for c in candles]
        highs   = [float(c["h"]) for c in candles]
        lows    = [float(c["l"]) for c in candles]
        volumes = [float(c["v"]) for c in candles]
        return {"closes": closes, "highs": highs, "lows": lows, "volumes": volumes}

    except Exception as e:
        logger.error(f"Erreur OHLCV Hyperliquid {symbol}: {e}")
        return {}


# ============================================================
# INDICATEURS TECHNIQUES
# ============================================================
def compute_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def compute_ema(closes: list, period: int) -> float:
    if len(closes) < period:
        return closes[-1] if closes else 0
    k = 2 / (period + 1)
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
    current     = closes[-1]
    diff        = recent_high - recent_low
    fib_382     = round(recent_low + diff * 0.382, 2)
    fib_618     = round(recent_low + diff * 0.618, 2)
    return {
        "support":    round(recent_low, 2),
        "resistance": round(recent_high, 2),
        "fib_382":    fib_382,
        "fib_618":    fib_618,
        "current":    round(current, 2),
    }


def volume_signal(volumes: list) -> str:
    if len(volumes) < 5:
        return "normal"
    avg   = sum(volumes[-6:-1]) / 5
    last  = volumes[-1]
    ratio = last / avg if avg > 0 else 1
    if ratio >= 1.5:
        return "fort"
    elif ratio <= 0.6:
        return "faible"
    return "normal"


def build_token_analysis(symbol: str, change: float, ohlcv: dict) -> str:
    lines = []

    if change <= -8:
        tendance = "Possible rebond — chute importante"
        conseil  = "Zone de support potentielle. Surveille un retournement haussier."
        t_emoji  = "🔥"
    elif change >= 8:
        tendance = "Momentum haussier fort"
        conseil  = "Breakout en cours. Attention au retrace avant continuation."
        t_emoji  = "⚡"
    elif 3 < change < 8:
        tendance = "Tendance haussiere moderee"
        conseil  = "Momentum positif. Attends confirmation au-dessus de la resistance."
        t_emoji  = "📈"
    elif -8 < change <= -3:
        tendance = "Tendance baissiere moderee"
        conseil  = "Prudence. Surveille le support. Pas de signal fort."
        t_emoji  = "📉"
    else:
        tendance = "Consolidation / Range"
        conseil  = "Range serre. Attends une cassure claire pour trader."
        t_emoji  = "⏸"

    lines.append(f"{t_emoji} *{symbol}* — {tendance}")
    lines.append(f"   Variation 24h: {change:+.2f}%")

    if ohlcv:
        closes  = ohlcv.get("closes", [])
        highs   = ohlcv.get("highs", [])
        lows    = ohlcv.get("lows", [])
        volumes = ohlcv.get("volumes", [])

        rsi = compute_rsi(closes)
        if rsi >= 70:
            rsi_label = f"{rsi} — Surachat ⚠️"
        elif rsi <= 30:
            rsi_label = f"{rsi} — Survendu, opportunite 🛒"
        else:
            rsi_label = f"{rsi} — Neutre"
        lines.append(f"   RSI(14): {rsi_label}")

        ema9  = compute_ema(closes, 9)
        ema21 = compute_ema(closes, 21)
        current_price = closes[-1] if closes else 0
        ema_trend = "haussier 🟢" if ema9 > ema21 else "baissier 🔴"
        lines.append(f"   EMA9/EMA21: ${ema9:,.2f} / ${ema21:,.2f} — {ema_trend}")

        sr = find_support_resistance(highs, lows, closes)
        if sr:
            dist_sup = ((current_price - sr['support'])    / current_price * 100) if current_price else 0
            dist_res = ((sr['resistance'] - current_price) / current_price * 100) if current_price else 0
            lines.append(f"   Support:    ${sr['support']:,.2f}  (-{dist_sup:.1f}%)")
            lines.append(f"   Resistance: ${sr['resistance']:,.2f}  (+{dist_res:.1f}%)")
            lines.append(f"   Fibo 38.2%: ${sr['fib_382']:,.2f}")
            lines.append(f"   Fibo 61.8%: ${sr['fib_618']:,.2f}")

            if current_price >= sr['resistance'] * 0.99:
                lines.append(f"   🚨 CASSURE RESISTANCE IMMINENTE")
            elif current_price <= sr['support'] * 1.01:
                lines.append(f"   🚨 TEST DU SUPPORT EN COURS")

        vol = volume_signal(volumes)
        vol_emoji = "🔊" if vol == "fort" else ("🔇" if vol == "faible" else "📊")
        lines.append(f"   Volume: {vol} {vol_emoji}")
    else:
        lines.append(f"   _Donnees techniques indisponibles_")

    lines.append(f"   ➡️ {conseil}")
    return "\n".join(lines)


# ============================================================
# API NEWS
# ============================================================
async def get_crypto_news() -> list:
    url = "https://www.coindesk.com/arc/outboundfeeds/rss/"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MarignyCryptoBot/1.0)"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                text = await resp.text()
                items_raw = re.findall(r'<item>(.*?)</item>', text, re.DOTALL)
                results = []
                for item in items_raw[:3]:
                    title_match = re.search(r'<title><!\[CDATA\[(.*?)\]\]></title>', item)
                    link_match  = re.search(r'<link>(.*?)</link>', item)
                    if title_match and link_match:
                        results.append({
                            "title": title_match.group(1).strip(),
                            "url":   link_match.group(1).strip()
                        })
                return results
    except Exception as e:
        logger.error(f"Erreur news: {e}")
        return []


# ============================================================
# API TRENDING (CoinGecko — 1 seul appel)
# ============================================================
async def get_crypto_trending() -> list:
    url = "https://api.coingecko.com/api/v3/search/trending"
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
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
        logger.error(f"Erreur Hyperliquid positions: {e}")
        return []


async def get_hyperliquid_balance() -> dict:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HYPERLIQUID_API,
                json={"type": "clearinghouseState", "user": HYPERLIQUID_ADDRESS},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                logger.info(f"Hyperliquid keys: {list(data.keys())}")
                margin = (
                    data.get("crossMarginSummary")
                    or data.get("marginSummary")
                    or {}
                )
                account_value        = float(margin.get("accountValue", 0))
                total_margin_used    = float(margin.get("totalMarginUsed", 0))
                total_unrealized_pnl = float(margin.get("totalUnrealizedPnl", 0))
                if account_value == 0:
                    account_value = float(
                        data.get("crossAccountValue", 0)
                        or data.get("withdrawable", 0)
                        or 0
                    )
                if total_unrealized_pnl == 0:
                    positions = data.get("assetPositions", [])
                    total_unrealized_pnl = sum(
                        float(p.get("position", {}).get("unrealizedPnl", 0))
                        for p in positions
                    )
                return {
                    "accountValue":       account_value,
                    "totalMarginUsed":    total_margin_used,
                    "totalUnrealizedPnl": total_unrealized_pnl,
                }
    except Exception as e:
        logger.error(f"Erreur Hyperliquid balance: {e}")
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
            data   = prices[symbol]
            usd    = data.get("usd", 0)
            eur    = data.get("eur", 0)
            change = data.get("usd_24h_change", 0)
            emoji  = "🟢" if change >= 0 else "🔴"
            lines.append(
                f"{emoji} *{symbol}*\n"
                f"   💵 ${usd:,.2f}  |  💶 €{eur:,.2f}\n"
                f"   24h: {change:+.2f}%\n"
            )
    lines.append(f"_Mis a jour: {datetime.now().strftime('%H:%M:%S')}_")
    return "\n".join(lines)


def format_positions(positions: list, balance: dict) -> str:
    lines = ["📈 *Positions Hyperliquid*\n"]
    if balance:
        pnl           = balance.get("totalUnrealizedPnl", 0)
        account_value = balance.get("accountValue", 0)
        margin_used   = balance.get("totalMarginUsed", 0)
        pnl_emoji     = "🟢" if pnl >= 0 else "🔴"
        lines.append("💼 *Compte*")
        lines.append(f"   Valeur totale: ${account_value:,.2f}" if account_value > 0 else "   Valeur totale: _non disponible_")
        if margin_used > 0:
            lines.append(f"   Marge utilisee: ${margin_used:,.2f}")
        lines.append(f"   {pnl_emoji} PnL non realise: ${pnl:+,.2f}\n")

    open_positions = [p for p in positions if float(p.get("position", {}).get("szi", 0)) != 0]
    if not open_positions:
        lines.append("_Aucune position ouverte en ce moment._")
    else:
        lines.append(f"*{len(open_positions)} position(s) ouverte(s):*\n")
        for p in open_positions:
            pos        = p.get("position", {})
            coin       = pos.get("coin", "?")
            size       = float(pos.get("szi", 0))
            entry      = float(pos.get("entryPx", 0))
            upnl       = float(pos.get("unrealizedPnl", 0))
            direction  = "LONG 🟢" if size > 0 else "SHORT 🔴"
            upnl_emoji = "✅" if upnl >= 0 else "❌"
            lines.append(
                f"*{coin}* — {direction}\n"
                f"   Taille: {abs(size)}\n"
                f"   Entree: ${entry:,.4f}\n"
                f"   {upnl_emoji} PnL: ${upnl:+,.2f}\n"
            )
    lines.append(f"_Mis a jour: {datetime.now().strftime('%H:%M:%S')}_")
    return "\n".join(lines)


def format_daily_summary(news: list, trending: list) -> str:
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    lines = [
        f"🌅 *Resume Crypto — {now}*\n",
        "━━━━━━━━━━━━━━━━━━━━",
        "📰 *3 Actus Crypto du Jour*\n",
    ]
    if news:
        for i, n in enumerate(news, 1):
            lines.append(f"{i}. [{n['title']}]({n['url']})\n")
    else:
        lines.append("_Actualites indisponibles pour le moment._\n")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("🔥 *Top 3 Tokens Trending (24h)*\n")
    if trending:
        for i, t in enumerate(trending, 1):
            change = t.get("change", 0) or 0
            emoji  = "🟢" if change >= 0 else "🔴"
            lines.append(f"{i}. *{t['name']}* (${t['symbol']}) — Rank #{t['rank']} {emoji} {change:+.1f}%\n")
    else:
        lines.append("_Trending indisponible pour le moment._\n")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("_Bonne journee depuis Vallauris! 🌴_")
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
        bloc   = build_token_analysis(symbol, change, ohlcv)
        lines.append(bloc)
        lines.append("━━━━━━━━━━━━━━━━━━━━")

    lines.append("🔥 *Top 3 Tokens Trending (24h)*\n")
    if trending:
        for i, t in enumerate(trending, 1):
            change = t.get("change", 0) or 0
            emoji  = "🟢" if change >= 0 else "🔴"
            lines.append(f"{i}. *{t['name']}* (${t['symbol']}) — Rank #{t['rank']} {emoji} {change:+.1f}%\n")
    else:
        lines.append("_Trending indisponible pour le moment._\n")

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
        "ℹ️ /aide — Affiche ce message\n\n"
        "_Bot cree pour Tabac Le Marigny, Vallauris 🌴_"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_prix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    await update.message.reply_text("⏳ Recuperation des prix...", parse_mode="Markdown")
    prices = await get_crypto_prices()
    msg = format_prices(prices)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    await update.message.reply_text("⏳ Connexion a Hyperliquid...", parse_mode="Markdown")
    positions, balance = await asyncio.gather(
        get_hyperliquid_positions(),
        get_hyperliquid_balance()
    )
    msg = format_positions(positions, balance)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    await update.message.reply_text("⏳ Analyse technique en cours (RSI, EMA, S/R, Fibo, Volume)...", parse_mode="Markdown")
    prices, trending = await asyncio.gather(
        get_crypto_prices(),
        get_crypto_trending()
    )
    msg = await analyze_setup(prices, trending)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    await update.message.reply_text("⏳ Preparation du resume...", parse_mode="Markdown")
    news, trending = await asyncio.gather(
        get_crypto_news(),
        get_crypto_trending()
    )
    msg = format_daily_summary(news, trending)
    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)


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
                msg = (
                    f"{emoji} *ALERTE {symbol}*\n"
                    f"Variation: {variation:+.2f}% en 1h\n"
                    f"Prix actuel: ${current:,.2f}"
                )
                await context.bot.send_message(
                    chat_id=context.job.chat_id,
                    text=msg,
                    parse_mode="Markdown"
                )
        last_prices[symbol] = current


async def job_daily_summary(context: ContextTypes.DEFAULT_TYPE):
    news, trending = await asyncio.gather(
        get_crypto_news(),
        get_crypto_trending()
    )
    msg = format_daily_summary(news, trending)
    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text=msg,
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


async def cmd_activer_alertes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    chat_id = update.effective_chat.id
    job_queue = context.job_queue
    for job in job_queue.get_jobs_by_name(f"alert_{chat_id}"):
        job.schedule_removal()
    job_queue.run_repeating(
        job_price_alert,
        interval=3600,
        first=10,
        chat_id=chat_id,
        name=f"alert_{chat_id}"
    )
    job_queue.run_daily(
        job_daily_summary,
        time=time(DAILY_SUMMARY_HOUR, DAILY_SUMMARY_MIN),
        chat_id=chat_id,
        name=f"daily_{chat_id}"
    )
    await update.message.reply_text(
        "✅ *Alertes activees!*\n\n"
        f"• Alerte prix si variation > {ALERT_THRESHOLD_PERCENT}% / heure\n"
        f"• Resume quotidien a {DAILY_SUMMARY_HOUR:02d}h{DAILY_SUMMARY_MIN:02d}\n\n"
        "Utilise /desactiver\\_alertes pour stopper.",
        parse_mode="Markdown"
    )


async def cmd_desactiver_alertes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    chat_id = update.effective_chat.id
    for name in [f"alert_{chat_id}", f"daily_{chat_id}"]:
        for job in context.job_queue.get_jobs_by_name(name):
            job.schedule_removal()
    await update.message.reply_text("🔕 Alertes desactivees.")


# ============================================================
# MAIN
# ============================================================
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("aide", cmd_aide))
    app.add_handler(CommandHandler("prix", cmd_prix))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("alertes", cmd_activer_alertes))
    app.add_handler(CommandHandler("desactiver_alertes", cmd_desactiver_alertes))
    logger.info("🤖 MarignyCryptoBot demarre!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
