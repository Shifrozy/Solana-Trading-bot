# sol_trading_bot.py
# Basic SOL trading bot with Telegram control (starter).
# WARNING: Use DRY_RUN=True until you fully test.

import os
import asyncio
import logging
import base64
import json
import time
from decimal import Decimal
import requests
import nest_asyncio
nest_asyncio.apply()  
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from dotenv import load_dotenv
load_dotenv()
# Optional: jupiter-python-sdk wrapper (recommended if installed)
try:
    from jupiter_python_sdk.jupiter import Jupiter
    HAVE_JUPITER_SDK = True
except Exception:
    HAVE_JUPITER_SDK = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sol-bot")

# CONFIG (edit or set via environment variables)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
RPC_URL = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
PRIVATE_KEY_JSON = os.getenv("PRIVATE_KEY_JSON")     # path to keypair json (array of ints)
PRIVATE_KEY_B58 = os.getenv("PRIVATE_KEY_B58")       # alt: base58-encoded private key bytes
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# Default strategy params (you can change via Telegram)
STATE = {
    "buy_drop_pct": 5.0,        # buy when 24h drop >= 5%
    "take_profit_pct": 2.0,     # sell when profit >= 2% (example)
    "holding": False,
    "last_buy_price": None,
    "position_amount_sol": 0.0
}

# Token mints (mainnet)
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
JUP_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUP_SWAP_API = "https://quote-api.jup.ag/v6/swap"

# Helper: load keypair
def load_keypair():
    if PRIVATE_KEY_B58:
        import base58
        raw = base58.b58decode(PRIVATE_KEY_B58)
        return Keypair.from_bytes(raw)
    if PRIVATE_KEY_JSON and os.path.exists(PRIVATE_KEY_JSON):
        arr = json.load(open(PRIVATE_KEY_JSON, "r"))
        secret = bytes(arr)
        return Keypair.from_bytes(secret)
    raise RuntimeError("No PRIVATE_KEY configured. Set PRIVATE_KEY_JSON or PRIVATE_KEY_B58")

