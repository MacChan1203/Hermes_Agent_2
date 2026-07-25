"""executor.py のセキュリティガードの回帰テスト。

前提となる脅威モデル:
  - 組み込みステップは開発者が書いた静的コマンド (信頼できる)
  - `CMD:` は LLM プランナー出力 (信頼できない入力)
  - 静的検査を抜けた場合の最後の防壁が sandbox-exec (カーネル境界)
"""
from pathlib import Path

import pytest

from hermes_agent2 import sandbox
from hermes_agent2.agent_state import AgentState
from hermes_agent2.executor import Executor, _subprocess_env
import hermes_agent2.executor as executor_module


BUILTIN_STEPS = [
    "Inspect project structure",
    "Read README",
    "Read pyproject config",
    "Read requirements",
    "Read core config files",
    "Read main entry point",
    "Inspect CLI entry point",
    "Inspect tests",
    "Inspect state store",
    "Inspect toolsets",
    "Inspect tool distributions",
    "Inspect model tools",
    "Inspect time handling",
    "Inspect constants",
    "Inspect mini-swe-agent path support",
    "Check installed commands and PATH",
    "Inspect file permissions",
    "Check Python environment and pip packages",
    "Summarize findings and propose next upgrade",
]


def _state():
    return AgentState(user_goal="security test")


# 各組み込みステップが実際に読むファイル。空ディレクトリだと全ステップが
# `else echo "... not found"` に落ちて rc=0 になり、bash 実行が壊れていても
# テストが緑のままになる。中身を持つ最小プロジェクトを作って本文を読ませる。
_STEP_FILES = {
    "README.md": "# readme\n" + "readme line\n" * 240,
    "pyproject.toml": "[project]\nname = 'probe'\n" + "# pad\n" * 240,
    "requirements.txt": "fire\n" + "# pad\n" * 240,
    ".env.example": "GROQ_API_KEY=replace_me\n",
    "run_agent.py": "MARKER_RUN_AGENT = 1\n" + "# pad\n" * 240,
    "cli.py": "MARKER_CLI = 1\n" + "# pad\n" * 220,
    "tests/test_probe.py": "def test_probe():\n    assert True\n",
    "hermes_agent2/state_store.py": "MARKER_STATE_STORE = 1\n" + "# pad\n" * 260,
    "hermes_agent2/toolsets.py": "MARKER_TOOLSETS = 1\n",
    "hermes_agent2/toolset_distributions.py": "MARKER_DISTRIBUTIONS = 1\n",
    "hermes_agent2/model_tools.py": "MARKER_MODEL_TOOLS = 1\n",
    "hermes_agent2/hermes_time.py": "MARKER_TIME = 1\n",
    "hermes_agent2/hermes_constants.py": "MARKER_CONSTANTS = 1\n",
    "hermes_agent2/minisweagent_path.py": "MARKER_MINISWE = 1\n",
    "hermes_agent2/agent_runner.py": "MARKER_AGENT_RUNNER = 1\n",
}

# ステップ名 -> その出力に必ず現れるべき文字列 (実際にファイルを読んだ証拠)。
_STEP_EXPECTED_CONTENT = {
    "Read README": "readme line",
    "Read pyproject config": "name = 'probe'",
    "Read requirements": "fire",
    "Read core config files": "GROQ_API_KEY=replace_me",
    "Read main entry point": "MARKER_RUN_AGENT",
    "Inspect CLI entry point": "MARKER_CLI",
    "Inspect tests": "test_probe.py",
    "Inspect state store": "MARKER_STATE_STORE",
    "Inspect toolsets": "MARKER_TOOLSETS",
    "Inspect tool distributions": "MARKER_DISTRIBUTIONS",
    "Inspect model tools": "MARKER_MODEL_TOOLS",
    "Inspect time handling": "MARKER_TIME",
    "Inspect constants": "MARKER_CONSTANTS",
    "Inspect mini-swe-agent path support": "MARKER_MINISWE",
    "Inspect file permissions": "README.md",
    "Inspect project structure": "README.md",
    "Check installed commands and PATH": "/",
    "Check Python environment and pip packages": "Python ",
}


