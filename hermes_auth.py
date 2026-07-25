"""HTTP API 共通の認証。

api.py と safe_api.py はどちらも「到達できる者がホスト上でコードを動かせる」
性質の API であり、認証が唯一の外周防御になる。認証コードを各ファイルに
複製すると片方だけ直して片方が古いまま、という事故が起きるため一箇所に置く。
"""
from __future__ import annotations

import os
import secrets

from fastapi import HTTPException, Request, status

TOKEN_ENV = "HERMES_API_TOKEN"


def require_token(request: Request) -> None:
    """Bearer トークンを検証する。

    FastAPI(dependencies=[Depends(require_token)]) の形でアプリ全体に掛ける。
    route ごとに書く方式は付け忘れが起きるため使わない。

    トークン未設定時に素通りさせると、設定漏れがそのまま全開放になる。
    未設定は構成不備として 503 で止める (fail closed)。
    """
    expected = os.getenv(TOKEN_ENV, "")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{TOKEN_ENV} が未設定です。API は無効化されています。",
        )

    header = request.headers.get("Authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization: Bearer <token> が必要です。",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # タイミング差で総当たりの手掛かりを与えないよう定数時間比較する。
    if not secrets.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="トークンが不正です。",
            headers={"WWW-Authenticate": "Bearer"},
        )
