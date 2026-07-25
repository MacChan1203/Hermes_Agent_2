"""ステップ実行器。

セキュリティ設計:
    実行経路は信頼度が異なる2種類に分かれる。混ぜてはいけない。

    1. 組み込みステップ ("Inspect project structure" 等)
       開発者が本ファイル内に静的に書いたコマンド文字列。LLM も外部入力も
       内容を左右できないため、シェル注入の入口にならない。`if [ -f ]` /
       `for` / `&&` といったシェル構文を必要とするので bash 経由のまま残す。
       ただし `-l` (ログインシェル) は外す — rc ファイルを読み込む必然性が
       無く、環境を汚す。

    2. `CMD:` ステップ
       LLM プランナーの出力がそのまま渡る = 実質的に信頼できない入力。
       ここは shell を通さず argv 実行にし、連結演算子の禁止・コマンド
       denylist・パイプ許可リストで多層に絞る。

    どちらの経路も最終的に sandbox-exec (sandbox.py) で包み、ネットワーク
    拒否・書込制限・秘密の読取り拒否をカーネルで強制する。静的な denylist は
    本物のシェル上では回避され得るため、これが最後の防壁になる。
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .memory import remember_successful_command, set_environment_info
from .agent_state import AgentState
from . import sandbox

# 実行タイムアウト (秒)。従来は無制限で、応答しないコマンドがエージェント
# 全体を止められた。
EXEC_TIMEOUT = 120

_RE_TILDE = re.compile(r'(?<![a-zA-Z0-9_])~(?=/|$| )')
_RE_CMD_SUBSTITUTION = re.compile(r'\$\(')

# CMD: で拒否するコマンド名 (basename で判定)。
_BLOCKED_SHELL_COMMANDS = frozenset({
    # 破壊的操作
    "rm", "rmdir", "unlink", "shred", "wipefs",
    "chmod", "chown", "chgrp", "chroot",
    "dd", "mkfs", "mount", "umount", "diskutil",
    "sudo", "su", "doas",
    "kill", "pkill", "killall",
    "shutdown", "reboot", "halt", "poweroff",
    # インタプリタ: 本ファイルのコマンド方針を丸ごと迂回できる。
    "python", "python3", "python2", "pypy", "pypy3",
    "bash", "sh", "zsh", "fish", "perl", "ruby", "node", "deno", "php",
    # ランチャ/ラッパ: 自身は無害でも「別コマンドを起動する」ため、
    # basename によるコマンド名判定を素通りさせる (例: `env python3 -c ...`,
    # `xargs sh -c ...`)。env/printenv は親環境の秘密の露出源でもある。
    "env", "printenv", "nohup", "xargs", "timeout", "gtimeout",
    "stdbuf", "command", "exec", "eval", "watch", "script", "nice",
    # macOS 固有の実行/資格情報アクセス/永続化経路。
    # sandbox は (allow mach-lookup) が blanket なため securityd 経由の
    # キーチェーン読出し等はカーネル層では止まらない。
    "osascript", "open", "launchctl", "crontab", "at",
    "security", "defaults", "systemsetup", "spctl", "csrutil",
})

# パイプ後に許可する読み取り専用コマンド。
# awk / sed は任意コード実行が可能なため除外。tee は書込プリミティブなので除外。
_SAFE_PIPE_CMDS = frozenset({
    "grep", "head", "tail", "wc", "sort", "uniq", "tr", "cut", "cat",
})

# サブプロセスに引き渡す環境変数のキー許可リスト。
#
# 親プロセスの環境には API キー (GROQ_API_KEY 等) が入る。全継承すると
# `CMD: env` で丸見えになるだけでなく、任意の子プロセスへも渡ってしまう。
_ENV_PASSTHROUGH = (
    "PATH", "HOME", "TMPDIR", "TERM", "LANG", "LC_ALL", "LC_CTYPE",
)


def _subprocess_env() -> Dict[str, str]:
    """サブプロセス用の最小環境を構築する (秘密の継承を断つ)。"""
    env = {"PYTHONIOENCODING": "utf-8"}
    for key in _ENV_PASSTHROUGH:
        value = os.getenv(key)
        if value:
            env[key] = value
    return env


def _deny(message: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "stdout": "",
        "stderr": f"セキュリティ制限: {message}",
        "returncode": 1,
        "command": None,
    }


def _split_pipeline(cmd: str) -> Tuple[Optional[List[List[str]]], Optional[str]]:
    """コマンドを一度だけトークン化し、パイプ区間の argv 列に分割する。

    重要: 文字列を "|" で分割してから各区間を shlex にかけると、引用符の中の
    "|" (例: grep "a|b" file) で区間の切れ目がズレ、**検査した内容と実際に
    実行される argv が食い違う**。punctuation_chars でトークン化してから
    区切ることで、検査と実行が必ず同じトークン列を見るようにする。

    Returns:
        (パイプ区間ごとの argv リスト, エラーメッセージ)。片方が None。
    """
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError as exc:
        return None, f"コマンド解析エラー: {exc}"

    segments: List[List[str]] = []
    current: List[str] = []
    for token in tokens:
        if token == "|":
            segments.append(current)
            current = []
        elif token in ("<", ">", ">>", "<<", "&"):
            # shell を通さないため、リダイレクトは黙って引数に化けて紛らわしい。
            # 明示的に fail-closed にする。
            return None, f"リダイレクト/バックグラウンド演算子 '{token}' は禁止されています"
        else:
            current.append(token)
    segments.append(current)

    if any(not seg for seg in segments):
        return None, "空のコマンド区間があります"
    return segments, None


def _check_llm_command(cmd: str) -> Tuple[Optional[List[List[str]]], Optional[Dict[str, Any]]]:
    """LLM 由来コマンドを検査する。

    Returns:
        (実行すべき argv 区間, 拒否結果)。片方が None。
    """
    for pat in (";", "&&", "||", "`"):
        if pat in cmd:
            return None, _deny(f"シェル連結演算子 '{pat}' は禁止されています")
    if _RE_CMD_SUBSTITUTION.search(cmd):
        return None, _deny("$(...) コマンド置換は禁止されています")

    segments, error = _split_pipeline(cmd)
    if error is not None:
        if error.startswith("コマンド解析エラー"):
            return None, {
                "ok": False, "stdout": "", "stderr": error,
                "returncode": -1, "command": None,
            }
        return None, _deny(error)
    assert segments is not None

    for index, argv in enumerate(segments):
        name = os.path.basename(argv[0])
        if name in _BLOCKED_SHELL_COMMANDS:
            return None, _deny(f"コマンド '{name}' は禁止されています")
        # 2 段目以降は読み取り専用コマンドのみ許可する。
        if index > 0 and name not in _SAFE_PIPE_CMDS:
            return None, _deny(f"パイプ後続コマンド '{name}' は許可リストにありません")
    return segments, None


class Executor:
    def __init__(self, repo_root: Path | str) -> None:
        self.repo_root = Path(repo_root)

    # ------------------------------------------------------------------
    # 実行プリミティブ
    # ------------------------------------------------------------------

    def _run(self, argv: List[str]) -> Tuple[int, str, str]:
        """argv を sandbox で包んで実行する。"""
        argv = sandbox.wrap(argv, self.repo_root) or argv
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                cwd=str(self.repo_root),
                env=_subprocess_env(),
                timeout=EXEC_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return -1, "", f"タイムアウト ({EXEC_TIMEOUT}秒)"
        except Exception as exc:
            return -1, "", f"実行エラー: {exc}"
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    def _run_builtin(self, cmd: str) -> Tuple[int, str, str]:
        """組み込みステップ (信頼済みの静的コマンド) を実行する。

        シェル構文を含むため bash 経由。`-l` は付けない (rc ファイルを
        読み込ませない)。文字列は本ファイル内の定数のみで、外部入力は混ざらない。
        """
        return self._run(["bash", "-c", cmd])

    def _run_llm_cmd(self, cmd: str) -> Dict[str, Any]:
        """LLM 由来の CMD: を検査してから shell 無しで実行する。"""
        cmd = _RE_TILDE.sub(os.path.expanduser("~"), cmd)
        if not cmd.strip():
            return {"ok": False, "stdout": "", "stderr": "コマンドが空です", "returncode": 1, "command": None}

        # 検査と実行は同じトークン列を使う (再分割しない)。
        segments, rejection = _check_llm_command(cmd)
        if rejection is not None:
            return rejection
        assert segments is not None

        if len(segments) == 1:
            rc, out, err = self._run(segments[0])
            return {"ok": rc == 0, "stdout": out, "stderr": err, "returncode": rc, "command": cmd}

        # パイプ: shell を使わず Popen チェーンで組む。
        procs: List[subprocess.Popen] = []
        prev: Optional[subprocess.Popen] = None
        try:
            for seg in segments:
                argv = sandbox.wrap(seg, self.repo_root) or seg
                proc = subprocess.Popen(
                    argv,
                    stdin=prev.stdout if prev else None,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=str(self.repo_root),
                    env=_subprocess_env(),
                )
                if prev and prev.stdout:
                    prev.stdout.close()
                procs.append(proc)
                prev = proc
        except Exception as exc:
            for proc in procs:
                proc.kill()
            return {"ok": False, "stdout": "", "stderr": f"実行エラー: {exc}", "returncode": -1, "command": None}

        last = procs[-1]
        try:
            out, err = last.communicate(timeout=EXEC_TIMEOUT)
        except subprocess.TimeoutExpired:
            for proc in procs:
                proc.kill()
            last.communicate()
            return {"ok": False, "stdout": "", "stderr": f"タイムアウト ({EXEC_TIMEOUT}秒)", "returncode": -1, "command": None}
        for proc in procs[:-1]:
            proc.wait()
        return {
            "ok": last.returncode == 0,
            "stdout": out or "",
            "stderr": err or "",
            "returncode": last.returncode,
            "command": cmd,
        }

    # ------------------------------------------------------------------
    # ステップ定義
    # ------------------------------------------------------------------

    def _builtin_command(self, step: str) -> Optional[str]:
        """組み込みステップに対応する静的コマンドを返す (無ければ None)。"""
        python_bin = shlex.quote(sys.executable)

        if step == "Inspect project structure":
            return f'pwd && {python_bin} --version && ls -la && find . -maxdepth 2 | sort | head -120'
        if step == "Read README":
            return 'if [ -f README.md ]; then sed -n "1,220p" README.md; else echo "README.md not found"; fi'
        if step == "Read pyproject config":
            return 'if [ -f pyproject.toml ]; then sed -n "1,240p" pyproject.toml; else echo "pyproject.toml not found"; fi'
        if step == "Read requirements":
            return 'if [ -f requirements.txt ]; then sed -n "1,220p" requirements.txt; else echo "requirements.txt not found"; fi'
        if step == "Read core config files":
            # 注意: .env は列挙しない。ここに含めると API キー (GROQ_API_KEY 等)
            # が毎回そのままモデル文脈へ流れ込む。設定の形だけ見たい用途は
            # .env.example で足りる。
            return (
                'for f in pyproject.toml requirements.txt README.md .env.example; do '
                'if [ -f "$f" ]; then echo "\\n===== $f ====="; sed -n "1,160p" "$f"; fi; '
                "done"
            )
        if step == "Read main entry point":
            return (
                'for f in run_agent.py main.py agent_runner.py cli.py hermes_agent2/agent_runner.py; do '
                'if [ -f "$f" ]; then echo "\\n===== $f ====="; sed -n "1,240p" "$f"; fi; '
                "done"
            )
        if step == "Inspect CLI entry point":
            return (
                'for f in cli.py hermes_agent2/cli.py; do '
                'if [ -f "$f" ]; then echo "\\n===== $f ====="; sed -n "1,220p" "$f"; fi; '
                "done"
            )
        if step == "Inspect tests":
            return 'if [ -d tests ]; then find tests -maxdepth 2 | sort | head -120; else echo "tests directory not found"; fi'
        if step == "Inspect state store":
            return 'if [ -f hermes_agent2/state_store.py ]; then sed -n "1,260p" hermes_agent2/state_store.py; else echo "state_store.py not found"; fi'
        if step == "Inspect toolsets":
            return 'if [ -f hermes_agent2/toolsets.py ]; then sed -n "1,260p" hermes_agent2/toolsets.py; else echo "toolsets.py not found"; fi'
        if step == "Inspect tool distributions":
            return 'if [ -f hermes_agent2/toolset_distributions.py ]; then sed -n "1,260p" hermes_agent2/toolset_distributions.py; else echo "toolset_distributions.py not found"; fi'
        if step == "Inspect model tools":
            return 'if [ -f hermes_agent2/model_tools.py ]; then sed -n "1,260p" hermes_agent2/model_tools.py; else echo "model_tools.py not found"; fi'
        if step == "Inspect time handling":
            return 'if [ -f hermes_agent2/hermes_time.py ]; then sed -n "1,240p" hermes_agent2/hermes_time.py; else echo "hermes_time.py not found"; fi'
        if step == "Inspect constants":
            return 'if [ -f hermes_agent2/hermes_constants.py ]; then sed -n "1,220p" hermes_agent2/hermes_constants.py; else echo "hermes_constants.py not found"; fi'
        if step == "Inspect mini-swe-agent path support":
            return 'if [ -f hermes_agent2/minisweagent_path.py ]; then sed -n "1,240p" hermes_agent2/minisweagent_path.py; else echo "minisweagent_path.py not found"; fi'
        if step == "Check installed commands and PATH":
            return 'echo "$PATH" && which python || true && which python3 || true && which pip || true'
        if step == "Inspect file permissions":
            return "pwd && ls -la"
        if step == "Check Python environment and pip packages":
            return f'{python_bin} --version && {python_bin} -m pip list --disable-pip-version-check | head -60'
        return None

    # ------------------------------------------------------------------
    # ディスパッチ
    # ------------------------------------------------------------------

    def execute(self, step: str, state: AgentState) -> Dict[str, Any]:
        if step == "Summarize findings and propose next upgrade":
            return {
                "ok": True,
                "stdout": "Summary step is logical-only; no shell execution needed.",
                "stderr": "",
                "returncode": 0,
                "command": None,
            }

        builtin = self._builtin_command(step)
        if builtin is not None:
            rc, stdout, stderr = self._run_builtin(builtin)
            cmd = builtin
            result: Dict[str, Any] = {
                "ok": rc == 0, "stdout": stdout, "stderr": stderr,
                "returncode": rc, "command": cmd,
            }
        elif step.startswith("CMD:"):
            # LLM プランナーが生成した任意のシェルコマンド (最初の行のみ使用)
            cmd = step[len("CMD:"):].strip().splitlines()
            cmd = cmd[0].strip() if cmd else ""
            result = self._run_llm_cmd(cmd)
            stdout = result["stdout"]
        else:
            return {
                "ok": False,
                "stdout": "",
                "stderr": f"Unknown step: {step}",
                "returncode": 1,
                "command": None,
            }

        if result["ok"] and result.get("command"):
            remember_successful_command(state, result["command"])

        lines = stdout.splitlines()
        cwd = None
        pyver = None

        if step == "Inspect project structure":
            cwd = lines[0].strip() if lines else None
            for line in lines[:8]:
                if line.lower().startswith("python "):
                    pyver = line.strip()
                    break
        elif step == "Check Python environment and pip packages":
            for line in lines[:8]:
                if line.lower().startswith("python "):
                    pyver = line.strip()
                    break

        set_environment_info(
            state,
            cwd=cwd,
            python_version=pyver,
            python_executable=sys.executable,
        )

        if step == "Inspect project structure":
            state.working_memory["project_structure_text"] = stdout

        return result
