"""telegram_whisper_bot.py のパストラバーサル修正の回帰テスト。

telegram_whisper_bot.py は WhisperModel をモジュール読込時にロードする
(実モデルの初期化は重く、テストには不要) ため、faster_whisper をスタブに
差し替えてから import する。

注意: _safe_local_path 単体のテストだけでは、handle_audio が実際にそれを
呼んでいるかどうかは検証できない (現に、呼び出し側だけを旧コードに戻しても
単体テストは全て緑のままだった)。ヘルパの単体テストに加えて、
handle_audio 経由の配線テストを別に用意する。
"""
import asyncio
import importlib
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def bot(monkeypatch):
    """faster_whisper をスタブ化した状態で telegram_whisper_bot を読み込む。"""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:test-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "111")

    stub = types.ModuleType("faster_whisper")

    class _StubWhisperModel:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, *args, **kwargs):
            return [], types.SimpleNamespace()

    stub.WhisperModel = _StubWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", stub)

    import telegram_whisper_bot as module

    importlib.reload(module)
    return module


def _make_update(*, user_id=111, document_filename=None):
    """document 添付を持つ最小限の Update モックを作る。"""
    tg_file = MagicMock()
    tg_file.download_to_drive = AsyncMock()

    document = MagicMock()
    document.file_name = document_filename
    document.get_file = AsyncMock(return_value=tg_file)

    message = MagicMock()
    message.voice = None
    message.audio = None
    message.document = document
    message.reply_text = AsyncMock()

    update = MagicMock()
    update.message = message
    update.effective_user.id = user_id
    return update, tg_file


@pytest.mark.parametrize("sender_filename", [
    "../../../../etc/cron.d/evil",
    "../../.ssh/authorized_keys",
    "/etc/passwd",
    "..%2f..%2fetc%2fpasswd",
    "sub/dir/evil.mp3",
])
def test_traversal_filename_stays_inside_tmpdir(bot, tmp_path, sender_filename):
    """送信者が指定した危険なファイル名でも tmpdir の外へは出ない。

    従来は `tmpdir / (file_name or default)` としており、file_name に
    '../../etc/cron.d/evil' のような値を入れると tmpdir の外へ書き込めた。
    許可ユーザー (ALLOWED_USERS) からの入力であっても、パス検証が不要には
    ならない (電話の乗っ取り・アカウント誤登録等で前提が崩れ得るため)。
    """
    result = bot._safe_local_path(tmp_path, sender_filename, "audio.mp3")

    assert result.parent == tmp_path
    assert result.is_relative_to(tmp_path)
    assert "/" not in result.name
    assert ".." not in result.parts


def test_old_construction_would_have_escaped(tmp_path):
    """比較用: 修正前の単純結合だと実際に tmpdir の外を指していたことを示す。"""
    sender_filename = "../../../../../../etc/cron.d/evil"
    old_style = tmp_path / (sender_filename or "audio.mp3")

    assert not old_style.resolve().is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize("sender_filename,expected_suffix", [
    ("voice_memo.mp3", ".mp3"),
    ("recording.WAV", ".wav"),
    ("clip.m4a", ".m4a"),
    (None, ".mp3"),
    ("", ".mp3"),
    ("no_extension_at_all", ".mp3"),
    ("payload.exe", ".mp3"),  # ホワイトリスト外の拡張子はデフォルトに落ちる
])
def test_extension_whitelisted_or_defaulted(bot, tmp_path, sender_filename, expected_suffix):
    result = bot._safe_local_path(tmp_path, sender_filename, "audio.mp3")
    assert result.suffix == expected_suffix


def test_normal_filename_preserved_extension(bot, tmp_path):
    """正常系: 通常のファイル名は拡張子を保持したまま安全に使われる。"""
    result = bot._safe_local_path(tmp_path, "interview.wav", "audio.mp3")
    assert result == tmp_path / "input.wav"


# ---------------------------------------------------------------------------
# 配線テスト: handle_audio が実際に _safe_local_path を経由しているか
# ---------------------------------------------------------------------------

def test_handle_audio_document_writes_inside_tmpdir(bot):
    """handle_audio (document 分岐) がトラバーサルするファイル名でも tmpdir 内に書く。

    _safe_local_path 単体のテストだけでは、呼び出し側 (handle_audio) が
    それを経由しているかまでは分からない。実際に download_to_drive へ渡された
    custom_path を検証することで、配線そのものを固定する。
    """
    update, tg_file = _make_update(document_filename="../../../../etc/cron.d/evil")

    asyncio.run(bot.handle_audio(update, MagicMock()))

    assert tg_file.download_to_drive.await_count == 1
    _, kwargs = tg_file.download_to_drive.await_args
    written_path = Path(kwargs["custom_path"])
    assert ".." not in written_path.parts
    assert written_path.name == "input.bin"


def test_handle_audio_rejects_disallowed_user(bot):
    """許可されていない user_id では download_to_drive まで到達しない。"""
    update, tg_file = _make_update(user_id=999, document_filename="x.mp3")

    asyncio.run(bot.handle_audio(update, MagicMock()))

    tg_file.download_to_drive.assert_not_called()
