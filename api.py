"""Hermes Agent 2 のツール API。

セキュリティ設計:
    本 API はエージェント実行・ファイル書込・パッチ適用を行う。到達できる者は
    ホスト上でコードを動かせるのと同義であり、認証が唯一の外周防御になる。

    - 認証はアプリ全体の依存として掛ける (route ごとの付け忘れを構造的に防ぐ)
    - トークン未設定は「認証なし」ではなく 503 で停止する (fail closed)
    - パス検査は文字列の前方一致ではなく is_relative_to で行う
      (前方一致は <project>-evil を配下と誤認する)
    - patch は patch_path の位置だけでなく、**本文が指す書込先**も検証する
      (-p1 の ../ でプロジェクト外へ抜けられるため)
    - .py へ追記するメモは全行をコメント化する (改行1つで実行コードになる)
"""
from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional
import os
import re
import subprocess
import sys
import time
import uuid
import json
from pathlib import Path
import datetime

from hermes_auth import require_token


# ---------------------------------------------------------------------------
# 配置 (ハードコードされた /home/ubuntu を廃し、実行環境から導出する)
# ---------------------------------------------------------------------------

BASE_DIR = Path(os.getenv("HERMES_PROJECT_DIR") or Path(__file__).resolve().parent).resolve()
MEMORY_DIR = BASE_DIR / "memory"
PATCHES_DIR = BASE_DIR / "patches"
BACKUP_DIR = BASE_DIR / "backups"
README_PATH = BASE_DIR / "README.md"

# run_agent.py を動かすインタプリタ。未指定なら本 API と同じものを使う。
PYTHON_BIN = os.getenv("HERMES_PYTHON") or sys.executable

RUN_HERMES_TIMEOUT = 60
PATCH_TIMEOUT = 60


# 認証は hermes_auth.require_token (safe_api.py と共通) を使う。
# 全 route に認証を掛ける。route ごとに Depends を書く方式は付け忘れが起きる。
# 内部の相互呼び出し (auto_loop -> self_improve 等) は素の dict 呼び出しなので
# 影響を受けない。
app = FastAPI(dependencies=[Depends(require_token)])


# ---------------------------------------------------------------------------
# パス検査
# ---------------------------------------------------------------------------

class PathOutsideProject(ValueError):
    """プロジェクト外を指すパス。"""


def _resolve_in_project(relative: str) -> Path:
    """プロジェクト配下に収まる絶対パスへ解決する。

    従来の `str(p).startswith(str(base))` は前方一致であり、
    /home/ubuntu/hermes-agent2-evil/x.py を配下と誤認した。
    symlink 解決後に is_relative_to で判定する。
    """
    candidate = (BASE_DIR / relative).resolve()
    if candidate != BASE_DIR and not candidate.is_relative_to(BASE_DIR):
        raise PathOutsideProject(f"プロジェクト外のパスです: {relative}")
    return candidate


def _reject(tool: str, message: str) -> dict:
    return {"tool": tool, "ok": False, "error": message}

class Message(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: str = "hermes-agent2"
    messages: List[Message]
    max_tokens: Optional[int] = 4096
    temperature: Optional[float] = 0.7
    stream: Optional[bool] = False

def extract_user_query(messages: List[Message]) -> str:
    for msg in reversed(messages):
        if msg.role == "user":
            return msg.content
    return messages[-1].content if messages else ""

def run_hermes(query: str, max_turns: int = 2) -> str:
    try:
        result = subprocess.run(
            [
                PYTHON_BIN,
                "run_agent.py",
                "--query",
                query,
                "--max_turns",
                str(min(max_turns, 2)),
            ],
            capture_output=True,
            text=True,
            timeout=RUN_HERMES_TIMEOUT,
            cwd=str(BASE_DIR),
        )
        if result.returncode != 0:
            return "Hermes error:\n" + result.stderr
        return result.stdout or "(Hermes returned empty output)"
    except Exception as e:
        return f"Hermes API exception: {type(e).__name__}: {e}"

@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": "hermes-agent2",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "local"
            }
        ]
    }


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionRequest):
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Hermes API 接続テスト成功です。"
                },
                "finish_reason": "stop"
            }
        ],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2
        }
    }



