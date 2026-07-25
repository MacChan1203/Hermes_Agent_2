"""OS レベルのプロセス隔離 (macOS seatbelt / sandbox-exec)。

設計意図:
    executor.py は最終的に本物のシェル/バイナリを subprocess で実行する。
    コマンド denylist は静的解析であり、本物のインタプリタ上では原理的に
    回避され得る (例: `env python3 -c`, awk の system(), GTFOBins)。

    したがって信頼境界をカーネルに移す。実行対象のプロセスを sandbox-exec で
    包み、以下をカーネルレベルで強制する:
      - ネットワーク全面拒否 (情報持ち出しチャネルを塞ぐ)
      - 書き込みを repo_root と一時ディレクトリのみに限定。さらに repo 内でも
        自エージェントの実装 (hermes_agent2/) と .git/ は拒否
      - 読み取りは原則許可 (stdlib/dyld のロードに必要) しつつ、秘密の巣
        (~/.ssh, ~/.aws, ~/.hermes, キーチェーン等) だけを deny で穴埋め

    seatbelt は子プロセスに継承されるため、サンドボックス内から起動された
    別バイナリ (bash が呼ぶ sed、env が呼ぶ python3 等) も同じ制約を受ける。
    これが「列挙ではなく構造で」突破を防ぐ核心。

    静的チェック (コマンド denylist) は引き続き多層防御として機能させ、
    本モジュールはその下のカーネル境界を担う。

注記:
    本エージェントには READ:/WRITE: のようなメインプロセス側のファイルツールが
    無い。つまりサブプロセスのファイルアクセスを縛るのは本プロファイルだけで
    あり、read deny はここでしか担保されない。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

_SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def sandbox_available() -> bool:
    """このプラットフォームで sandbox-exec による隔離が使えるか。"""
    return sys.platform == "darwin" and os.path.exists(_SANDBOX_EXEC)


def _network_allowed() -> bool:
    """サブプロセスにネットワークを許可するか (既定: 拒否)。

    既定では拒否。どうしても必要な場合のみ
    HERMES_SANDBOX_ALLOW_NETWORK=1 で明示的にオプトインする。
    """
    return os.getenv("HERMES_SANDBOX_ALLOW_NETWORK", "") == "1"


def _sb_escape(path: str) -> str:
    """seatbelt プロファイルの文字列リテラル用にエスケープする。"""
    return path.replace("\\", "\\\\").replace('"', '\\"')


def _resolve_variants(path: Path) -> List[str]:
    """パスを resolve し、macOS の /tmp→/private/tmp 等のエイリアスも含める。"""
    variants: List[str] = []
    try:
        rp = str(path.resolve())
    except OSError:
        rp = str(path)
    variants.append(rp)
    # macOS では /tmp, /var は /private/... の symlink。realpath 済みでも
    # 念のため両表記を許可しておく。
    if rp.startswith("/private/"):
        variants.append(rp[len("/private"):])
    else:
        variants.append("/private" + rp)
    return variants


def _write_subpaths(repo_root: Path, allow_dirs: Iterable[Path]) -> List[str]:
    """書き込みを許可する subpath 群を構築する。"""
    paths: set[str] = set()
    for p in [repo_root, *allow_dirs]:
        for v in _resolve_variants(Path(p)):
            paths.add(v)
    # インタプリタ/ツールが使う一時ディレクトリ
    for tmp in (tempfile.gettempdir(), "/var/folders", "/private/var/folders"):
        for v in _resolve_variants(Path(tmp)):
            paths.add(v)
    return sorted(paths)


# 読み取りを拒否する機微な場所 (ホームディレクトリ相対)。
#
# file-read* は blanket 許可のまま (インタプリタ/dyld 共有キャッシュの起動には
# macOS バージョン依存の広範な read が要る。allow-list 方式は /System 配下の
# firmlink を辿れず SIGABRT する)。その上に「秘密の巣」だけを deny で穴埋めする。
#
# 注意: これは高価値の既知の場所を塞ぐ多層防御であり、任意の場所 (例:
# ~/Documents/passwords.txt) までは列挙できない。
_SENSITIVE_HOME_SUBPATHS: tuple[str, ...] = (
    ".ssh", ".aws", ".gnupg", ".config", ".docker", ".kube",
    ".azure", ".gcp", ".oci", ".terraform.d",
    ".netrc", ".pgpass", ".git-credentials", ".npmrc", ".pypirc",
    ".zsh_history", ".bash_history", ".python_history", ".irb_history",
    # ~/.hermes: 本系列のエージェント群が使う設定置き場。実際に Telegram
    # ボットトークンや各種 API キーを含む .env が置かれているため、
    # サブプロセスから読めてはいけない。
    ".hermes",
    "Library/Keychains",
    "Library/Application Support",
    "Library/Cookies",
    "Library/Safari",
    "Library/Messages",
    "Library/Mail",
)

# repo 内にある秘密ファイル (実行系サブプロセスは読む必要が無い)。
_SENSITIVE_REPO_NAMES: tuple[str, ...] = (".env",)

# repo_root 配下でも書き込みを拒否するサブディレクトリ。
#
# 書込許可は repo_root 全体に出ている。しかし repo_root にはエージェント自身の
# 実装 (hermes_agent2/) が置かれており、これは次回実行時に **メインプロセス =
# サンドボックス外・ネットワーク許可** で走る。つまりサブプロセスが自分のソース
# を書き換えれば、次のターンでガードごと外して脱獄できる。
#
# 書込プリミティブを列挙して塞ぐ (tee, sed -i, cp, mv, ...) のは静的 denylist と
# 同じ敗北パターンなので、ここでもカーネルで構造的に落とす。
# .git も同様: hooks/config を書ければ任意コード実行・資格情報の書換えに繋がる。
_NO_WRITE_REPO_SUBDIRS: tuple[str, ...] = ("hermes_agent2", ".git")


def _read_deny_paths(repo_root: Path) -> List[str]:
    """読み取りを拒否する絶対パス群を構築する (存在有無に関わらず列挙する)。"""
    home = Path.home()
    deny: set[str] = set()
    for rel in _SENSITIVE_HOME_SUBPATHS:
        for v in _resolve_variants(home / rel):
            deny.add(v)
    for name in _SENSITIVE_REPO_NAMES:
        for v in _resolve_variants(repo_root / name):
            deny.add(v)
    return sorted(deny)


def _write_deny_paths(repo_root: Path) -> List[str]:
    """repo 配下で書き込みを拒否する絶対パス群を構築する。"""
    deny: set[str] = set()
    for rel in _NO_WRITE_REPO_SUBDIRS:
        for v in _resolve_variants(repo_root / rel):
            deny.add(v)
    return sorted(deny)


def build_profile(repo_root: Path | str, allow_dirs: Iterable[Path] = ()) -> str:
    """repo_root と allow_dirs を書込可能とする seatbelt プロファイルを生成する。"""
    repo_root = Path(repo_root)
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-fork)",
        "(allow process-exec)",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow file-read*)",
        "(allow network*)" if _network_allowed() else "(deny network*)",
    ]
    # file-read* の後に機微パスの deny を重ねる (seatbelt は最後に一致した規則が勝つ)。
    read_deny = _read_deny_paths(repo_root)
    if read_deny:
        deny_rules = " ".join(f'(subpath "{_sb_escape(p)}")' for p in read_deny)
        lines.append(f"(deny file-read* {deny_rules})")
    write_rules = " ".join(
        f'(subpath "{_sb_escape(p)}")' for p in _write_subpaths(repo_root, allow_dirs)
    )
    lines.append(f'(allow file-write* {write_rules} (literal "/dev/null"))')
    # allow の後に repo 内の書込禁止領域を重ねる。
    write_deny = _write_deny_paths(repo_root)
    if write_deny:
        deny_rules = " ".join(f'(subpath "{_sb_escape(p)}")' for p in write_deny)
        lines.append(f"(deny file-write* {deny_rules})")
    return "\n".join(lines)


def wrap(
    argv: Sequence[str],
    repo_root: Path | str,
    allow_dirs: Iterable[Path] = (),
) -> Optional[List[str]]:
    """argv を sandbox-exec でラップした新しい argv を返す。

    sandbox-exec が利用できない環境では None を返す (呼び出し側がフォールバック)。
    """
    if not sandbox_available():
        return None
    profile = build_profile(repo_root, allow_dirs)
    return [_SANDBOX_EXEC, "-p", profile, *argv]
