import os
import re
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from faster_whisper import WhisperModel
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USERS = {
    int(x.strip())
    for x in os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",")
    if x.strip()
}

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN がありません。~/hermes-whisper/.env を確認してください。")

model = WhisperModel(
    "small",
    device="cpu",
    compute_type="int8"
)

def allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


# 拡張子ホワイトリスト。それ以外は無視して固定の拡張子にする。
_SAFE_SUFFIXES = {".ogg", ".oga", ".mp3", ".m4a", ".wav", ".flac", ".webm", ".mp4"}


def _safe_local_path(tmpdir: Path, sender_filename: str | None, default: str) -> Path:
    """送信者が指定したファイル名を、パス区切りを一切含まない安全な名前に変える。

    Telegram の document.file_name / audio.file_name は送信者が自由に設定できる。
    従来は `tmpdir / (file_name or default)` としており、file_name に
    "../../etc/cron.d/evil" のような値を入れると tmpdir の外へ書き込めた
    (許可ユーザーからの入力とはいえ、認可された相手が発行元だからといって
    パス検証が要らなくなるわけではない)。

    拡張子だけをホワイトリストから拾い、ファイル名本体は固定にする。
    """
    suffix = Path(sender_filename or "").suffix.lower()
    if suffix not in _SAFE_SUFFIXES:
        suffix = Path(default).suffix or ".bin"
    return tmpdir / f"input{suffix}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "音声メッセージ、m4a、mp3、wavを送ってください。文字起こしします。"
    )

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not allowed(user_id):
        await update.message.reply_text(f"このユーザーIDは許可されていません: {user_id}")
        return

    msg = update.message
    await msg.reply_text("文字起こししています。")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        if msg.voice:
            tg_file = await msg.voice.get_file()
            audio_path = tmpdir / "voice.ogg"
        elif msg.audio:
            tg_file = await msg.audio.get_file()
            audio_path = _safe_local_path(tmpdir, msg.audio.file_name, "audio.mp3")
        elif msg.document:
            tg_file = await msg.document.get_file()
            audio_path = _safe_local_path(tmpdir, msg.document.file_name, "audio_file.bin")
        else:
            await msg.reply_text("音声ファイルを送ってください。")
            return

        await tg_file.download_to_drive(custom_path=str(audio_path))

        segments, info = model.transcribe(
            str(audio_path),
            language="ja",
            beam_size=5
        )

        text = "\n".join(seg.text.strip() for seg in segments).strip()

    if not text:
        text = "文字起こしできませんでした。音量やファイル形式を確認してください。"

    if len(text) <= 3500:
        await msg.reply_text(text)
    else:
        for i in range(0, len(text), 3500):
            await msg.reply_text(text[i:i+3500])

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.Document.ALL, handle_audio))

    print("Telegram faster-whisper bot started.")
    app.run_polling()

if __name__ == "__main__":
    main()