@app.post("/run")
def run_direct(payload: dict):
    query = payload.get("query", "")
    max_turns = int(payload.get("max_turns", 2))
    return {
        "ok": True,
        "stdout": run_hermes(query, max_turns=max_turns)
    }

@app.post("/tool/hermes")
def hermes_tool(payload: dict):
    query = payload.get("query", "")
    max_turns = int(payload.get("max_turns", 2))

    hints = load_recent_improvement_hints(limit=3)

    if hints:
        enhanced_query = (
            f"{query}\n\n"
            f"以下は過去の自己改善ヒントです。今回の実行に反映してください。\n"
            f"{hints}"
        )
    else:
        enhanced_query = query

    result = run_hermes(enhanced_query, max_turns=max_turns)

    save_self_improvement_log(query=query, result=result, ok=True)

    return {
        "tool": "hermes-agent2",
        "ok": True,
        "query": query,
        "used_improvement_hints": hints,
        "result": result
    }


SELF_IMPROVEMENT_LOG = MEMORY_DIR / "self_improvement.jsonl"
AUTO_LOOP_LOG = MEMORY_DIR / "auto_loop_runs.jsonl"

def save_self_improvement_log(query: str, result: str, ok: bool = True):
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    record = {
        "ts": datetime.datetime.now().isoformat(),
        "query": query,
        "ok": ok,
        "result_preview": result[:1000],
        "improvement_hint": extract_improvement_hint(result),
    }

    with SELF_IMPROVEMENT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def extract_improvement_hint(result: str) -> str:
    marker = "[直近の改善ヒント]"
    if marker in result:
        return result.split(marker, 1)[1].split("\n\n", 1)[0].strip()
    return ""

def load_recent_improvement_hints(limit: int = 3) -> str:
    if not SELF_IMPROVEMENT_LOG.exists():
        return ""

    try:
        lines = SELF_IMPROVEMENT_LOG.read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""

    seen = set()
    unique_hints = []

    # 新しい順に読む
    for line in reversed(lines):
        try:
            record = json.loads(line)
            hint = record.get("improvement_hint", "").strip()

            # " - " や余分な記号を除去
            hint = hint.lstrip("- ").strip()

            # 空 or 重複は除外
            if not hint or hint in seen:
                continue

            seen.add(hint)
            unique_hints.append(hint)

        except Exception:
            continue

        if len(unique_hints) >= limit:
            break

    if not unique_hints:
        return ""

    # 古い順に戻して整形
    unique_hints.reverse()

    return "\n".join(f"- {h}" for h in unique_hints)



@app.post("/tool/self_improve")
def self_improve(payload: dict):
    theme = payload.get("theme", "Hermes Agent 2 の改善")
    max_turns = int(payload.get("max_turns", 2))
    hints = load_recent_improvement_hints(limit=5)

    query = (
        f"テーマ: {theme}\n\n"
        f"過去の改善ヒント:\n{hints}\n\n"
        f"上記を踏まえて、次に実装すべき改善案を1つだけ提案してください。"
    )

    result = run_hermes(query, max_turns=max_turns)
    save_self_improvement_log(query=query, result=result, ok=True)

    return {
        "tool": "self_improve",
        "ok": True,
        "theme": theme,
        "used_improvement_hints": hints,
        "result": result
    }

