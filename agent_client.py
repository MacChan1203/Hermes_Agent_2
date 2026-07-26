import json
import os
import requests
from dotenv import dotenv_values
from pathlib import Path

from hermes_agent2.hermes_constants import (
    SAFE_API_ALLOWED_COMMANDS,
    SAFE_API_BLOCKED_OPERATORS,
)

_HERE = Path(__file__).parent

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
# safe_api.py の既定は 8001 (8000 は api.py)。compose も 127.0.0.1:8001 で公開する。
SAFE_API_URL = os.getenv("SAFE_API_URL", "http://127.0.0.1:8001/run")
MODEL = "qwen3-swallow-8b-64k:latest"
MEMORY_FILE = Path("memory.json")

# safe_api.py は全 route に Bearer 認証が掛かっている (hermes_auth.require_token)。
# トークンは docker.env が唯一の置き場なので、環境変数を優先しつつそこから読む。
# load_dotenv ではなく dotenv_values を使う — docker.env には TELEGRAM_BOT_TOKEN 等も
# 入っており、このプロセスの os.environ へ撒くと安全側に倒れない。
TOKEN_ENV = "HERMES_API_TOKEN"


def load_api_token() -> str:
    token = os.getenv(TOKEN_ENV, "")
    if token:
        return token

    env_file = _HERE / "docker.env"
    if env_file.exists():
        token = (dotenv_values(env_file).get(TOKEN_ENV) or "").strip()

    if not token:
        raise RuntimeError(
            f"{TOKEN_ENV} が見つかりません。環境変数で渡すか、{env_file} に設定してください。"
        )
    return token

# 許可コマンドは safe_api.py と同じ定数から組み立てる。ここに手書きすると
# サーバ側の変更に追従できず、「プロンプトは許可と言うがサーバは拒否する」
# 状態になる (実際に python がその状態だった)。
# sorted() で並びを固定する — set の反復順は実行ごとに変わり得るため。
_ALLOWED_LINES = "\n".join(f"- {c}" for c in sorted(SAFE_API_ALLOWED_COMMANDS))
_OPERATORS = " ".join(SAFE_API_BLOCKED_OPERATORS)

SYSTEM = f"""
あなたは safe_api の読み取り専用サンドボックスを操作するエージェントです。
ユーザーの依頼を、実行可能な単一のコマンドに変換してください。

許可コマンド (これ以外は必ず拒否されます):
{_ALLOWED_LINES}

制約:
- コマンドは1つだけ。次の演算子は引数ではなく演算子として拒否されます: {_OPERATORS}
- パイプやリダイレクトの代わりに、上記コマンドのオプションを使ってください
  (例: 先頭10行なら `head -n 10 ファイル`)
- パスはワークスペース内の相対パスのみ。`~` や外部を指す絶対パスは拒否されます
- インタプリタ (python, sh, bash 等) とファイルを書き換えるコマンドは許可リストに
  無いため、それらを必要とする依頼は実行できません

許可コマンドだけでは達成できない依頼の場合、command を空文字 "" にして、
thought に理由を書いてください。無理に近いコマンドを当てはめないこと。

出力は必ずJSONのみ。thought と command を同じオブジェクトに入れてください:
{{"thought":"どう考えたか","command":"ここにコマンド"}}
"""

def load_memory():
    if not MEMORY_FILE.exists():
        return []

    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_memory(entry):
    memory = load_memory()
    memory.append(entry)

    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memory[-20:], f, ensure_ascii=False, indent=2)




def ask_llm(user_text: str) -> str:
    prompt = SYSTEM + "\nユーザー依頼:\n" + user_text

    res = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0
            }
        },
        timeout=120,
    )
    res.raise_for_status()
    text = res.json()["response"].strip()

    # qwen系が余計な説明を出した時の保険
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError(f"JSONが見つかりません: {text}")

    data = json.loads(text[start:end])

    return {
        "thought": data["thought"],
        "command": data["command"]
    }

def run_safe(command: str):
    res = requests.post(
        SAFE_API_URL,
        json={"command": command},
        headers={"Authorization": f"Bearer {load_api_token()}"},
        timeout=30,
    )
    res.raise_for_status()
    return res.json()

if __name__ == "__main__":
    user_text = input("依頼 > ")
    response = ask_llm(user_text)

    thought = response["thought"]
    command = response["command"]

    print("\n思考:")
    print(thought)

    # 許可コマンドで実現できない依頼はここで打ち切る。空文字を run_safe に
    # 渡すと safe_api 側の "empty command" になるだけで、理由が伝わらない。
    if not command.strip():
        print("\nこの依頼は許可コマンドでは実行できません。")
        raise SystemExit(0)

    print("\n生成コマンド:")
    print(command)



    confirm = input("\n実行しますか? (y/n) > ")

    if confirm.lower() == "y":
        result = run_safe(command)

        print("\n実行結果:")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        save_memory({
            "user": user_text,
            "thought": thought,
            "command": command,
            "result": result,
        })


    else:
        print("キャンセルしました")



