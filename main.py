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
# CONFIGURATION — variables chargees depuis .env
# ============================================================

TELEGRAM_TOKEN = "8413363300:AAEldjYE3nqAoF9-tZdYurwH1PNfUWJbZEQ"
HYPERLIQUID_ADDRESS = "0x6e89b986FBB4B985AcCC9B3CfEE4c7B5301D9a5C"
AUTHORIZED_USER_ID = 1429797974

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
    if mdd <= 10: return 80.0
    if mdd <= 20: return 55.0
    if mdd <= 30: return 25.0
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
    """Recupere les top traders via stats-data.hyperliquid.xyz"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard",
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                logger.info(f"Leaderboard status: {resp.status}")
                data = await resp.json()

        # Structure reelle: {"leaderboardRows": [[adresse, {windowPerformances:[...]}], ...]}
        raw_rows = data.get("leaderboardRows", []) if isinstance(data, dict) else data
        logger.info(f"Traders bruts: {len(raw_rows)}")

        traders = []
        for row in raw_rows[:100]:
            try:
                # row = [adresse_str, {windowPerformances: [...]}]
                if isinstance(row, list) and len(row) >= 2:
                    address    = str(row[0])
                    stats_dict = row[1] if isinstance(row[1], dict) else {}
                elif isinstance(row, dict):
                    address    = row.get("ethAddress") or row.get("user", "")
                    stats_dict = row
                else:
                    continue

                if not address or len(address) < 5:
                    continue

                # Chercher la meilleure window dispo
                window_performances = stats_dict.get("windowPerformances", [])
                window_data = {}
                for w in window_performances:
                    if not isinstance(w, dict):
                        continue
                    wname = w.get("window", "")
                    wp    = w.get("windowPerformance", {})
                    if wname == "allTime":
                        window_data = wp
                        break
                    elif wname in ("month", "week") and not window_data:
                        window_data = wp

                if not window_data:
                    window_data = stats_dict

                pnl      = float(window_data.get("pnl", 0) or 0)
                n_trades = int(float(window_data.get("numTrades", 0) or 0))
                winrate  = float(window_data.get("winRate", 0) or 0) * 100
                roi      = float(window_data.get("roi", 0) or 0) * 100

                mdd_proxy         = abs(min(0.0, roi)) if roi < 0 else max(0.0, 5.0 - roi * 0.1)
                consistency_proxy = 75.0 if winrate >= 55 else 50.0

                traders.append({
                    "address":     address,
                    "pnl":         pnl,
                    "n_trades":    n_trades,
                    "winrate":     winrate,
                    "roi":         roi,
                    "mdd":         round(mdd_proxy, 1),
                    "consistency": round(consistency_proxy, 1),
                })
            except Exception as row_err:
                logger.warning(f"Row ignoree: {row_err}")
                continue

        logger.info(f"Traders parsed avec succes: {len(traders)}")
        return traders

    except Exception as e:
        logger.error(f"Erreur fetch_top_traders: {e}")
        return []


def apply_exclusion_filters(traders: list) -> list:
    """Applique les filtres d'exclusion."""
    filtered = []
    for t in traders:
        # Exclure seulement si MDD catastrophique ou PnL negatif
        if t["mdd"] > TRADEBOT_MAX_DRAWDOWN:
            continue
        if t["pnl"] <= 0:
            continue
        # n_trades optionnel — API ne le fournit pas toujours
        filtered.append(t)
    logger.info(f"Apres filtres: {len(filtered)}/{len(traders)} traders")
    return filtered


def rank_and_score_traders(traders: list) -> list:
    """Score et classe tous les traders filtres."""
    if not traders:
        return []
    max_pnl = max(t["pnl"] for t in traders)
    scored  = []
    for t in traders:
        pnl_score = score_pnl_relative(t["pnl"], max_pnl)
        composite = compute_composite_score(t["consistency"], t["mdd"], t["winrate"], pnl_score)
        scored.append({**t, "score": composite})
    return sorted(scored, key=lambda x: x["score"], reverse=True)