@app.post("/tool/apply_readme_improvement")
def apply_readme_improvement(payload: dict):
    theme = payload.get("theme", "Hermes Agent 2をより使いやすくする")
    readme_path = README_PATH

    hints = load_recent_improvement_hints(limit=5)

    # 「次の改善候補」を固定文で書かない。以前は「README に起動手順、
    # APIエンドポイント、OpenClaw連携方法を追記する」と書き込んでおり、
    # /tool/propose_patch の架空 patch がそれを満たす提案に見えてしまう
    # 対になっていた (コミット 9e2754a の経緯)。候補はヒントログから来る
    # ものだけにし、無ければ無いと書く。
    section = f"""

---

## 自己改善メモ

テーマ: {theme}

過去の改善ヒント:
{hints if hints else "- なし"}
"""

    if not readme_path.exists():
        readme_path.write_text("# Hermes Agent 2\n", encoding="utf-8")

    current = readme_path.read_text(encoding="utf-8")

    if "## 自己改善メモ" not in current:
        readme_path.write_text(current.rstrip() + section + "\n", encoding="utf-8")
        applied = True
    else:
        applied = False

    return {
        "tool": "apply_readme_improvement",
        "ok": True,
        "applied": applied,
        "path": str(readme_path),
        "message": "README.md に自己改善メモを追加しました。" if applied else "README.md には既に自己改善メモがあります。"
    }


@app.post("/tool/propose_patch")
def propose_patch(payload: dict):
    theme = payload.get("theme", "Hermes Agent 2の改善")
    target_file = payload.get("target_file", "README.md")

    if target_file != "README.md":
        return {
            "tool": "propose_patch",
            "ok": False,
            "error": "現在は README.md のpatch提案のみ対応しています。"
        }

    # patch 生成は未実装。以前はここが固定文字列の patch を返していたが、その
    # 中身は実在しない配置 (~/hermes-venv、systemd user service) を前提とした
    # 架空の README 節だった。自己改善ループ (self_improve → propose_patch →
    # apply_patch) がそれを README へ適用し、コミット 9e2754a で削除する必要が
    # 生じた (経緯は README の「APIエンドポイント」節の注記を参照)。
    #
    # theme も target_file も生成に使われておらず、リポジトリの実状態も見ない。
    # 「もっともらしい嘘」を返す実装は、正しい内容に書き換えたとしてもレビューを
    # 通り抜けてしまうため、供給そのものを止める。
    return {
        "tool": "propose_patch",
        "ok": False,
        "theme": theme,
        "target_file": target_file,
        "error": (
            "patch 自動生成は未実装です。以前この endpoint が返していた固定 patch は "
            "実在しない配置を前提とした架空の内容で、README に適用された後 "
            "コミット 9e2754a で削除されました。patch は /tool/save_patch に "
            "本文を渡す形で、内容を確認した上で扱ってください。"
        ),
    }

_PATCH_HEADER_RE = re.compile(r"^(?:---|\+\+\+)[ \t]+(\S+)", re.MULTILINE)


def validate_patch_targets(patch_text: str, strip: int = 1) -> Optional[str]:
    """patch が書き換える先がプロジェクト内かを検証する。

    patch_path をディレクトリで縛っても、**本文が書込先を決める**ため不十分。
    `--- a/../../../etc/cron.d/evil` のようなヘッダは -p1 適用でプロジェクト外へ
    抜ける。新しい GNU patch は '..' や絶対パスを拒否するが、デプロイ先の
    patch 実装/版に依存したくないので自分で検証する。

    Returns:
        問題があればエラーメッセージ。無ければ None。
    """
    targets = _PATCH_HEADER_RE.findall(patch_text)
    if not targets:
        return "patch に ---/+++ ヘッダがありません"

    for raw in targets:
        if raw == "/dev/null":
            continue
        if raw.startswith("/"):
            return f"patch が絶対パスを対象にしています: {raw}"

        parts = [p for p in raw.split("/") if p]
        # -p<strip> で落とされる先頭要素を除く
        stripped_parts = parts[strip:] if len(parts) > strip else []
        if not stripped_parts:
            return f"patch の対象パスが -p{strip} 適用後に空になります: {raw}"
        if ".." in stripped_parts:
            return f"patch の対象パスに '..' が含まれます: {raw}"

        try:
            _resolve_in_project("/".join(stripped_parts))
        except PathOutsideProject:
            return f"patch がプロジェクト外を対象にしています: {raw}"

    return None


