import os
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from dotenv import load_dotenv
load_dotenv()
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Hello! Your bot is working.")

async def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("No TELEGRAM_TOKEN set")

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))

    me = await app.bot.get_me()
    print("🤖 Bot connected as:", me.username)

    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
