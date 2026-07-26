"""ワークスペース内でのコマンド実行 API。

セキュリティ設計:
    旧版は「安全」を名乗りながら次の3点で成立していなかった。

    1. 認証が無く、誰でもコマンドを実行できた
    2. 許可リストに `python` が入っていた
       — `python -c ...` で任意コード実行になり、許可リスト自体が無意味だった
    3. 禁止語を部分一致で見ていた
       — `cat ~/.ss''h/id_rsa` のように引用符を挟めば素通りし、逆に `firmware`
         のような無関係な語 ("rm" を含む) を誤って弾いた。そもそも引数のパスが
         ワークスペース内かどうかは一切見ていなかった

    方針を「禁止語の列挙」から「許可した読み取り専用コマンドを、ワークスペース
    内のパスに対してのみ実行する」に変える。加えて親環境の秘密を継承させず、
    macOS では sandbox-exec でカーネル境界 (ネットワーク拒否・書込制限・秘密の
    読取り拒否) も重ねる。
"""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI
from pydantic import BaseModel

from hermes_agent2 import sandbox
from hermes_agent2.hermes_constants import (
    SAFE_API_ALLOWED_COMMANDS,
    SAFE_API_BLOCKED_OPERATORS,
)
from hermes_auth import require_token

# 全 route に認証を掛ける (api.py と同じ HERMES_API_TOKEN を使う)。
app = FastAPI(dependencies=[Depends(require_token)])

WORKSPACE = Path(os.getenv("HERMES_WORKSPACE") or (Path.home() / "agent-work")).resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)

# 許可コマンドと拒否する演算子は hermes_constants に置く。agent_client.py が
# LLM へ提示する表と食い違わせないため (経緯は定義側のコメントを参照)。
ALLOWED = SAFE_API_ALLOWED_COMMANDS
BLOCKED_OPERATORS = SAFE_API_BLOCKED_OPERATORS

EXEC_TIMEOUT = 10
MAX_OUTPUT = 100_000

# サブプロセスへ渡す環境変数。親には API キーが入り得るので最小限に絞る。
_ENV_PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")


def _subprocess_env() -> dict:
    env = {"PYTHONIOENCODING": "utf-8"}
    for key in _ENV_PASSTHROUGH:
        value = os.getenv(key)
        if value:
            env[key] = value
    return env


def _looks_like_path(arg: str) -> bool:
    """引数がパスを指していそうか。

    grep のパターン等はパスではないので、区切り文字や ~ / .. を含むものだけを
    パス候補として扱う。判定を広めに取り、疑わしいものは検査対象にする。
    """
    return "/" in arg or arg.startswith("~") or arg == ".." or arg.startswith("../")


def check_args(parts: list[str]) -> Optional[str]:
    """引数のパスがワークスペース内に収まっているか検査する。

    Returns:
        問題があればエラーメッセージ。無ければ None。
    """
    for arg in parts[1:]:
        if arg.startswith("-"):
            continue  # オプション
        if not _looks_like_path(arg):
            continue  # grep のパターン等
        if arg.startswith("~"):
            return f"ホームディレクトリ参照は許可されていません: {arg}"
        candidate = (WORKSPACE / arg).resolve()
        if candidate != WORKSPACE and not candidate.is_relative_to(WORKSPACE):
            return f"ワークスペース外のパスは指定できません: {arg}"
    return None


class Cmd(BaseModel):
    command: str


@app.post("/run")
def run_cmd(cmd: Cmd):
    try:
        parts = shlex.split(cmd.command)
    except ValueError as exc:
        return {"ok": False, "error": f"コマンド解析エラー: {exc}"}

    if not parts:
        return {"ok": False, "error": "empty command"}

    # シェルは通さないので、連結演算子やリダイレクトは黙って引数に化ける。
    # 意図が曖昧なまま実行するより fail-closed にする。
    for token in parts:
        if token in BLOCKED_OPERATORS:
            return {"ok": False, "error": f"シェル演算子は使用できません: {token}"}

    name = os.path.basename(parts[0])
    if name not in ALLOWED:
        return {"ok": False, "error": f"blocked command: {parts[0]}"}

    problem = check_args(parts)
    if problem:
        return {"ok": False, "error": problem}

    # OS レベル隔離 (macOS のみ)。Linux では None が返り素の argv になるため、
    # 実質的な封じ込めは上の許可リストとパス検査が担う。
    argv = sandbox.wrap(parts, WORKSPACE) or parts

    try:
        result = subprocess.run(
            argv,
            cwd=str(WORKSPACE),
            capture_output=True,
            text=True,
            timeout=EXEC_TIMEOUT,
            env=_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"タイムアウト ({EXEC_TIMEOUT}秒)"}
    except FileNotFoundError:
        return {"ok": False, "error": f"command not found: {parts[0]}"}

    return {
        "ok": result.returncode == 0,
        "command": cmd.command,
        "cwd": str(WORKSPACE),
        "stdout": (result.stdout or "")[:MAX_OUTPUT],
        "stderr": (result.stderr or "")[:MAX_OUTPUT],
        "returncode": result.returncode,
    }


if __name__ == "__main__":
    import uvicorn

    # 既定はループバック。外部公開は明示的なオプトインにする。
    uvicorn.run(
        app,
        host=os.getenv("HERMES_API_HOST", "127.0.0.1"),
        port=int(os.getenv("HERMES_SAFE_API_PORT", "8001")),
    )