@app.post("/tool/save_patch")
def save_patch(payload: dict):
    theme = payload.get("theme", "patch")
    target_file = payload.get("target_file", "README.md")
    patch = payload.get("patch", "")

    if not patch.strip():
        return {
            "tool": "save_patch",
            "ok": False,
            "error": "patch is empty"
        }

    # 保存時点で弾く (適用時にも再検証する)。
    problem = validate_patch_targets(patch)
    if problem:
        return _reject("save_patch", problem)

    patches_dir = PATCHES_DIR
    patches_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = target_file.replace("/", "_")
    filename = f"{ts}_{safe_name}.patch"

    path = patches_dir / filename

    path.write_text(patch, encoding="utf-8")

    return {
        "tool": "save_patch",
        "ok": True,
        "path": str(path),
        "message": "patchを保存しました"
    }

@app.post("/tool/apply_patch")
def apply_patch(payload: dict):
    patch_path = payload.get("patch_path", "")
    dry_run = bool(payload.get("dry_run", True))

    if not patch_path:
        return {
            "tool": "apply_patch",
            "ok": False,
            "error": "patch_path is required"
        }

    # 1) patch ファイルの位置を patches/ 配下に限定する。
    try:
        path = _resolve_in_project(patch_path)
    except PathOutsideProject as exc:
        return _reject("apply_patch", str(exc))

    if not path.is_relative_to(PATCHES_DIR):
        return _reject(
            "apply_patch",
            f"patch は {PATCHES_DIR.name}/ 配下のもののみ適用できます: {patch_path}",
        )

    if not path.exists():
        return {
            "tool": "apply_patch",
            "ok": False,
            "error": f"patch not found: {patch_path}"
        }

    # 2) 本文が指す書込先も検証する (置き場所の制限だけでは不十分)。
    problem = validate_patch_targets(path.read_text(encoding="utf-8", errors="replace"))
    if problem:
        return _reject("apply_patch", problem)

    cmd = [
        "patch",
        "-p1",
        "-i",
        str(path),
    ]

    if dry_run:
        cmd.insert(1, "--dry-run")

    result = subprocess.run(
        cmd,
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        timeout=PATCH_TIMEOUT,
    )

    return {
        "tool": "apply_patch",
        "ok": result.returncode == 0,
        "dry_run": dry_run,
        "patch_path": str(path),
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
    }

@app.post("/tool/update_readme_section")
def update_readme_section(payload: dict):
    title = payload.get("title", "自己改善メモ")
    body = payload.get("body", "")
    mode = payload.get("mode", "append_once")

    readme_path = README_PATH

    if not body.strip():
        return {
            "tool": "update_readme_section",
            "ok": False,
            "error": "body is empty"
        }

    if not readme_path.exists():
        readme_path.write_text("# Hermes Agent 2\n", encoding="utf-8")

    text = readme_path.read_text(encoding="utf-8")
    heading = f"## {title}"

    # --- overwrite対応 ---
    if mode == "overwrite" and heading in text:
        # title は外部入力。正規表現メタ文字をエスケープしないと意図せぬ範囲を消す。
        pattern = rf"{re.escape(heading)}.*?(?=\n## |\Z)"
        text = re.sub(pattern, "", text, flags=re.S)

    # --- append_once ---
    if mode == "append_once" and heading in text:
        return {
            "tool": "update_readme_section",
            "ok": True,
            "applied": False,
            "message": f"{heading} は既に存在します"
        }

    section = f"""

{heading}

{body.strip()}
"""

    readme_path.write_text(text.rstrip() + section + "\n", encoding="utf-8")

    return {
        "tool": "update_readme_section",
        "ok": True,
        "applied": True,
        "path": str(readme_path),
        "heading": heading
    }



