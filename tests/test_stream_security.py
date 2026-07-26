"""stream.py の認証ガードの回帰テスト。

本サーバは自室のカメラ映像を配信するため、到達できる者は室内を覗ける。
認証が唯一の防御であり、その fail-open を防ぐことが要点。

stream.py は cv2 をモジュール読込時に import し、カメラも初期化し得るため、
重い依存はスタブに差し替えてから import する。
"""
import importlib
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TOKEN = "stream-test-token-abc123"


class _AnyAttr(types.ModuleType):
    """未知の属性を呼び出し可能なダミーとして返すモジュールスタブ。

    cv2 の API を個別に列挙すると、stream.py が新しい関数を使い始めるたびに
    テストが AttributeError で落ちる (実際に落ちた)。認証の検証が目的なので、
    画像処理系は一律ダミーで足りる。
    """

    def __getattr__(self, name):
        if name.isupper():   # CAP_PROP_* 等の定数
            return 0
        return lambda *a, **k: _AnyAttr("dummy")


def _install_cv2_stub(monkeypatch):
    """cv2 をスタブ化する (実カメラを開かせない)。"""
    monkeypatch.setitem(sys.modules, "cv2", _AnyAttr("cv2"))


@pytest.fixture
def stream(monkeypatch, tmp_path):
    """STREAM_TOKEN を設定した状態で stream モジュールを読み込む。"""
    _install_cv2_stub(monkeypatch)
    monkeypatch.setenv("STREAM_TOKEN", TOKEN)
    monkeypatch.setenv("ENABLE_YOLO", "0")
    monkeypatch.setenv("EVENT_DIR", str(tmp_path / "events"))

    import stream as module

    importlib.reload(module)
    module.app.config["TESTING"] = True
    return module


@pytest.fixture
def client(stream):
    return stream.app.test_client()


ALL_ROUTES = ["/", "/video_feed", "/snapshot", "/api/status"]


# ---------------------------------------------------------------------------
# 認証
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ALL_ROUTES)
def test_no_token_rejected(client, path):
    """トークン無しは全ルートで 401。"""
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", ALL_ROUTES)
def test_wrong_token_rejected(client, path):
    assert client.get(f"{path}?token=wrong").status_code == 401


def test_query_token_accepted(client):
    assert client.get(f"/?token={TOKEN}").status_code == 200


def test_header_token_accepted(client):
    response = client.get("/api/status", headers={"X-Stream-Token": TOKEN})
    assert response.status_code == 200


def test_cookie_token_accepted(client):
    client.set_cookie("stream_token", TOKEN)
    assert client.get("/api/status").status_code == 200


# ---------------------------------------------------------------------------
# fail-closed (旧実装の最大の穴)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ALL_ROUTES)
def test_unset_token_fails_closed(monkeypatch, tmp_path, path):
    """STREAM_TOKEN 未設定は「認証なしで全公開」ではなく 503。

    旧実装は is_authorized() が True を返し、既定の 0.0.0.0 bind と相まって
    同一ネットワークの誰でもカメラ映像を視聴できた。
    """
    _install_cv2_stub(monkeypatch)
    monkeypatch.delenv("STREAM_TOKEN", raising=False)
    monkeypatch.setenv("ENABLE_YOLO", "0")
    monkeypatch.setenv("EVENT_DIR", str(tmp_path / "events"))
    import stream as module

    importlib.reload(module)
    module.app.config["TESTING"] = True

    response = module.app.test_client().get(path)

    assert response.status_code == 503, "未設定時に配信されている"


def test_unset_token_is_not_authorized(monkeypatch, tmp_path):
    """is_authorized() 単体でも未設定時に True を返さない。"""
    _install_cv2_stub(monkeypatch)
    monkeypatch.delenv("STREAM_TOKEN", raising=False)
    monkeypatch.setenv("ENABLE_YOLO", "0")
    monkeypatch.setenv("EVENT_DIR", str(tmp_path / "events"))
    import stream as module

    importlib.reload(module)

    with module.app.test_request_context("/?token=anything"):
        assert module.is_authorized() is False


# ---------------------------------------------------------------------------
# bind アドレス
# ---------------------------------------------------------------------------

def test_default_host_is_loopback(stream):
    """既定の bind はループバック。LAN 公開は明示的なオプトインにする。"""
    assert stream.HOST == "127.0.0.1"


def test_host_can_be_opted_into_lan(monkeypatch, tmp_path):
    """明示指定すれば従来どおり 0.0.0.0 で待ち受けられる。"""
    _install_cv2_stub(monkeypatch)
    monkeypatch.setenv("STREAM_TOKEN", TOKEN)
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("ENABLE_YOLO", "0")
    monkeypatch.setenv("EVENT_DIR", str(tmp_path / "events"))
    import stream as module

    importlib.reload(module)

    assert module.HOST == "0.0.0.0"


# ---------------------------------------------------------------------------
# トークンの露出低減
# ---------------------------------------------------------------------------

def test_index_sets_httponly_cookie(client):
    """認証成功時に Cookie を発行する (以後 URL にトークンを載せないため)。"""
    response = client.get(f"/?token={TOKEN}")

    cookie = response.headers.get("Set-Cookie", "")
    assert "stream_token=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie


def test_index_html_has_no_token_in_urls(client):
    """埋め込みリソースの URL にトークンが載らない。

    クエリ文字列はアクセスログ・ブラウザ履歴に残るため、video_feed /
    snapshot のすべてに載せるのは避ける。
    """
    body = client.get(f"/?token={TOKEN}").get_data(as_text=True)

    assert 'src="/video_feed"' in body
    assert TOKEN not in body, "HTML 本文にトークンが埋め込まれている"


def test_index_sets_referrer_policy(client):
    response = client.get(f"/?token={TOKEN}")
    assert response.headers.get("Referrer-Policy") == "no-referrer"


# ---------------------------------------------------------------------------
# 定数時間比較
# ---------------------------------------------------------------------------

def test_token_compare_is_constant_time(stream, monkeypatch):
    """比較に secrets.compare_digest を使っている (== ではない)。"""
    calls = []
    real = stream.secrets.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(stream.secrets, "compare_digest", spy)
    stream._token_matches("something")

    assert calls, "compare_digest が使われていない"


def test_empty_presented_token_rejected(stream):
    assert stream._token_matches("") is False
