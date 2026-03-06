#!/usr/bin/env python3
"""
MarignyCryptoBot - Bot Telegram pour suivi crypto & Hyperliquid
Auteur: Pour Tabac Le Marigny, Vallauris
"""

import asyncio
import logging
import re
from datetime import datetime, time
import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# CONFIGURATION
# ============================================================
TELEGRAM_TOKEN = "8413363300:AAEldjYE3nqAoF9-tZdYurwH1PNfUWJbZEQ"
HYPERLIQUID_ADDRESS = "0x6e89b986FBB4B985AcCC9B3CfEE4c7B5301D9a5C"

# Mapping coin -> symbole Binance
WATCHED_TOKENS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "HYPE": "HYPEUSDT",
}

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
# API BINANCE — PRIX TEMPS REEL
# ============================================================
async def get_crypto_prices() -> dict:
    """Recupere les prix via Binance (pas de rate limit strict)."""
    result = {}
    headers = {"Accept": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            for symbol, pair in WATCHED_TOKENS.items():
                try:
                    url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={pair}"
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        data = await resp.json()
                        if "lastPrice" in data:
                            usd = float(data["lastPrice"])
                            change = float(data["priceChangePercent"])
                            result[symbol] = {
                                "usd": usd,
                                "eur": usd * 0.92,  # taux EUR approximatif
                                "usd_24h_change": change,
                                "volume": float(data.get("quoteVolume", 0)),
                            }
                except Exception as e:
                    logger.error(f"Erreur Binance prix {symbol}: {e}")
    except Exception as e:
        logger.error(f"Erreur session Binance: {e}")
    return result


# ============================================================
# API BINANCE — OHLCV POUR ANALYSE TECHNIQUE
# ============================================================
async def get_ohlcv(symbol: str, interval: str = "4h", limit: int = 50) -> dict:
    """Recupere les bougies Binance pour les indicateurs techniques."""
    pair = WATCHED_TOKENS.get(symbol)
    if not pair:
        return {}
    url = f"https://api.binance.com/api/v3/klines?symbol={pair}&interval={interval}&limit={limit}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                if not data or not isinstance(data, list):
                    return {}
                # Binance klines: [timestamp, open, high, low, close, volume, ...]
                closes  = [float(k[4]) for k in data]
                highs   = [float(k[2]) for k in data]
                lows    = [float(k[3]) for k in data]
                volumes = [float(k[5]) for k in data]
                return {"closes": closes, "highs": highs, "lows": lows, "volumes": volumes}
    except Exception as e:
        logger.error(f"Erreur OHLCV Binance {symbol}: {e}")
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

        # RSI
        rsi = compute_rsi(closes)
        if rsi >= 70:
            rsi_label = f"{rsi} — Surachat ⚠️"
        elif rsi <= 30:
            rsi_label = f"{rsi} — Survendu, opportunite 🛒"
        else:
            rsi_label = f"{rsi} — Neutre"
        lines.append(f"   RSI(14): {rsi_label}")

        # EMA
        ema9  = compute_ema(closes, 9)
        ema21 = compute_ema(closes, 21)
        current_price = closes[-1] if closes else 0
        ema_trend = "haussier 🟢" if ema9 > ema21 else "baissier 🔴"
        lines.append(f"   EMA9/EMA21: ${ema9:,.2f} / ${ema21:,.2f} — {ema_trend}")

        # Support / Resistance / Fibo
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

        # Volume
        vol = volume_signal(volumes)
        vol_emoji = "🔊" if vol == "fort" else ("🔇" if vol == "faible" else "📊")
        lines.append(f"   Volume: {vol} {vol_emoji}")

    lines.append(f"   ➡️ {conseil}")
    return "\n".join(lines)


# ============================================================
# API NEWS (CoinDesk RSS — pas de rate limit)
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
# API TRENDING (CoinGecko — 1 seul appel, moins de risque)
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
# API HYPERLIQUID
# ============================================================
async def get_hyperliquid_positions() -> list:
    url = "https://api.hyperliquid.xyz/info"
    payload = {"type": "clearinghouseState", "user": HYPERLIQUID_ADDRESS}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                return data.get("assetPositions", [])
    except Exception as e:
        logger.error(f"Erreur Hyperliquid: {e}")
        return []


async def get_hyperliquid_balance() -> dict:
    url = "https://api.hyperliquid.xyz/info"
    payload = {"type": "clearinghouseState", "user": HYPERLIQUID_ADDRESS}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                logger.info(f"Hyperliquid raw response keys: {list(data.keys())}")

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
    for symbol in WATCHED_TOKENS.keys():
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
        if account_value > 0:
            lines.append(f"   Valeur totale: ${account_value:,.2f}")
        else:
            lines.append(f"   Valeur totale: _non disponible_")

        if margin_used > 0:
            lines.append(f"   Marge utilisee: ${margin_used:,.2f}")

        if pnl != 0:
            lines.append(f"   {pnl_emoji} PnL non realise: ${pnl:+,.2f}\n")
        else:
            lines.append(f"   PnL non realise: $0.00\n")

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
        return "❌ Donnees indisponibles pour l'analyse."

    for symbol in WATCHED_TOKENS.keys():
        if symbol not in prices:
            continue
        change = prices[symbol].get("usd_24h_change", 0)
        ohlcv  = await get_ohlcv(symbol, interval="4h", limit=50)
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
    for symbol in WATCHED_TOKENS.keys():
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
    current_jobs = job_queue.get_jobs_by_name(f"alert_{chat_id}")
    for job in current_jobs:
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