@app.post("/agent/auto_loop")
def auto_loop(payload: dict):
    loop_id = str(uuid.uuid4())
    theme = payload.get("theme", "Hermes Agent 2を改善する")
    max_turns = int(payload.get("max_turns", 2))
    update_readme = bool(payload.get("update_readme", True))

    logs = []
    ok = True
    error = ""

    try:
        improve = self_improve({
            "theme": theme,
            "max_turns": max_turns
        })
        logs.append({"step": "self_improve", "result": improve})

        if not improve.get("ok", False):
            ok = False
            error = "self_improve failed"
            raise RuntimeError(error)

        hermes = hermes_tool({
            "query": theme,
            "max_turns": max_turns
        })
        logs.append({"step": "hermes", "result": hermes})

        if not hermes.get("ok", False):
            ok = False
            error = "hermes_tool failed"
            raise RuntimeError(error)

        hints = load_recent_improvement_hints(limit=3)

        if update_readme:
            mode = "append_once"

            if "更新" in theme or "既存セクション" in theme or "既存" in theme:
                mode = "overwrite"

            title = "自動改善ログ"

            body = (
                f"loop_id: {loop_id}\n\n"
                f"テーマ: {theme}\n\n"
                f"使用ヒント:\n{hints if hints else '- なし'}"
            )

            update = update_readme_section({
                "title": title,
                "body": body,
                "mode": mode
            })

            logs.append({"step": "update_readme", "result": update})

    except Exception as e:
        ok = False
        error = f"{type(e).__name__}: {e}"

    evaluation = evaluate_auto_loop_result(logs)
    next_theme = suggest_next_theme(evaluation, theme)

    record = {
        "loop_id": loop_id,
        "ts": datetime.datetime.now().isoformat(),
        "ok": ok,
        "theme": theme,
        "max_turns": max_turns,
        "error": error,
        "evaluation": evaluation,
        "next_theme": next_theme,
        "steps": logs,
    }

    save_auto_loop_log(record)

    return {
        "agent": "auto_loop",
        "ok": ok,
        "loop_id": loop_id,
        "theme": theme,
        "error": error,
        "evaluation": evaluation,
        "next_theme": next_theme,
        "steps": logs,
    }


def save_auto_loop_log(record: dict):
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    with AUTO_LOOP_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def evaluate_auto_loop_result(steps: list[dict]) -> dict:
    score = 0
    reasons = []

    if not steps:
        return {"score": 0, "grade": "F", "reasons": ["steps が空"]}

    # ① 基本動作
    step_names = [s.get("step", "") for s in steps]

    if "self_improve" in step_names:
        score += 20
        reasons.append("改善案生成あり")

    if "hermes" in step_names:
        score += 20
        reasons.append("実行あり")

    # ② 実際に変化があったか
    updated = False
    for s in steps:
        if s.get("step") == "update_readme":
            r = s.get("result", {})
            if isinstance(r, dict) and r.get("applied") is True:
                updated = True

    if updated:
        score += 40
        reasons.append("READMEに実変更あり")
    else:
        score -= 20
        reasons.append("変更なし（停滞）")

    # ③ 失敗チェック
    failed = []
    for s in steps:
        r = s.get("result", {})
        if isinstance(r, dict) and r.get("ok") is False:
            failed.append(s.get("step"))

    if failed:
        score -= 40
        reasons.append(f"失敗あり: {failed}")
    else:
        score += 20
        reasons.append("失敗なし")

    # 正規化
    score = max(0, min(100, score))

    if score >= 80:
        grade = "A"
    elif score >= 60:
        grade = "B"
    elif score >= 40:
        grade = "C"
    else:
        grade = "D"

    return {
        "score": score,
        "grade": grade,
        "reasons": reasons
    }