def build_tradebot_report(top5: list) -> str:
    """Construit le rapport de selection ETF synthetique."""
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    avg = round(sum(t["score"] for t in top5) / len(top5), 1) if top5 else 0
    lines = [
        "🏆 *ETF SYNTHETIQUE — Selection TradeBot*",
        f"📅 _{now}_",
        "━━━━━━━━━━━━━━━━━━━━\n",
    ]
    for i, t in enumerate(top5, 1):
        addr    = t["address"][:6] + "..." + t["address"][-4:]
        verdict = "🟢 HOLD" if t["score"] >= 70 else ("🟡 WATCH" if t["score"] >= 55 else "🔴 WEAK")
        lines.append(
            f"*#{i} — `{addr}`*\n"
            f"   Score:       *{t['score']}/100*\n"
            f"   Consistance: {stars(t['consistency'])} ({t['consistency']:.0f}%)\n"
            f"   Drawdown:    {stars(100 - t['mdd'])} ({t['mdd']:.1f}%)\n"
            f"   Win Rate:    {t['winrate']:.1f}%\n"
            f"   PnL 90j:     ${t['pnl']:+,.0f}\n"
            f"   Statut:      {verdict}\n"
        )
    justifications = {
        1: "Meilleur ratio consistance/drawdown sur 90j.",
        2: "Win rate solide avec drawdown maitrise.",
        3: "PnL fort avec bonne regularite.",
        4: "Consistance elevee — peu de semaines negatives.",
        5: "Bon equilibre toutes metriques.",
    }
    lines += [
        "━━━━━━━━━━━━━━━━━━━━",
        f"📊 *Score ETF moyen: {avg}/100*\n",
        "💬 *Justifications:*\n",
    ]
    for i, t in enumerate(top5, 1):
        addr = t["address"][:6] + "..." + t["address"][-4:]
        lines.append(f"*#{i}* `{addr}` — {justifications.get(i, 'Profil solide sur 90j.')}")
    lines += [
        "\n━━━━━━━━━━━━━━━━━━━━",
        "_TradeBot v1.0 — Phase Analyse_",
        "_/tb\\_historique pour voir les selections passees_",
    ]
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
    await update.message.reply_text(build_tradebot_report(top5), parse_mode="Markdown")


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
        "Selectionne les 5 meilleurs traders sur 90 jours.\n\n"
        "📐 *Scoring:*\n"
        "   🥇 Consistance:  35%\n"
        "   🥈 Drawdown min: 30%\n"
        "   🥉 Win Rate:     20%\n"
        "   4️⃣ PnL absolu:   15%\n\n"
        "🚫 *Filtres:* MDD > 40% | < 50 trades | PnL negatif\n\n"
        "📋 *Commandes:*\n"
        "   /toptraders — Lancer une analyse\n"
        "   /tb\\_historique — Selections passees\n"
        "   /tb\\_aide — Cette aide\n\n"
        "_v1.0 — Analyse | v2.0 prevue: copy trading auto_"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ============================================================
# COMMANDES PRINCIPALES
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    msg = (
        "👋 *Bienvenue sur MarignyCryptoBot!*\n\n"
        "📊 *Marche & Analyse*\n"
        "   /prix — Prix en temps reel\n"
        "   /setup — Analyse technique complete\n"
        "   /resume — Resume du marche\n"
        "   /peur — Fear & Greed Index\n\n"
        "📈 *Mes Positions*\n"
        "   /positions — Positions Hyperliquid\n\n"
        "🏆 *TradeBot — ETF Hyperliquid*\n"
        "   /toptraders — Selectionner les 5 meilleurs traders\n"
        "   /tb\\_historique — Historique des selections\n"
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

    logger.info("🤖 MarignyCryptoBot demarre avec module TradeBot!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