@pytest.fixture
def project(tmp_path):
    """組み込みステップが読むファイルを備えた最小プロジェクトを作る。"""
    for rel, body in _STEP_FILES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return tmp_path


def _cmd(executor, command):
    return executor.execute(f"CMD: {command}", _state())


# ---------------------------------------------------------------------------
# 組み込みステップ (信頼済み経路) — 回帰
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("step", BUILTIN_STEPS)
def test_builtin_steps_still_succeed(project, step):
    """sandbox で包んでも組み込みステップは全て成功する。"""
    result = Executor(repo_root=project).execute(step, _state())
    assert result["ok"] is True, result["stderr"]


@pytest.mark.parametrize("step,expected", sorted(_STEP_EXPECTED_CONTENT.items()))
def test_builtin_steps_actually_read_content(project, step, expected):
    """組み込みステップが実際にファイル本文を読めている。

    rc=0 だけでは 'not found' 分岐に落ちても緑になるため、中身を検証する。
    """
    result = Executor(repo_root=project).execute(step, _state())

    assert result["ok"] is True, result["stderr"]
    assert expected in result["stdout"]


def test_core_config_step_does_not_read_dotenv(tmp_path):
    """'Read core config files' が .env を列挙しない。

    ここに .env があると、通常運転で API キーがそのままモデル文脈に流れ込む
    (攻撃者入力を一切必要としない漏洩経路だった)。
    """
    command = Executor(repo_root=tmp_path)._builtin_command("Read core config files")
    assert ".env.example" in command
    assert " .env " not in command


def test_core_config_step_does_not_leak_secret_value(tmp_path):
    """回帰: repo に .env があってもその中身が出力に出ない。"""
    (tmp_path / ".env").write_text("GROQ_API_KEY=gsk_topsecretvalue\n")
    (tmp_path / "README.md").write_text("readme body\n")

    result = Executor(repo_root=tmp_path).execute("Read core config files", _state())

    assert "gsk_topsecretvalue" not in result["stdout"]


# ---------------------------------------------------------------------------
# CMD: (信頼できない経路) — 静的ガード
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "echo hi; id",
    "echo hi && id",
    "echo hi || id",
    "echo `id`",
    "echo $(id)",
])
def test_shell_chaining_rejected(tmp_path, command):
    """連結演算子・コマンド置換は拒否される。"""
    result = _cmd(Executor(repo_root=tmp_path), command)
    assert result["ok"] is False
    assert "セキュリティ制限" in result["stderr"]


@pytest.mark.parametrize("command", [
    "bash -c id",
    "sh -c id",
    "python3 -c 'print(1)'",
    "env python3 -c 'print(1)'",
    "xargs sh -c id",
    "env",
    "printenv",
    "osascript -e 'do shell script \"id\"'",
    "security find-generic-password -s foo",
    "launchctl list",
    "sudo id",
    "rm -rf .",
])
def test_blocked_commands_rejected(tmp_path, command):
    """インタプリタ・ランチャ・破壊的コマンド・macOS 資格情報経路を拒否する。"""
    result = _cmd(Executor(repo_root=tmp_path), command)
    assert result["ok"] is False
    assert "セキュリティ制限" in result["stderr"]


def test_pipe_allowlist_rejects_write_primitive(tmp_path):
    """tee は読み取り専用ではないのでパイプ後続に許可しない。"""
    result = _cmd(Executor(repo_root=tmp_path), "cat README.md | tee out.txt")
    assert result["ok"] is False
    assert "許可リスト" in result["stderr"]


def test_pipe_allowlist_rejects_awk(tmp_path):
    """awk は system() で任意実行できるため許可しない。"""
    result = _cmd(Executor(repo_root=tmp_path), "ls | awk '{system(\"id\")}'")
    assert result["ok"] is False


