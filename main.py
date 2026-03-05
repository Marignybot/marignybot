#!/usr/bin/env python3
"""
MarignyCryptoBot - Bot Telegram pour suivi crypto & Hyperliquid
Auteur: Pour Tabac Le Marigny, Vallauris
"""

import asyncio
import logging
import json
from datetime import datetime, time
import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# CONFIGURATION — MODIFIE CES VALEURS
# ============================================================
TELEGRAM_TOKEN = "8413363300:AAEldjYE3nqAoF9-tZdYurwH1PNfUWJbZEQ"  # Token Marignybot
HYPERLIQUID_ADDRESS = "0x6e89b986FBB4B985AcCC9B3CfEE4c7B5301D9a5C"

# Tokens à surveiller (symboles CoinGecko)
WATCHED_TOKENS = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "hyperliquid": "HYPE",
}

# Alertes de prix — seuil en % pour déclencher une alerte
ALERT_THRESHOLD_PERCENT = 5.0  # Alerte si variation > 5% en 1h

# Heure du résumé quotidien (format 24h)
DAILY_SUMMARY_HOUR = 8   # 8h00 du matin
DAILY_SUMMARY_MIN = 0

# ============================================================
# SETUP LOGGING
# ============================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============================================================
# STOCKAGE TEMPORAIRE DES PRIX (pour calcul variation)
# ============================================================
last_prices = {}

# ============================================================
# FONCTIONS API
# ============================================================

async def get_crypto_prices() -> dict:
    """Récupère les prix en temps réel via CoinGecko"""
    ids = ",".join(WATCHED_TOKENS.keys())
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd,eur&include_24hr_change=true"
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
                return data
    except Exception as e:
        logger.error(f"Erreur CoinGecko: {e}")
        return {}


async def get_crypto_news() -> list:
    """Récupère les dernières actus crypto via CoinDesk RSS"""
    url = "https://www.coindesk.com/arc/outboundfeeds/rss/"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MarignyCryptoBot/1.0)"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                text = await resp.text()
                import re
                # Extrait les blocs <item>
                items_raw = re.findall(r'<item>(.*?)</item>', text, re.DOTALL)
                results = []
                for item in items_raw[:3]:
                    title_match = re.search(r'<title><!\[CDATA\[(.*?)\]\]></title>', item)
                    link_match = re.search(r'<link>(.*?)</link>', item)
                    if title_match and link_match:
                        results.append({
                            "title": title_match.group(1).strip(),
                            "url": link_match.group(1).strip()
                        })
                return results
    except Exception as e:
        logger.error(f"Erreur news CoinDesk: {e}")
        return []


async def get_crypto_trending() -> list:
    """Récupère les tokens trending via CoinGecko"""
    url = "https://api.coingecko.com/api/v3/search/trending"
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
                coins = data.get("coins", [])[:3]
                return [
                    {
                        "name": c["item"].get("name", ""),
                        "symbol": c["item"].get("symbol", ""),
                        "rank": c["item"].get("market_cap_rank", "?"),
                        "change": c["item"].get("data", {}).get("price_change_percentage_24h", {}).get("usd", 0) or 0
                    }
                    for c in coins
                ]
    except Exception as e:
        logger.error(f"Erreur trending: {e}")
        return []


