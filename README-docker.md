# Hermes Agent 2 — Docker 運用

## 前提

- Docker Desktop for Mac (arm64 で検証済み)
- Ollama は**ホスト側**で動かす (`brew services start ollama`)
  - コンテナからは `host.docker.internal:11434` で到達する
  - Ollama が `127.0.0.1` のみで待ち受けていても到達できることを実測で確認済み。
    `OLLAMA_HOST=0.0.0.0` にして LAN へ晒す必要は**ない**

## 初期設定

### 1. 秘密の設定

`docker.env` (gitignore 済み、パーミッション 600) に値を入れる。
`~/.hermes/.env` は 473 行の秘密を含むため**コンテナにはマウントしない**方針。
必要な値だけをここに書き写す。

```
HERMES_API_TOKEN=<python3 -c "import secrets; print(secrets.token_urlsafe(32))" の出力>
TELEGRAM_BOT_TOKEN=<@BotFather のトークン>
TELEGRAM_ALLOWED_USERS=<Telegram の数値ユーザーID>
```

`HERMES_API_TOKEN` が空だと API は 503 で全停止する (fail closed)。

### 2. launchd 側の bot を止める

`telegram_bridge.py` を launchd と Docker の両方で動かすと、同一トークンで
二重ポーリングになり Telegram が 409 を返して両方が不安定になる。
**どちらか一方だけ**を常用すること。

```bash
launchctl unload ~/Library/LaunchAgents/com.hermes.telegram-bridge.plist
```

Docker を常用にする場合、再起動後に launchd 版が復活しないよう
`~/Library/LaunchAgents/com.hermes.telegram-bridge.plist` を削除するか
`launchctl unload -w` で無効化しておく。

## 起動

```bash
docker compose up -d api safe-api telegram-bridge
```

音声文字起こしも使う場合 (初回にモデル 464MB を取得):

```bash
docker compose --profile whisper up -d telegram-whisper
```

## 動作確認

```bash
curl -H "Authorization: Bearer $(grep ^HERMES_API_TOKEN= docker.env | cut -d= -f2)" http://127.0.0.1:8000/v1/models
```

ヘッダ無しで叩くと 401 が返る (＝認証が効いている)。

## テスト

コンテナ内なら単一インタプリタで全件を一度に実行できる。
(ホストでは `pydantic_core` の ABI 制約で Python 3.14 と 3.11 に分けて
実行する必要があった)

```bash
docker run --rm hermes-agent2:latest python -m pytest tests/ -q
```

期待値: **185 passed, 6 skipped**。
skip される 6 件は macOS 専用の seatbelt テストで、これが Linux コンテナ内で
カーネル層の防御が効いていないことの証拠でもある (下記参照)。

## セキュリティ上の注意

### 境界が入れ替わっている

macOS 上では `hermes_agent2/sandbox.py` が seatbelt で以下をカーネル強制していた。

| seatbelt が担っていたもの | コンテナでの代替 |
|---|---|
| `~/.hermes` / `~/.ssh` / repo `.env` の読取り拒否 | **マウントしない**ことで代替 (env で必要な値のみ注入) |
| `hermes_agent2/` `.git/` への書込拒否 | ソースをイメージに焼き込み + `read_only: true` |
| CMD: サブプロセスのネットワーク拒否 | **完全な代替なし** (下記) |

`sandbox_available()` は `sys.platform == "darwin"` を要求するため、Linux
コンテナ内では常に `False` を返し、executor は素の argv 実行にフォールバック
する (fail-open)。

### ネットワーク遮断が完全でない理由

当初 `internal: true` ネットワークで外向き通信を塞ごうとしたが、
**internal ネットワークではポート公開が機能しない** (ホスト側に LISTEN が
現れない) ことを実測で確認した。Docker では「受信は許可・送信は禁止」を
compose だけでは表現できない。

現在は以下の多層で緩和している。

1. イメージにネットワーククライアントを入れていない
   (`curl` / `wget` / `nc` / `telnet` / `ssh` いずれも未インストール)
2. executor の denylist がインタプリタ (`python3`/`perl`/`ruby`/`node`) と
   ランチャ (`env`/`xargs` 等) を拒否する
3. `safe_api.py` の許可リストは読み取り専用コマンドのみ

これらは緩和策であり、カーネル層の遮断と同等ではない。**完全に遮断したい
場合**は該当サービスの `ports:` を外して `internal: true` ネットワークに戻し、
`docker compose exec` か別コンテナ経由でのみアクセスすること。

### その他

- 全サービス非 root (uid 10001) / `cap_drop: ALL` / `no-new-privileges` /
  `read_only` ルートFS + `/tmp` は tmpfs
- ポートはホストのループバックにのみ公開 (`127.0.0.1:8000:8000`)
- `read_only: true` のため `api.py` の `apply_patch` は `/app` 配下の
  ソースを書き換えられない。これは意図的で、seatbelt の書込拒否を
  コンテナ層で再現したもの。`memory/` `patches/` `backups/` は named volume
  として書込可能
