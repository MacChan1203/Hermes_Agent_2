import json
import os
import requests
from dotenv import dotenv_values
from pathlib import Path

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

SYSTEM = """
あなたはMac上の安全なローカルエージェントです。
ユーザーの依頼を、許可された単一のシェルコマンドに変換してください。

許可コマンド:
- pwd
- ls
- cat
- grep
- find
- python

禁止:
- rm
- sudo
- chmod
- chown
- mv
- cp
- curl
- wget
- ssh
- scp
- brew
- open
- osascript
- /Users 配下の直接指定
- .env, .ssh, 秘密鍵の読み取り

出力は必ずJSONのみ:
"thought":"どう考えたか",
{"command":"ここにコマンド"}
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