async def get_hyperliquid_positions() -> list:
    """Récupère les positions ouvertes sur Hyperliquid"""
    url = "https://api.hyperliquid.xyz/info"
    payload = {
        "type": "clearinghouseState",
        "user": HYPERLIQUID_ADDRESS
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                return data.get("assetPositions", [])
    except Exception as e:
        logger.error(f"Erreur Hyperliquid: {e}")
        return []


async def get_hyperliquid_balance() -> dict:
    """Récupère le solde du compte Hyperliquid"""
    url = "https://api.hyperliquid.xyz/info"
    payload = {
        "type": "clearinghouseState",
        "user": HYPERLIQUID_ADDRESS
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                margin = data.get("marginSummary", {})
                return {
                    "accountValue": float(margin.get("accountValue", 0)),
                    "totalMarginUsed": float(margin.get("totalMarginUsed", 0)),
                    "totalUnrealizedPnl": float(margin.get("totalUnrealizedPnl", 0)),
                }
    except Exception as e:
        logger.error(f"Erreur Hyperliquid balance: {e}")
        return {}

# ============================================================
# FORMATAGE DES MESSAGES
# ============================================================

def format_prices(prices: dict) -> str:
    if not prices:
        return "❌ Impossible de récupérer les prix."
    
    lines = ["📊 *Prix en temps réel*\n"]
    for coin_id, symbol in WATCHED_TOKENS.items():
        if coin_id in prices:
            data = prices[coin_id]
            usd = data.get("usd", 0)
            eur = data.get("eur", 0)
            change = data.get("usd_24h_change", 0)
            emoji = "🟢" if change >= 0 else "🔴"
            lines.append(
                f"{emoji} *{symbol}*\n"
                f"   💵 ${usd:,.2f}  |  💶 €{eur:,.2f}\n"
                f"   24h: {change:+.2f}%\n"
            )
    lines.append(f"_Mis à jour: {datetime.now().strftime('%H:%M:%S')}_")
    return "\n".join(lines)


def format_positions(positions: list, balance: dict) -> str:
    lines = ["📈 *Positions Hyperliquid*\n"]
    
    if balance:
        pnl = balance.get("totalUnrealizedPnl", 0)
        pnl_emoji = "🟢" if pnl >= 0 else "🔴"
        lines.append(
            f"💼 *Compte*\n"
            f"   Valeur totale: ${balance.get('accountValue', 0):,.2f}\n"
            f"   Marge utilisée: ${balance.get('totalMarginUsed', 0):,.2f}\n"
            f"   {pnl_emoji} PnL non réalisé: ${pnl:+,.2f}\n"
        )

    open_positions = [p for p in positions if float(p.get("position", {}).get("szi", 0)) != 0]
    
    if not open_positions:
        lines.append("_Aucune position ouverte en ce moment._")
    else:
        lines.append(f"*{len(open_positions)} position(s) ouverte(s):*\n")
        for p in open_positions:
            pos = p.get("position", {})
            coin = pos.get("coin", "?")
            size = float(pos.get("szi", 0))
            entry = float(pos.get("entryPx", 0))
            upnl = float(pos.get("unrealizedPnl", 0))
            direction = "LONG 🟢" if size > 0 else "SHORT 🔴"
            upnl_emoji = "✅" if upnl >= 0 else "❌"
            lines.append(
                f"*{coin}* — {direction}\n"
                f"   Taille: {abs(size)}\n"
                f"   Entrée: ${entry:,.4f}\n"
                f"   {upnl_emoji} PnL: ${upnl:+,.2f}\n"
            )
    
    lines.append(f"_Mis à jour: {datetime.now().strftime('%H:%M:%S')}_")
    return "\n".join(lines)


def format_daily_summary(news: list, trending: list) -> str:
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    lines = [
        f"🌅 *Résumé Crypto — {now}*\n",
        "━━━━━━━━━━━━━━━━━━━━",
        "📰 *3 Actus Crypto du Jour*\n",
    ]
    if news:
        for i, n in enumerate(news, 1):
            lines.append(f"{i}. [{n['title']}]({n['url']})\n")
    else:
        lines.append("_Actualités indisponibles pour le moment._\n")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("🔥 *Top 3 Tokens Trending (24h)*\n")
    if trending:
        for i, t in enumerate(trending, 1):
            change = t.get("change", 0) or 0
            emoji = "🟢" if change >= 0 else "🔴"
            lines.append(f"{i}. *{t['name']}* (${t['symbol']}) — Rank #{t['rank']} {emoji} {change:+.1f}%\n")
    else:
        lines.append("_Trending indisponible pour le moment._\n")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("_Bonne journée depuis Vallauris! 🌴_")
    return "\n".join(lines)


def analyze_setup(prices: dict) -> str:
    """Analyse simple de setup de trade basée sur les variations 24h"""
    lines = ["🎯 *Analyse Setups de Trade*\n"]
    
    if not prices:
        return "❌ Données indisponibles pour l'analyse."
    
    for coin_id, symbol in WATCHED_TOKENS.items():
        if coin_id not in prices:
            continue
        change = prices[coin_id].get("usd_24h_change", 0)
        price = prices[coin_id].get("usd", 0)
        
        if change <= -8:
            lines.append(
                f"🔥 *{symbol}* — Possible rebond\n"
                f"   Variation 24h: {change:.2f}%\n"
                f"   ➡️ Zone de support potentielle. Surveille un retournement.\n"
            )
        elif change >= 8:
            lines.append(
                f"⚡ *{symbol}* — Momentum haussier\n"
                f"   Variation 24h: {change:.2f}%\n"
                f"   ➡️ Breakout possible. Attention au retrace.\n"
            )
        elif -3 <= change <= 3:
            lines.append(
                f"⏸️ *{symbol}* — Consolidation\n"
                f"   Variation 24h: {change:.2f}%\n"
                f"   ➡️ Range serré. Attends une cassure claire.\n"
            )
        else:
            lines.append(
                f"📉 *{symbol}* — Tendance baissière modérée\n"
                f"   Variation 24h: {change:.2f}%\n"
                f"   ➡️ Prudence. Pas de signal fort.\n"
            )
    
    lines.append("\n⚠️ _Ceci n'est pas un conseil financier. DYOR._")
    return "\n".join(lines)

# ============================================================
# COMMANDES TELEGRAM
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Bienvenue sur MarignyCryptoBot!*\n\n"
        "Voici les commandes disponibles:\n\n"
        "📊 /prix — Prix en temps réel\n"
        "📈 /positions — Tes positions Hyperliquid\n"
        "🎯 /setup — Analyse de setups de trade\n"
        "📋 /resume — Résumé complet du marché\n"
        "ℹ️ /aide — Affiche ce message\n\n"
        "_Bot créé pour Tabac Le Marigny, Vallauris 🌴_"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_prix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Récupération des prix...", parse_mode="Markdown")
    prices = await get_crypto_prices()
    msg = format_prices(prices)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Connexion à Hyperliquid...", parse_mode="Markdown")
    positions, balance = await asyncio.gather(
        get_hyperliquid_positions(),
        get_hyperliquid_balance()
    )
    msg = format_positions(positions, balance)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Analyse en cours...", parse_mode="Markdown")
    prices = await get_crypto_prices()
    msg = analyze_setup(prices)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Préparation du résumé...", parse_mode="Markdown")
    news, trending = await asyncio.gather(
        get_crypto_news(),
        get_crypto_trending()
    )
    msg = format_daily_summary(news, trending)
    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)


async def cmd_aide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)