def test_quoted_pipe_is_not_a_pipeline(tmp_path):
    """引用符内の '|' で区間がズレない (検査と実行のトークン列を一致させる)。

    文字列を "|" で単純分割すると、検査したコマンド名と実際に起動する argv が
    食い違う。'bash' が引用符の中にある以上、それは grep の引数であって
    起動されるコマンドではない。
    """
    (tmp_path / "a.txt").write_text("hit-a|bash-here\n")

    result = _cmd(Executor(repo_root=tmp_path), 'grep "a|bash" a.txt')

    assert result["ok"] is True
    assert "hit-a|bash-here" in result["stdout"]


def test_unspaced_pipe_still_splits(tmp_path):
    """空白なしの 'ls|grep' も正しくパイプとして扱う。"""
    (tmp_path / "target.py").write_text("x")

    result = _cmd(Executor(repo_root=tmp_path), "ls|grep target")

    assert result["ok"] is True
    assert "target.py" in result["stdout"]


def test_quoted_blocked_command_not_executed(tmp_path):
    """引用符で囲まれた危険コマンド名は実行されず、ただの文字列として扱う。"""
    result = _cmd(Executor(repo_root=tmp_path), 'echo "x | bash"')

    assert result["ok"] is True
    assert result["stdout"].strip() == "x | bash"


@pytest.mark.parametrize("command", ["echo hi > out.txt", "echo hi >> out.txt", "cat < a.txt"])
def test_redirection_rejected(tmp_path, command):
    """shell を通さないためリダイレクトは黙って引数に化ける。fail-closed にする。"""
    result = _cmd(Executor(repo_root=tmp_path), command)

    assert result["ok"] is False
    assert "リダイレクト" in result["stderr"]
    assert not (tmp_path / "out.txt").exists()


def test_safe_pipe_allowed(tmp_path):
    """正常系: 読み取り専用コマンドのパイプは通る。"""
    (tmp_path / "a.txt").write_text("alpha\nbeta\n")
    result = _cmd(Executor(repo_root=tmp_path), "cat a.txt | grep beta")
    assert result["ok"] is True
    assert "beta" in result["stdout"]


def test_plain_command_allowed(tmp_path):
    """正常系: 単純なコマンドは通る。"""
    (tmp_path / "a.txt").write_text("x")
    result = _cmd(Executor(repo_root=tmp_path), "ls")
    assert result["ok"] is True
    assert "a.txt" in result["stdout"]


# ---------------------------------------------------------------------------
# 環境変数のスクラブ
# ---------------------------------------------------------------------------

def test_env_helper_drops_secrets(monkeypatch):
    """許可リストに無いキー (API キー等) は落ち、PATH は残る。"""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secret")

    env = _subprocess_env()

    assert "GROQ_API_KEY" not in env
    assert "PATH" in env


def test_subprocess_receives_scrubbed_env(tmp_path, monkeypatch):
    """回帰: 実行時に env= が渡っている (配線そのものを固定する)。

    ヘルパ単体の検査だけでは subprocess.run から env= を消しても気付けない。
    """
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secret")
    captured: dict = {}
    real_run = executor_module.subprocess.run

    def spy(argv, **kwargs):
        captured.update(kwargs)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(executor_module.subprocess, "run", spy)
    _cmd(Executor(repo_root=tmp_path), "ls")

    assert "env" in captured, "env= が渡されていない"
    assert "GROQ_API_KEY" not in captured["env"]
    assert captured.get("timeout"), "timeout が設定されていない"


def test_pipe_subprocess_receives_scrubbed_env(tmp_path, monkeypatch):
    """回帰: パイプ経路 (Popen) も同じ最小環境で起動する。"""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secret")
    (tmp_path / "a.txt").write_text("x\n")
    captured: list = []
    real_popen = executor_module.subprocess.Popen

    def spy(argv, **kwargs):
        captured.append(kwargs)
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(executor_module.subprocess, "Popen", spy)
    _cmd(Executor(repo_root=tmp_path), "cat a.txt | head -1")

    assert captured, "Popen が呼ばれていない"
    for kwargs in captured:
        assert "GROQ_API_KEY" not in kwargs["env"]