def suggest_next_theme(evaluation: dict, theme: str) -> str:
    score = evaluation.get("score", 0)
    reasons = evaluation.get("reasons", [])

    if score >= 80:
        return "新しい機能を1つ追加する"

    reason_text = " / ".join(reasons)

    # ★ ここを追加
    if "変更なし（停滞）" in reason_text:
        if "README" in theme:
            return "READMEの既存セクションを更新する"
        return "READMEに新しい見出しを追加する"

    if "失敗" in reason_text:
        return "失敗したステップの原因を調査して修正する"

    return "改善テーマをより小さなタスクに分解する"



@app.post("/agent/auto_loop2")
def auto_loop2(payload: dict):
    theme = payload.get("theme", "Hermes Agent 2を改善する")
    max_turns = int(payload.get("max_turns", 2))
    threshold = int(payload.get("threshold", 80))

    runs = []

    # 1回目
    first = auto_loop({
        "theme": theme,
        "max_turns": max_turns,
        "update_readme": True,
    })
    runs.append(first)

    first_score = first.get("evaluation", {}).get("score", 0)
    next_theme = first.get("next_theme", "")

    # 2回目：スコアが低い場合だけ実行
    if first_score < threshold and next_theme:
        second = auto_loop({
            "theme": next_theme,
            "max_turns": max_turns,
            "update_readme": True,
        })
        runs.append(second)

    return {
        "agent": "auto_loop2",
        "ok": True,
        "initial_theme": theme,
        "threshold": threshold,
        "run_count": len(runs),
        "runs": runs,
    }


@app.post("/tool/inspect_python_file")
def inspect_python_file(payload: dict):
    target_file = payload.get("target_file", "api.py")

    try:
        target_path = _resolve_in_project(target_file)
    except PathOutsideProject:
        return _reject("inspect_python_file", "project外のファイルは読めません")

    if not target_path.exists():
        return {
            "tool": "inspect_python_file",
            "ok": False,
            "error": f"file not found: {target_file}"
        }

    if target_path.suffix != ".py":
        return {
            "tool": "inspect_python_file",
            "ok": False,
            "error": "Pythonファイルのみ対応しています"
        }

    text = target_path.read_text(encoding="utf-8", errors="replace")

    findings = []

    if "subprocess.run" in text and "timeout=" not in text:
        findings.append("subprocess.run に timeout が無い可能性があります")

    if "except Exception" in text:
        findings.append("広すぎる例外処理があります")

    if "eval(" in text or "exec(" in text:
        findings.append("eval/exec が含まれています。安全性に注意が必要です")

    if "Path(" in text and "resolve()" not in text:
        findings.append("Path操作に resolve() が無い箇所があります")

    if not findings:
        findings.append("明確な危険箇所は見つかりませんでした")

    return {
        "tool": "inspect_python_file",
        "ok": True,
        "target_file": target_file,
        "line_count": len(text.splitlines()),
        "findings": findings,
        "preview": text[:2000]
    }

@app.post("/tool/propose_code_improvement")
def propose_code_improvement(payload: dict):
    target_file = payload.get("target_file", "api.py")
    theme = payload.get("theme", "Pythonコードの安全性と保守性を改善する")

    inspection = inspect_python_file({
        "target_file": target_file
    })

    if not inspection.get("ok"):
        return {
            "tool": "propose_code_improvement",
            "ok": False,
            "error": inspection.get("error", "inspection failed")
        }

    findings = inspection.get("findings", [])
    hints = load_recent_improvement_hints(limit=3)

    proposal = {
        "target_file": target_file,
        "theme": theme,
        "findings": findings,
        "improvement_plan": [
            "変更範囲を小さく保つ",
            "既存の動作を壊さない",
            "まずは提案だけ行い、自動適用しない",
            "必要ならREADMEやmemoryに記録する"
        ],
        "recommended_next_action": "問題箇所を1つ選び、最小patchを作成する",
        "used_improvement_hints": hints
    }

    save_self_improvement_log(
        query=f"code improvement proposal for {target_file}: {theme}",
        result=json.dumps(proposal, ensure_ascii=False, indent=2),
        ok=True,
    )

    return {
        "tool": "propose_code_improvement",
        "ok": True,
        **proposal
    }

