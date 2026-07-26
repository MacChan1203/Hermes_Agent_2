import os
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv(os.path.expanduser("~/.hermes/.env"))

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER = str(os.getenv("TELEGRAM_ALLOWED_USERS"))

# Ollama の接続先とモデル。
# 既定値はホスト直接実行時の従来値。コンテナ内から使う場合は Ollama が
# ホスト側に居るため、OLLAMA_URL に host.docker.internal を指定する
# (Docker Desktop for Mac は Ollama が 127.0.0.1 のみで待ち受けていても
#  host.docker.internal 経由の到達を通すため、ホスト側を 0.0.0.0 に開いて
#  LAN へ晒す必要はない — 実測で確認済み)。
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
MODEL = os.getenv("OLLAMA_MODEL", "qwen3-swallow-8b-64k:latest")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if user_id != ALLOWED_USER:
        await update.message.reply_text("許可されていないユーザーです。")
        return

    user_text = update.message.text

    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL,
                "prompt": user_text,
                "stream": False,
            },
            timeout=120,
        )

        data = response.json()
        answer = data.get("response", "返答を取得できませんでした。")

    except Exception as e:
        answer = f"エラー: {e}"

    await update.message.reply_text(answer)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    print("Telegram Bot 起動中...")
    app.run_polling()


if __name__ == "__main__":
    main()

