"""api.py のセキュリティガードの回帰テスト。

本 API はエージェント実行・ファイル書込・パッチ適用を提供する。到達できる者は
ホスト上でコードを動かせるのと同義なので、認証と封じ込めが崩れていないことを
経路ごとに固定する。
"""
import ast
import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TOKEN = "test-token-abc123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def api(tmp_path, monkeypatch):
    """プロジェクトルートを tmp に向けた api モジュールを読み込む。"""
    monkeypatch.setenv("HERMES_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_API_TOKEN", TOKEN)
    import api as api_module

    importlib.reload(api_module)
    return api_module


@pytest.fixture
def client(api):
    return TestClient(api.app)


# ---------------------------------------------------------------------------
# 認証
# ---------------------------------------------------------------------------

ALL_ROUTES = [
    ("GET", "/v1/models"),
    ("POST", "/run"),
    ("POST", "/tool/hermes"),
    ("POST", "/tool/self_improve"),
    ("POST", "/tool/apply_readme_improvement"),
    ("POST", "/tool/propose_patch"),
    ("POST", "/tool/save_patch"),
    ("POST", "/tool/apply_patch"),
    ("POST", "/tool/update_readme_section"),
    ("POST", "/agent/auto_loop"),
    ("POST", "/agent/auto_loop2"),
    ("POST", "/tool/inspect_python_file"),
    ("POST", "/tool/propose_code_improvement"),
    ("POST", "/tool/backup_file"),
    ("POST", "/tool/restore_backup"),
    ("POST", "/tool/append_python_note"),
    ("POST", "/tool/check_python_syntax"),
    ("POST", "/tool/safe_apply_patch"),
]


@pytest.mark.parametrize("method,path", ALL_ROUTES)
def test_every_route_requires_auth(client, method, path):
    """トークン無しでは全 route が 401。route ごとの付け忘れが無いことを固定する。"""
    response = client.request(method, path, json={})
    assert response.status_code == 401