# ============================================================
# TÂCHES AUTOMATIQUES
# ============================================================

async def job_price_alert(context: ContextTypes.DEFAULT_TYPE):
    """Vérifie les variations importantes et envoie une alerte"""
    global last_prices
    prices = await get_crypto_prices()
    if not prices:
        return

    for coin_id, symbol in WATCHED_TOKENS.items():
        if coin_id not in prices:
            continue
        current = prices[coin_id].get("usd", 0)
        if coin_id in last_prices and last_prices[coin_id] > 0:
            variation = ((current - last_prices[coin_id]) / last_prices[coin_id]) * 100
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
        last_prices[coin_id] = current


async def job_daily_summary(context: ContextTypes.DEFAULT_TYPE):
    """Envoie le résumé quotidien automatiquement"""
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
    """Active les alertes automatiques pour ce chat"""
    chat_id = update.effective_chat.id
    job_queue = context.job_queue
    
    # Supprime les anciens jobs si existants
    current_jobs = job_queue.get_jobs_by_name(f"alert_{chat_id}")
    for job in current_jobs:
        job.schedule_removal()
    
    # Alerte toutes les heures
    job_queue.run_repeating(
        job_price_alert,
        interval=3600,
        first=10,
        chat_id=chat_id,
        name=f"alert_{chat_id}"
    )
    
    # Résumé quotidien à 8h
    job_queue.run_daily(
        job_daily_summary,
        time=time(DAILY_SUMMARY_HOUR, DAILY_SUMMARY_MIN),
        chat_id=chat_id,
        name=f"daily_{chat_id}"
    )
    
    await update.message.reply_text(
        "✅ *Alertes activées!*\n\n"
        f"• Alerte prix si variation > {ALERT_THRESHOLD_PERCENT}% / heure\n"
        f"• Résumé quotidien à {DAILY_SUMMARY_HOUR:02d}h{DAILY_SUMMARY_MIN:02d}\n\n"
        "Utilise /desactiver\\_alertes pour stopper.",
        parse_mode="Markdown"
    )


async def cmd_desactiver_alertes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    for name in [f"alert_{chat_id}", f"daily_{chat_id}"]:
        for job in context.job_queue.get_jobs_by_name(name):
            job.schedule_removal()
    await update.message.reply_text("🔕 Alertes désactivées.")

# ============================================================
# MAIN
# ============================================================

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Commandes
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("aide", cmd_aide))
    app.add_handler(CommandHandler("prix", cmd_prix))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("alertes", cmd_activer_alertes))
    app.add_handler(CommandHandler("desactiver_alertes", cmd_desactiver_alertes))
    
    logger.info("🤖 MarignyCryptoBot démarré!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