# Price / 24h change (CoinGecko)
def fetch_sol_24h_change_and_price():
    params = {
        "vs_currency": "usd",
        "ids": "solana",
        "order": "market_cap_desc",
        "per_page": 1,
        "page": 1,
        "sparkline": "false",
        "price_change_percentage": "24h"
    }
    r = requests.get(COINGECKO_MARKETS, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    if not data:
        raise RuntimeError("CoinGecko returned empty")
    d = data[0]
    # field names: current_price, price_change_percentage_24h (CoinGecko)
    price = Decimal(str(d.get("current_price")))
    pct24 = Decimal(str(d.get("price_change_percentage_24h") or 0.0))
    return float(price), float(pct24)

# Build a Jupiter quote (example: buy SOL with USDC)
def get_jupiter_quote(amount_in_native_int):
    params = {
        "inputMint": USDC_MINT,
        "outputMint": WSOL_MINT,
        "amount": str(amount_in_native_int),   # amount in smallest units (USDC has 6 decimals)
        "slippageBps": "100"                   # 100 = 1% slippage BPS
    }
    r = requests.get(JUP_QUOTE_API, params=params, timeout=10)
    r.raise_for_status()
    return r.json()

# Build and request the swap from Jupiter (returns base64 unsigned transaction)
def request_jupiter_swap(quote_response, user_pubkey_str):
    body = {"quoteResponse": quote_response, "userPublicKey": user_pubkey_str}
    r = requests.post(JUP_SWAP_API, json=body, timeout=15)
    r.raise_for_status()
    return r.json()

# HIGH LEVEL: Buy function (example uses Jupiter swap API — advanced)
async def buy_with_usdc(async_client, keypair, usdc_amount):
    logger.info("BUY requested: USDC amount = %s (this is the amount token units, e.g., 1 USDC => 1)", usdc_amount)
    if DRY_RUN:
        logger.info("DRY_RUN is enabled — not executing on-chain. Simulating buy.")
        return {"simulated": True, "usdc": usdc_amount}

    # Convert USDC amount to smallest units (USDC usually 6 decimals)
    amount_int = int(Decimal(usdc_amount) * (10 ** 6))
    quote = get_jupiter_quote(amount_int)
    if not quote.get("data"):
        raise RuntimeError("No quote data returned from Jupiter")
    # Option A: if you installed jupiter sdk you can use it (recommended)
    if HAVE_JUPITER_SDK:
        logger.info("Using jupiter-python-sdk to execute swap (SDK handles signing + sending).")
        jup = Jupiter(async_client, keypair)
        # NOTE: the exact method names in the SDK might differ; check the SDK README.
        # The SDK will generally have methods to get a quote and execute the swap in one call.
        # Example pseudocode (SDK may use different names):
        try:
            res = await jup.swap_from_quote(quote)  # <- placeholder: actual method may be jup.swap or jup.execute_swap
            return res
        except Exception as e:
            logger.exception("SDK swap failed: %s", e)
            raise

    # Option B: manual request via REST swap API (advanced)
    swap_resp = request_jupiter_swap(quote, str(keypair.pubkey()))
    # swap_resp usually includes 'swapTransaction' base64 (unsigned or partially-signed)
    tx_b64 = swap_resp.get("swapTransaction") or swap_resp.get("swapTransactionSerialized")
    if not tx_b64:
        raise RuntimeError("Jupiter /swap did not return a transaction payload")
    tx_bytes = base64.b64decode(tx_b64)

    # Use solders to create VersionedTransaction, sign, serialize, and send
    # This area is advanced: versioned tx deserializing & signing differs across library versions.
    # Below is an outline; you may need to adapt depending on your solana/solders versions.
    from solders.transaction import VersionedTransaction
    from solders.keypair import Keypair as SKeypair
    from solana.rpc.types import TxOpts

    # Deserialize:
    vtx = VersionedTransaction.from_bytes(tx_bytes)
    # NOTE: VersionedTransaction doesn't have .sign() — you need to use the SDK or proper sign helper.
    # If you use solders directly you must add signatures yourself (advanced).
    # Instead, prefer jupiter-python-sdk or use solana-py helpers for signing versioned txns.

    raise RuntimeError("Manual signing of Jupiter versioned txns is advanced. Use jupiter-python-sdk or follow Jupiter docs. See comments in code.")

# SELL is symmetric: swap SOL -> USDC using same endpoints but reversed input/output.

# Telegram command handlers
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Solana trader bot running. Commands: /setbuy <pct>, /settp <pct>, /status, /manualbuy <usdc>, /manualsell")

async def setbuy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        v = float(context.args[0])
        STATE["buy_drop_pct"] = v
        await update.message.reply_text(f"Buy drop set to {v}%")
    except Exception:
        await update.message.reply_text("Usage: /setbuy 5.0  (for 5%)")

async def settp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        v = float(context.args[0])
        STATE["take_profit_pct"] = v
        await update.message.reply_text(f"TP set to {v}%")
    except Exception:
        await update.message.reply_text("Usage: /settp 2.0  (for 2%)")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = json.dumps(STATE, indent=2)
    await update.message.reply_text(f"State:\n{s}")

# Manual buy/sell via Telegram
async def manualbuy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /manualbuy <usdc_amount>")
        return
    usdc_amount = float(context.args[0])
    await update.message.reply_text(f"Scheduled manual buy of {usdc_amount} USDC (dry_run={DRY_RUN}). Running it now...")
    # run an async buy
    keypair = load_keypair()
    async_client = AsyncClient(RPC_URL)
    try:
        res = await buy_with_usdc(async_client, keypair, usdc_amount)
        await update.message.reply_text(f"Buy result: {res}")
    except Exception as e:
        await update.message.reply_text(f"Buy failed: {e}")

async def manualsell_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Manual sell requested. (Implement symmetric to buy_with_usdc)")

# Monitoring background task
async def monitor_task(app):
    keypair = load_keypair()
    async_client = AsyncClient(RPC_URL)
    while True:
        try:
            price, pct24 = fetch_sol_24h_change_and_price()
            logger.info(f"SOL price ${price:.4f}, 24h change {pct24:.2f}%")
            # If not holding and drop exceeds threshold -> buy
            if (not STATE["holding"]) and (pct24 <= -abs(STATE["buy_drop_pct"])):
                logger.info("Condition met to BUY.")
                # Example: buy with 5 USDC (adjust)
                try:
                    res = await buy_with_usdc(async_client, keypair, usdc_amount=5.0)
                    logger.info("Buy response: %s", res)
                    # If buy succeeded, set state (this is simplified)
                    STATE["holding"] = True
                    STATE["last_buy_price"] = price
                    STATE["position_amount_sol"] = 0.0  # set real amount from swap result
                except Exception as e:
                    logger.exception("Buy failed: %s", e)
            # If holding, check TP
            if STATE["holding"] and STATE["last_buy_price"]:
                target = STATE["last_buy_price"] * (1.0 + STATE["take_profit_pct"] / 100.0)
                if price >= target:
                    logger.info("Take profit reached -> SELL.")
                    # call sell flow (not implemented fully here)
                    # ... implement symmetric to buy_with_usdc with WSOL -> USDC
                    STATE["holding"] = False
                    STATE["last_buy_price"] = None
                    STATE["position_amount_sol"] = 0.0
        except Exception as e:
            logger.exception("Monitor loop error: %s", e)
        await asyncio.sleep(60)  # poll every 60s (adjust)

# Main startup
async def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Set TELEGRAM_TOKEN env var")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("setbuy", setbuy_cmd))
    app.add_handler(CommandHandler("settp", settp_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("manualbuy", manualbuy_cmd))
    app.add_handler(CommandHandler("manualsell", manualsell_cmd))

    # start monitor background task
    # app.create_task(monitor_task(app))
    asyncio.create_task(monitor_task(app))
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
