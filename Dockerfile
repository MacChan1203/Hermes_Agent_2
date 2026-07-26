# Hermes Agent 2 — 全サービス共通イメージ
#
# セキュリティ設計:
#   macOS 上では hermes_agent2/sandbox.py (seatbelt) が CMD:/PYTHON: の
#   ネットワークと書込をカーネルで拒否していた。しかし sandbox_available() は
#   `sys.platform == "darwin"` を要求するため、Linux コンテナ内では常に False を
#   返し、executor は素の argv 実行にフォールバックする (fail-open)。
#
#   つまりコンテナ内ではプロセス内の防御しか残らないため、**コンテナ自身が
#   境界を肩代わりする**必要がある。本 Dockerfile と compose 側で:
#     - 非 root 実行 (agent uid 10001)
#     - ソースはイメージに焼き込み、ホストの作業ツリーを bind マウントしない
#       (エージェントが自分のソースを書き換えてもホスト側には残らない)
#     - 秘密ファイル (~/.hermes/.env 等) をマウントしない。必要な値だけ env で渡す
#     - compose 側で read_only / cap_drop / ネットワーク制限を掛ける
#   を担保する。

FROM python:3.11-slim

# ffmpeg : faster-whisper (ctranslate2) の音声デコード用
# patch   : api.py の /tool/apply_patch が subprocess で呼ぶ。slim には無く、
#           入れないと apply_patch と safe_apply_patch が機能しない
#           (コンテナ内テストで実際に検知された)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg patch \
    && rm -rf /var/lib/apt/lists/*

# 依存を先に入れてレイヤキャッシュを効かせる
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# 非 root ユーザー。uid は固定してボリュームの所有者を予測可能にする。
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin agent

WORKDIR /app
COPY --chown=agent:agent . /app

# 実行時に書き込むディレクトリ。read_only ルートFSでも書けるよう
# named volume をここにマウントする (所有者を先に作っておく)。
RUN mkdir -p /app/memory /app/patches /app/backups /workspace \
    && chown -R agent:agent /app/memory /app/patches /app/backups /workspace

USER agent

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HERMES_PROJECT_DIR=/app \
    HERMES_WORKSPACE=/workspace

# 既定は何もしない。起動するサービスは compose 側の command で指定する。
CMD ["python", "-c", "print('サービスを compose の command で指定してください')"]