@pytest.mark.parametrize("method,path", ALL_ROUTES)
def test_wrong_token_rejected(client, method, path):
    response = client.request(method, path, json={}, headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_missing_token_config_fails_closed(tmp_path, monkeypatch):
    """トークン未設定は「認証なしで全開放」ではなく 503 で停止する。"""
    monkeypatch.setenv("HERMES_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("HERMES_API_TOKEN", raising=False)
    import api as api_module

    importlib.reload(api_module)

    response = TestClient(api_module.app).get("/v1/models", headers=AUTH)

    assert response.status_code == 503


def test_valid_token_allowed(client):
    response = client.get("/v1/models", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "hermes-agent2"


# ---------------------------------------------------------------------------
# パス封じ込め
# ---------------------------------------------------------------------------

PATH_TOOLS = [
    "/tool/inspect_python_file",
    "/tool/backup_file",
    "/tool/append_python_note",
    "/tool/check_python_syntax",
]


@pytest.mark.parametrize("path", PATH_TOOLS)
def test_traversal_rejected(client, path):
    """../ でプロジェクト外へ出られない。"""
    response = client.post(
        path, json={"target_file": "../../../etc/passwd", "note": "x"}, headers=AUTH
    )
    assert response.json()["ok"] is False
    assert "project外" in response.json()["error"]


@pytest.mark.parametrize("path", PATH_TOOLS)
def test_sibling_directory_rejected(client, api, path):
    """前方一致バグの回帰: <project>-evil を配下と誤認しない。

    従来の str(p).startswith(str(base)) はこれを通していた。
    """
    base = api.BASE_DIR
    evil_dir = base.parent / (base.name + "-evil")
    evil_dir.mkdir(parents=True, exist_ok=True)
    (evil_dir / "x.py").write_text("SECRET = 1\n")

    response = client.post(
        path,
        json={"target_file": f"../{evil_dir.name}/x.py", "note": "x"},
        headers=AUTH,
    )

    assert response.json()["ok"] is False
    assert "project外" in response.json()["error"]


def test_inspect_allows_project_file(client, api):
    """正常系: プロジェクト内の .py は読める。"""
    (api.BASE_DIR / "mod.py").write_text("VALUE = 1\n")

    response = client.post("/tool/inspect_python_file", json={"target_file": "mod.py"}, headers=AUTH)

    assert response.json()["ok"] is True
    assert "VALUE = 1" in response.json()["preview"]


# ---------------------------------------------------------------------------
# .py へのコード注入
# ---------------------------------------------------------------------------

def _module_level_assignments(source: str) -> set:
    """モジュール直下で代入される名前を静的に集める。"""
    names = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def test_note_with_newline_cannot_inject_code(client, api):
    """改行入りの note でも実行コードにならない。

    従来は `# - {note}` と1行で組み立てていたため、note の2行目以降が
    そのまま実行される Python コードになった (認証も無かった)。
    追記後のファイルを構文解析し、注入を意図した代入が**コードとして存在しない**
    ことを確認する。
    """
    target = api.BASE_DIR / "mod.py"
    target.write_text("VALUE = 1\n")
    injected = "メモ\nINJECTED_MARKER = 1\nVALUE = 999"

    response = client.post(
        "/tool/append_python_note",
        json={"target_file": "mod.py", "note": injected},
        headers=AUTH,
    )

    assert response.json()["ok"] is True
    written = target.read_text()
    assert "INJECTED_MARKER" in written, "追記自体はされるはず"

    assignments = _module_level_assignments(written)
    assert "INJECTED_MARKER" not in assignments, "注入行がコードになっている"
    assert assignments == {"VALUE"}

    # 注入を狙った行が全てコメントに留まっていること。
    for line in written.splitlines():
        if "INJECTED_MARKER" in line or "VALUE = 999" in line:
            assert line.lstrip().startswith("#"), f"コメント化されていない: {line!r}"


def test_note_appends_normally(client, api):
    """正常系: 単一行のメモは従来通り追記される。"""
    target = api.BASE_DIR / "mod.py"
    target.write_text("VALUE = 1\n")

    response = client.post(
        "/tool/append_python_note",
        json={"target_file": "mod.py", "note": "改善メモ"},
        headers=AUTH,
    )

    assert response.json()["ok"] is True
    assert "# - 改善メモ" in target.read_text()


# ---------------------------------------------------------------------------
# patch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("patch_body", [
    "--- a/../../../etc/cron.d/evil\n+++ b/../../../etc/cron.d/evil\n@@\n+evil\n",
    "--- /etc/passwd\n+++ /etc/passwd\n@@\n+evil\n",
    "--- a/../outside.py\n+++ b/../outside.py\n@@\n+evil\n",
])
def test_patch_targeting_outside_rejected(client, patch_body):
    """patch 本文が指す書込先を検証する (置き場所の制限だけでは不十分)。"""
    response = client.post(
        "/tool/save_patch", json={"patch": patch_body, "target_file": "x"}, headers=AUTH
    )

    assert response.json()["ok"] is False


def test_patch_without_header_rejected(client):
    response = client.post("/tool/save_patch", json={"patch": "just text\n"}, headers=AUTH)
    assert response.json()["ok"] is False
    assert "ヘッダ" in response.json()["error"]


def test_valid_patch_saved(client, api):
    """正常系: プロジェクト内を対象にした patch は保存できる。"""
    body = "--- a/README.md\n+++ b/README.md\n@@\n+line\n"

    response = client.post("/tool/save_patch", json={"patch": body}, headers=AUTH)

    assert response.json()["ok"] is True
    assert Path(response.json()["path"]).is_relative_to(api.PATCHES_DIR)


def test_apply_patch_outside_patches_dir_rejected(client, api):
    """patches/ 以外に置かれたファイルは適用できない。"""
    stray = api.BASE_DIR / "stray.patch"
    stray.write_text("--- a/README.md\n+++ b/README.md\n@@\n+x\n")

    response = client.post(
        "/tool/apply_patch", json={"patch_path": "stray.patch"}, headers=AUTH
    )

    assert response.json()["ok"] is False
    assert "patches" in response.json()["error"]


def test_apply_patch_absolute_path_rejected(client):
    response = client.post(
        "/tool/apply_patch", json={"patch_path": "/etc/passwd"}, headers=AUTH
    )
    assert response.json()["ok"] is False


# ---------------------------------------------------------------------------
# ロールバック (従来は NameError で機能していなかった)
# ---------------------------------------------------------------------------

def test_restore_backup_is_defined(api):
    """safe_apply_patch が呼ぶ restore_backup が存在する。"""
    assert hasattr(api, "restore_backup")


def test_restore_backup_rejects_outside_source(client, api):
    """backups/ 以外を復元元にできない。"""
    (api.BASE_DIR / "mod.py").write_text("VALUE = 1\n")
    (api.BASE_DIR / "evil.txt").write_text("PWNED\n")

    response = client.post(
        "/tool/restore_backup",
        json={"backup_path": "evil.txt", "target_file": "mod.py"},
        headers=AUTH,
    )

    assert response.json()["ok"] is False
    assert (api.BASE_DIR / "mod.py").read_text() == "VALUE = 1\n"


def test_safe_apply_patch_rolls_back_on_syntax_error(client, api):
    """構文が壊れる patch は適用後にロールバックされる。

    従来は restore_backup が未定義で NameError となり、壊れたまま残っていた。
    """
    target = api.BASE_DIR / "mod.py"
    target.write_text("x = 1\n")
    api.PATCHES_DIR.mkdir(parents=True, exist_ok=True)
    (api.PATCHES_DIR / "bad.patch").write_text(
        "--- a/mod.py\n+++ b/mod.py\n@@ -1 +1 @@\n-x = 1\n+def (\n"
    )

    response = client.post(
        "/tool/safe_apply_patch",
        json={"patch_path": "patches/bad.patch", "target_file": "mod.py"},
        headers=AUTH,
    ).json()

    assert response["ok"] is False
    assert response["stage"] == "syntax_check"
    assert response["restored"]["ok"] is True
    assert target.read_text() == "x = 1\n", "ロールバックされていない"
