"""safe_api.py のセキュリティガードの回帰テスト。

旧版の「安全」は次の3点で成立していなかった:
  - 認証が無い
  - 許可リストに python が入っている (= 任意コード実行)
  - 禁止語の部分一致 (引用符で回避可能、かつ引数のパスを一切見ていない)
"""
import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TOKEN = "test-token-abc123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def safe_api(tmp_path, monkeypatch):
    """ワークスペースを tmp に向けた safe_api モジュールを読み込む。"""
    monkeypatch.setenv("HERMES_WORKSPACE", str(tmp_path / "work"))
    monkeypatch.setenv("HERMES_API_TOKEN", TOKEN)
    import safe_api as module

    importlib.reload(module)
    return module


@pytest.fixture
def client(safe_api):
    return TestClient(safe_api.app)


def _run(client, command):
    return client.post("/run", json={"command": command}, headers=AUTH).json()


# ---------------------------------------------------------------------------
# 認証
# ---------------------------------------------------------------------------

def test_requires_auth(client):
    response = client.post("/run", json={"command": "pwd"})
    assert response.status_code == 401


def test_wrong_token_rejected(client):
    response = client.post(
        "/run", json={"command": "pwd"}, headers={"Authorization": "Bearer wrong"}
    )
    assert response.status_code == 401


def test_missing_token_config_fails_closed(tmp_path, monkeypatch):
    """トークン未設定は全開放ではなく 503。"""
    monkeypatch.setenv("HERMES_WORKSPACE", str(tmp_path / "work"))
    monkeypatch.delenv("HERMES_API_TOKEN", raising=False)
    import safe_api as module

    importlib.reload(module)

    response = TestClient(module.app).post("/run", json={"command": "pwd"}, headers=AUTH)

    assert response.status_code == 503


# ---------------------------------------------------------------------------
# インタプリタ (旧 ALLOWED の最大の穴)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "python -c 'print(1)'",
    "python3 -c 'print(1)'",
    "sh -c id",
    "bash -c id",
    "perl -e 'print 1'",
    "ruby -e 'puts 1'",
    "node -e 'console.log(1)'",
    "env python3 -c 'print(1)'",
    "xargs sh -c id",
    "awk 'BEGIN{system(\"id\")}'",
    "sed -i s/a/b/ f.txt",
])
def test_interpreters_and_launchers_blocked(client, command):
    """インタプリタとランチャは許可リストに無い。

    旧版は ALLOWED に 'python' が入っており、python -c で任意コード実行できた。
    """
    result = _run(client, command)
    assert result["ok"] is False
    assert "blocked command" in result["error"] or "シェル演算子" in result["error"]


# ---------------------------------------------------------------------------
# パス封じ込め (旧版は引数を一切見ていなかった)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "cat ../../../etc/passwd",
    "cat /etc/passwd",
    "ls /",
    "find / -name id_rsa",
    "cat ~/.ssh/id_rsa",
    "grep -r secret /Users",
])
def test_paths_outside_workspace_rejected(client, command):
    result = _run(client, command)
    assert result["ok"] is False
    assert "ワークスペース外" in result["error"] or "ホームディレクトリ" in result["error"]


def test_quote_evasion_of_old_denylist_rejected(client):
    """旧版の部分一致禁止語は引用符で回避できた。パス検査は回避できない。

    ".ss''h" は文字列 ".ssh" を含まないため旧チェックを素通りし、shlex 展開後は
    ~/.ssh になった。
    """
    result = _run(client, "cat ~/.ss''h/id_rsa")

    assert result["ok"] is False


def test_legitimate_word_containing_blocked_substring_allowed(client, safe_api):
    """旧版は 'firmware' が 'rm' を含むだけで弾いていた。誤検知を無くす。"""
    (safe_api.WORKSPACE / "firmware.txt").write_text("hello\n")

    result = _run(client, "cat firmware.txt")

    assert result["ok"] is True
    assert "hello" in result["stdout"]


# ---------------------------------------------------------------------------
# 正常系
# ---------------------------------------------------------------------------

def test_pwd_allowed(client, safe_api):
    result = _run(client, "pwd")
    assert result["ok"] is True
    assert str(safe_api.WORKSPACE) in result["stdout"]


def test_ls_and_cat_in_workspace(client, safe_api):
    (safe_api.WORKSPACE / "note.txt").write_text("content here\n")

    listing = _run(client, "ls")
    assert listing["ok"] is True
    assert "note.txt" in listing["stdout"]

    body = _run(client, "cat note.txt")
    assert body["ok"] is True
    assert "content here" in body["stdout"]


def test_grep_pattern_with_slash_allowed(client, safe_api):
    """スラッシュを含むパターンでも、ワークスペース内に収まる限り通る。"""
    (safe_api.WORKSPACE / "f.txt").write_text("a/b\n")

    result = _run(client, "grep a/b f.txt")

    assert result["ok"] is True
    assert "a/b" in result["stdout"]


def test_escaping_grep_pattern_rejected(client, safe_api):
    """既知の制約: '../' を含むパターンはパスとみなして拒否する。

    パターンとパスを引数位置だけで区別できないため、外へ出る形のものは
    fail-closed 側に倒す。
    """
    (safe_api.WORKSPACE / "f.txt").write_text("x\n")

    result = _run(client, "grep ../../etc f.txt")

    assert result["ok"] is False
    assert "ワークスペース外" in result["error"]


def test_subdirectory_access_allowed(client, safe_api):
    """正常系: ワークスペース内のサブディレクトリは辿れる。"""
    sub = safe_api.WORKSPACE / "sub"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "deep.txt").write_text("deep value\n")

    result = _run(client, "cat sub/deep.txt")

    assert result["ok"] is True
    assert "deep value" in result["stdout"]


# ---------------------------------------------------------------------------
# シェル演算子 / 環境変数
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", ["ls ; id", "ls && id", "ls | grep x", "ls > out.txt"])
def test_shell_operators_rejected(client, command):
    result = _run(client, command)
    assert result["ok"] is False


def test_env_is_scrubbed(safe_api, monkeypatch):
    """親プロセスの API キーはサブプロセスへ渡さない。"""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secret")

    env = safe_api._subprocess_env()

    assert "GROQ_API_KEY" not in env
    assert "PATH" in env


def test_subprocess_receives_scrubbed_env(client, safe_api, monkeypatch):
    """回帰: 実行時に env= が渡っている (配線を固定する)。"""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secret")
    captured: dict = {}
    real_run = safe_api.subprocess.run

    def spy(argv, **kwargs):
        captured.update(kwargs)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(safe_api.subprocess, "run", spy)
    _run(client, "pwd")

    assert "env" in captured, "env= が渡されていない"
    assert "GROQ_API_KEY" not in captured["env"]
    assert captured.get("timeout"), "timeout が設定されていない"