# ---------------------------------------------------------------------------
# seatbelt プロファイル (プラットフォーム非依存の組み立て検査)
# ---------------------------------------------------------------------------

def test_profile_denies_network_by_default(tmp_path):
    assert "(deny network*)" in sandbox.build_profile(tmp_path)


def test_profile_denies_reading_hermes_dir(tmp_path):
    """~/.hermes は Telegram トークン等を含むため読取り拒否に含める。"""
    profile = sandbox.build_profile(tmp_path)
    assert str(Path.home() / ".hermes") in profile


def test_profile_denies_writing_agent_source(tmp_path):
    """自エージェント実装と .git は repo 内でも書込拒否。"""
    profile = sandbox.build_profile(tmp_path)
    deny_write = [line for line in profile.splitlines() if line.startswith("(deny file-write*")]
    assert deny_write, "書込 deny 規則が無い"
    assert str(tmp_path / "hermes_agent2") in deny_write[0]
    assert str(tmp_path / ".git") in deny_write[0]


# ---------------------------------------------------------------------------
# カーネル境界 (macOS のみ)
# ---------------------------------------------------------------------------

requires_sandbox = pytest.mark.skipif(
    not sandbox.sandbox_available(), reason="sandbox-exec が無い環境ではスキップ"
)


@requires_sandbox
def test_cannot_read_repo_dotenv(tmp_path):
    """repo 内の .env はカーネルが読取り拒否する。"""
    (tmp_path / ".env").write_text("GROQ_API_KEY=gsk_topsecretvalue")

    result = _cmd(Executor(repo_root=tmp_path), "cat .env")

    assert result["ok"] is False
    assert "gsk_topsecretvalue" not in result["stdout"]


@requires_sandbox
def test_cannot_read_home_secret(tmp_path, monkeypatch):
    """~/.hermes 等の秘密の巣はカーネルが読取り拒否する。"""
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".hermes" / ".env").write_text("TELEGRAM_BOT_TOKEN=tok_secret")
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()

    result = _cmd(Executor(repo_root=repo), f"cat {home / '.hermes' / '.env'}")

    assert result["ok"] is False
    assert "tok_secret" not in result["stdout"]


@requires_sandbox
def test_cannot_overwrite_agent_source(tmp_path):
    """書込プリミティブでも hermes_agent2/ は書き換えられない。

    ここが書けると、次ターンでメインプロセス (サンドボックス外) が
    書き換え済みのガードを実行してしまう。
    """
    pkg = tmp_path / "hermes_agent2"
    pkg.mkdir()
    target = pkg / "executor.py"
    target.write_text("ORIGINAL")
    (tmp_path / "payload.txt").write_text("PWNED")

    _cmd(Executor(repo_root=tmp_path), f"cp payload.txt {target}")

    assert target.read_text() == "ORIGINAL"


@requires_sandbox
def test_cannot_write_git_dir(tmp_path):
    """.git/ への書込はカーネルが拒否する。"""
    (tmp_path / ".git").mkdir()

    _cmd(Executor(repo_root=tmp_path), "touch .git/hook_probe")

    assert not (tmp_path / ".git" / "hook_probe").exists()


@requires_sandbox
def test_can_write_inside_repo(tmp_path):
    """正常系: repo 内の作業ファイルへの書込は許可される。"""
    result = _cmd(Executor(repo_root=tmp_path), "touch work_probe.txt")
    assert result["ok"] is True
    assert (tmp_path / "work_probe.txt").exists()


@requires_sandbox
def test_network_denied(tmp_path):
    """外向き通信はカーネルで拒否される。"""
    result = _cmd(Executor(repo_root=tmp_path), "curl -s --max-time 5 http://example.com")
    assert result["ok"] is False or not result["stdout"].strip()