@app.post("/tool/backup_file")
def backup_file(payload: dict):
    target_file = payload.get("target_file", "api.py")

    try:
        target_path = _resolve_in_project(target_file)
    except PathOutsideProject:
        return _reject("backup_file", "project外のファイルはバックアップできません")

    if not target_path.exists():
        return {
            "tool": "backup_file",
            "ok": False,
            "error": f"file not found: {target_file}"
        }

    backup_dir = BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = target_file.replace("/", "_")
    backup_path = backup_dir / f"{ts}_{safe_name}.bak"

    backup_path.write_text(
        target_path.read_text(encoding="utf-8", errors="replace"),
        encoding="utf-8"
    )

    return {
        "tool": "backup_file",
        "ok": True,
        "target_file": target_file,
        "backup_path": str(backup_path)
    }


@app.post("/tool/restore_backup")
def restore_backup(payload: dict):
    """バックアップを対象ファイルへ書き戻す。

    safe_apply_patch のロールバック経路から呼ばれるが、従来は **定義されて
    いなかった**。そのため patch 失敗時や構文エラー時に NameError となり、
    ロールバックされないままファイルが壊れた状態で残っていた。
    """
    backup_path = payload.get("backup_path", "")
    target_file = payload.get("target_file", "")

    if not backup_path or not target_file:
        return _reject("restore_backup", "backup_path と target_file が必要です")

    try:
        source = _resolve_in_project(backup_path)
        target_path = _resolve_in_project(target_file)
    except PathOutsideProject as exc:
        return _reject("restore_backup", str(exc))

    if not source.is_relative_to(BACKUP_DIR):
        return _reject(
            "restore_backup",
            f"バックアップは {BACKUP_DIR.name}/ 配下のもののみ復元できます: {backup_path}",
        )

    if not source.exists():
        return _reject("restore_backup", f"backup not found: {backup_path}")

    target_path.write_text(
        source.read_text(encoding="utf-8", errors="replace"),
        encoding="utf-8",
    )

    return {
        "tool": "restore_backup",
        "ok": True,
        "target_file": target_file,
        "backup_path": str(source),
        "message": "バックアップから復元しました",
    }


def _commentize(note: str) -> str:
    """メモを必ずコメント行に変換する。

    従来は `# - {note}` と1行だけ組み立てていたため、note に改行を入れると
    2行目以降が **実行される Python コード** になった。認証も無かったので、
    リモートから .py へ任意コードを書き込める経路になっていた。全行を # で始める。
    """
    lines = note.strip().splitlines() or [""]
    out = [f"# - {lines[0].strip()}"]
    out.extend(f"#   {line.strip()}" for line in lines[1:])
    return "\n".join(out) + "\n"


@app.post("/tool/append_python_note")
def append_python_note(payload: dict):
    target_file = payload.get("target_file", "api.py")
    note = payload.get("note", "")

    try:
        target_path = _resolve_in_project(target_file)
    except PathOutsideProject:
        return _reject("append_python_note", "project外のファイルは変更できません")

    if not target_path.exists():
        return {
            "tool": "append_python_note",
            "ok": False,
            "error": f"file not found: {target_file}"
        }

    if target_path.suffix != ".py":
        return {
            "tool": "append_python_note",
            "ok": False,
            "error": "Pythonファイルのみ対応しています"
        }

    if not note.strip():
        return {
            "tool": "append_python_note",
            "ok": False,
            "error": "note is empty"
        }

    backup = backup_file({"target_file": target_file})
    if not backup.get("ok"):
        return {
            "tool": "append_python_note",
            "ok": False,
            "error": "backup failed",
            "backup": backup
        }

    text = target_path.read_text(encoding="utf-8", errors="replace")

    marker = "# Hermes self-improvement notes"
    if marker not in text:
        addition = f"\n\n{marker}\n{_commentize(note)}"
    else:
        addition = _commentize(note)

    target_path.write_text(text.rstrip() + "\n" + addition, encoding="utf-8")

    return {
        "tool": "append_python_note",
        "ok": True,
        "target_file": target_file,
        "backup_path": backup.get("backup_path"),
        "message": "Pythonファイルに自己改善メモを追記しました"
    }


