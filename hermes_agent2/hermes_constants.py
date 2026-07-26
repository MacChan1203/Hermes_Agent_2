"""Hermes Agent 2 共有定数。"""

# Mistral / Ollama
OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
MISTRAL_API_BASE_URL = "https://api.mistral.ai/v1"
DEFAULT_MISTRAL_MODEL = "mistral"

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODELS_URL = f"{OPENROUTER_BASE_URL}/models"
OPENROUTER_CHAT_URL = f"{OPENROUTER_BASE_URL}/chat/completions"

AI_GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/v1"
AI_GATEWAY_MODELS_URL = f"{AI_GATEWAY_BASE_URL}/models"
AI_GATEWAY_CHAT_URL = f"{AI_GATEWAY_BASE_URL}/chat/completions"

NOUS_API_BASE_URL = "https://inference-api.nousresearch.com/v1"
NOUS_API_CHAT_URL = f"{NOUS_API_BASE_URL}/chat/completions"

# Groq (無料ティア・OpenAI互換・高速)
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_DEFAULT_MODEL = "llama-3.3-70b-versatile"

# safe_api.py が実行を許可するコマンド。すべて読み取り専用。
#
# `python` は入れない — インタプリタを許可した時点で許可リストは無意味になる
# (`python -c ...` で何でもできる)。同じ理由で sh/bash/perl/ruby/node、および
# 別コマンドを起動するラッパ (env/xargs/nohup 等) も入れない。
# awk/sed も除外 (awk の system(), sed の -i/e で実行・書込ができる)。
#
# ここに置く理由: safe_api.py (検証する側) と agent_client.py (LLM に
# 許可コマンドを提示する側) の両方で同じ表が必要になる。片方だけ直して
# もう片方が古いまま、という事故が実際に起きた (プロンプトが python を
# 許可と称し、サーバは拒否していた) ため hermes_auth.py と同じ方針で
# 一箇所に置く。
SAFE_API_ALLOWED_COMMANDS = frozenset({
    "pwd", "ls", "cat", "grep", "find", "head", "tail", "wc", "sort", "uniq", "file", "stat",
})

# safe_api.py がトークンとして拒否するシェル演算子。シェルを介さないため、
# これらは黙って引数に化ける。意図が曖昧なまま実行せず fail-closed にする。
SAFE_API_BLOCKED_OPERATORS = (";", "&&", "||", "|", ">", ">>", "<", "&")