@app.post("/tool/check_python_syntax")
def check_python_syntax(payload: dict):
    target_file = payload.get("target_file", "api.py")

    try:
        target_path = _resolve_in_project(target_file)
    except PathOutsideProject:
        return _reject("check_python_syntax", "project外のファイルはチェックできません")

    if not target_path.exists():
        return {
            "tool": "check_python_syntax",
            "ok": False,
            "error": f"file not found: {target_file}"
        }

    if target_path.suffix != ".py":
        return {
            "tool": "check_python_syntax",
            "ok": False,
            "error": "Pythonファイルのみ対応しています"
        }

    try:
        compile(
            target_path.read_text(encoding="utf-8", errors="replace"),
            str(target_path),
            "exec"
        )

        return {
            "tool": "check_python_syntax",
            "ok": True,
            "target_file": target_file,
            "message": "構文エラーはありません"
        }

    except SyntaxError as e:
        return {
            "tool": "check_python_syntax",
            "ok": False,
            "target_file": target_file,
            "error": f"{e.msg} (line {e.lineno})"
        }

    except Exception as e:
        return {
            "tool": "check_python_syntax",
            "ok": False,
            "error": f"{type(e).__name__}: {e}"
        }

@app.post("/tool/safe_apply_patch")
def safe_apply_patch(payload: dict):
    patch_path = payload.get("patch_path", "")
    target_file = payload.get("target_file", "api.py")

    if not patch_path:
        return {
            "tool": "safe_apply_patch",
            "ok": False,
            "error": "patch_path is required"
        }

    # 1. backup
    backup = backup_file({"target_file": target_file})
    if not backup.get("ok"):
        return {
            "tool": "safe_apply_patch",
            "ok": False,
            "stage": "backup",
            "backup": backup
        }

    backup_path = backup.get("backup_path")

    # 2. dry-run
    dry = apply_patch({
        "patch_path": patch_path,
        "dry_run": True
    })
    if not dry.get("ok"):
        return {
            "tool": "safe_apply_patch",
            "ok": False,
            "stage": "dry_run",
            "backup_path": backup_path,
            "dry_run": dry
        }

    # 3. apply
    applied = apply_patch({
        "patch_path": patch_path,
        "dry_run": False
    })
    if not applied.get("ok"):
        restored = restore_backup({
            "backup_path": backup_path,
            "target_file": target_file
        })
        return {
            "tool": "safe_apply_patch",
            "ok": False,
            "stage": "apply",
            "backup_path": backup_path,
            "applied": applied,
            "restored": restored
        }

    # 4. syntax check
    syntax = check_python_syntax({
        "target_file": target_file
    })

    if not syntax.get("ok"):
        restored = restore_backup({
            "backup_path": backup_path,
            "target_file": target_file
        })
        return {
            "tool": "safe_apply_patch",
            "ok": False,
            "stage": "syntax_check",
            "backup_path": backup_path,
            "syntax": syntax,
            "restored": restored
        }

    return {
        "tool": "safe_apply_patch",
        "ok": True,
        "target_file": target_file,
        "patch_path": patch_path,
        "backup_path": backup_path,
        "dry_run": dry,
        "applied": applied,
        "syntax": syntax
    }


if __name__ == "__main__":
    import uvicorn

    # 既定はループバック。外部公開は明示的なオプトインにする。
    uvicorn.run(
        app,
        host=os.getenv("HERMES_API_HOST", "127.0.0.1"),
        port=int(os.getenv("HERMES_API_PORT", "8000")),
    )


