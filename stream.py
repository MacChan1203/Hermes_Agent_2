# stream.py
#
# セキュリティ設計:
#   本サーバは自室のカメラ映像を配信する。到達できる者は室内を覗けるため、
#   認証が唯一の防御になる。したがって:
#     - STREAM_TOKEN 未設定は「認証なしで全公開」ではなく 503 で停止する
#       (fail closed)。旧実装は未設定時に is_authorized() が True を返し、
#       既定の 0.0.0.0 bind と相まって同一ネットワークの誰でも視聴できた
#     - bind 既定はループバック。LAN 公開は明示的なオプトインにする
#     - トークン比較は定数時間で行う
#     - 認証成功後は Cookie を発行し、以後の URL にトークンを載せない
#       (クエリ文字列はアクセスログ・ブラウザ履歴に残るため)
import os
import cv2
import time
import atexit
import secrets
import threading
import urllib.request
from pathlib import Path
from datetime import datetime
from flask import Flask, Response, request, jsonify, abort, make_response

# =========================================================
# 設定
# =========================================================

# 既定はループバックのみ。LAN やインターネットへ公開するのは明示的な
# オプトイン (HOST=0.0.0.0) とする。旧既定は 0.0.0.0 だった。
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "5000"))

CAMERA_INDEX = int(os.getenv("CAMERA_INDEX", "0"))
CAMERA_WIDTH = int(os.getenv("CAMERA_WIDTH", "1280"))
CAMERA_HEIGHT = int(os.getenv("CAMERA_HEIGHT", "720"))
CAMERA_FPS = int(os.getenv("CAMERA_FPS", "20"))

# 画面内の人物検知を有効にする場合は 1
ENABLE_YOLO = os.getenv("ENABLE_YOLO", "1") == "1"

# YOLOモデル。初回起動時にダウンロードされる場合があります
YOLO_MODEL = os.getenv("YOLO_MODEL", "yolo11n.pt")
YOLO_CONFIDENCE = float(os.getenv("YOLO_CONFIDENCE", "0.55"))
DETECT_EVERY_N_FRAMES = int(os.getenv("DETECT_EVERY_N_FRAMES", "5"))

# 動体検知
ENABLE_MOTION = os.getenv("ENABLE_MOTION", "1") == "1"
MOTION_MIN_AREA = int(os.getenv("MOTION_MIN_AREA", "2500"))

# 人物検知時にTelegram通知
ENABLE_TELEGRAM = os.getenv("ENABLE_TELEGRAM", "0") == "1"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# 同じイベントを何度も通知しないための待ち時間
ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "300"))

# 画像保存先
EVENT_DIR = Path(os.getenv("EVENT_DIR", "events"))
EVENT_DIR.mkdir(parents=True, exist_ok=True)

# 認証用トークン。未設定の場合、本サーバは全リクエストを 503 で拒否する。
#   生成例: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
STREAM_TOKEN = os.getenv("STREAM_TOKEN", "")

# 認証成功後に発行する Cookie 名。これにより <img src="/video_feed"> など
# 以後の URL にトークンを載せずに済む。
_TOKEN_COOKIE = "stream_token"

# =========================================================
# Flask
# =========================================================

app = Flask(__name__)

camera = None
camera_lock = threading.Lock()
frame_lock = threading.Lock()

latest_frame = None
latest_jpeg = None
latest_person_count = 0
latest_motion = False
latest_timestamp = None

running = True
frame_number = 0
last_alert_time = 0.0
alert_lock = threading.Lock()

yolo_model = None
yolo_available = False


# =========================================================
# 認証
# =========================================================

def _token_matches(presented: str) -> bool:
    """定数時間でトークンを比較する (応答時間差から桁を推測されないため)。"""
    if not presented:
        return False
    return secrets.compare_digest(presented, STREAM_TOKEN)


def is_authorized():
    """?token= / X-Stream-Token ヘッダ / Cookie のいずれかで認証する。

    STREAM_TOKEN 未設定時に True を返してはいけない。旧実装はそうなっており、
    既定の 0.0.0.0 bind と組み合わさって「設定し忘れると同一ネットワークの
    全員がカメラ映像を見られる」状態だった。未設定は呼び出し側で 503 にする。
    """
    if not STREAM_TOKEN:
        return False

    return (
        _token_matches(request.args.get("token", ""))
        or _token_matches(request.headers.get("X-Stream-Token", ""))
        or _token_matches(request.cookies.get(_TOKEN_COOKIE, ""))
    )


def require_auth():
    # 未設定は「認証なし」ではなく構成不備。設定漏れがそのまま全公開に
    # ならないよう、稼働自体を止める。
    if not STREAM_TOKEN:
        abort(503, description="STREAM_TOKEN が未設定です。配信は無効化されています。")

    if not is_authorized():
        abort(401, description="Unauthorized")


# =========================================================
# Telegram
# =========================================================

def send_telegram_photo(image_path: Path, caption: str):
    if not ENABLE_TELEGRAM:
        return

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram設定が不足しています")
        return

    try:
        boundary = "----CodexCameraBoundary"

        fields = {
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": caption,
        }

        body = bytearray()

        for key, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
            )
            body.extend(str(value).encode())
            body.extend(b"\r\n")

        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            b'Content-Disposition: form-data; '
            b'name="photo"; filename="event.jpg"\r\n'
        )
        body.extend(b"Content-Type: image/jpeg\r\n\r\n")
        body.extend(image_path.read_bytes())
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode())

        url = (
            f"https://api.telegram.org/bot"
            f"{TELEGRAM_BOT_TOKEN}/sendPhoto"
        )

        req = urllib.request.Request(
            url,
            data=bytes(body),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=30) as response:
            response.read()

        print("Telegram通知を送信しました")

    except Exception as exc:
        print(f"Telegram通知エラー: {exc}")


def send_alert_async(image_path: Path, person_count: int, motion: bool):
    global last_alert_time

    now = time.time()

    with alert_lock:
        if now - last_alert_time < ALERT_COOLDOWN_SECONDS:
            return

        last_alert_time = now

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if person_count > 0:
        reason = f"人物を{person_count}人検知"
    elif motion:
        reason = "動きを検知"
    else:
        reason = "イベントを検知"

    caption = f"自室監視通知\n時刻: {timestamp}\n内容: {reason}"

    thread = threading.Thread(
        target=send_telegram_photo,
        args=(image_path, caption),
        daemon=True,
    )
    thread.start()


# =========================================================
# YOLO
# =========================================================

def load_yolo():
    global yolo_model, yolo_available

    if not ENABLE_YOLO:
        print("YOLOは無効です")
        return

    try:
        from ultralytics import YOLO

        print(f"YOLOモデルを読み込み中: {YOLO_MODEL}")
        yolo_model = YOLO(YOLO_MODEL)
        yolo_available = True
        print("YOLO人物検知を有効にしました")

    except Exception as exc:
        yolo_available = False
        print(f"YOLOを読み込めませんでした: {exc}")
        print("動体検知のみで起動します")


def detect_people(frame):
    """
    YOLOで人物だけを検知します。
    COCOクラスの0番がpersonです。
    """
    if not yolo_available or yolo_model is None:
        return frame, 0

    try:
        results = yolo_model.predict(
            source=frame,
            classes=[0],
            conf=YOLO_CONFIDENCE,
            imgsz=640,
            verbose=False,
        )

        result = results[0]
        count = 0
        output = frame.copy()

        if result.boxes is not None:
            for box in result.boxes:
                xyxy = box.xyxy[0].cpu().numpy().astype(int)
                confidence = float(box.conf[0].cpu().numpy())

                x1, y1, x2, y2 = xyxy
                count += 1

                cv2.rectangle(
                    output,
                    (x1, y1),
                    (x2, y2),
                    (0, 0, 255),
                    2,
                )

                label = f"person {confidence:.2f}"

                cv2.putText(
                    output,
                    label,
                    (x1, max(y1 - 10, 20)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (0, 0, 255),
                    2,
                    cv2.LINE_AA,
                )

        return output, count

    except Exception as exc:
        print(f"YOLO検知エラー: {exc}")
        return frame, 0


# =========================================================
# 動体検知
# =========================================================

background_subtractor = cv2.createBackgroundSubtractorMOG2(
    history=300,
    varThreshold=32,
    detectShadows=True,
)


def detect_motion(frame):
    if not ENABLE_MOTION:
        return False, frame

    small = cv2.resize(frame, (640, 360))

    mask = background_subtractor.apply(small)

    # 影を除去
    _, mask = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)

    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (5, 5),
    )

    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_OPEN,
        kernel,
    )

    mask = cv2.dilate(
        mask,
        kernel,
        iterations=2,
    )

    contours, _ = cv2.findContours(
        mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    motion = False

    for contour in contours:
        area = cv2.contourArea(contour)

        if area < MOTION_MIN_AREA:
            continue

        motion = True

        x, y, w, h = cv2.boundingRect(contour)

        # 640x360から元の解像度へ戻す
        scale_x = frame.shape[1] / 640
        scale_y = frame.shape[0] / 360

        x = int(x * scale_x)
        y = int(y * scale_y)
        w = int(w * scale_x)
        h = int(h * scale_y)

        cv2.rectangle(
            frame,
            (x, y),
            (x + w, y + h),
            (0, 255, 255),
            2,
        )

    return motion, frame


# =========================================================
# カメラ処理
# =========================================================

def camera_worker():
    global camera
    global latest_frame
    global latest_jpeg
    global latest_person_count
    global latest_motion
    global latest_timestamp
    global frame_number

    camera = cv2.VideoCapture(CAMERA_INDEX)

    if not camera.isOpened():
        print(f"カメラを開けませんでした: index={CAMERA_INDEX}")
        return

    camera.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    camera.set(cv2.CAP_PROP_FPS, CAMERA_FPS)

    print("カメラを開始しました")
    print(f"解像度: {CAMERA_WIDTH}x{CAMERA_HEIGHT}")

    while running:
        ok, frame = camera.read()

        if not ok:
            print("カメラからフレームを取得できません")
            time.sleep(1)
            continue

        frame_number += 1

        motion, processed_frame = detect_motion(frame)

        person_count = 0

        if (
            yolo_available
            and frame_number % DETECT_EVERY_N_FRAMES == 0
        ):
            processed_frame, person_count = detect_people(
                processed_frame
            )

        # 画面左上に状態を表示
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        status_text = (
            f"{timestamp} | "
            f"person={person_count} | "
            f"motion={motion}"
        )

        cv2.putText(
            processed_frame,
            status_text,
            (15, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )

        ok, encoded = cv2.imencode(
            ".jpg",
            processed_frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), 85],
        )

        if not ok:
            continue

        jpeg = encoded.tobytes()

        with frame_lock:
            latest_frame = processed_frame.copy()
            latest_jpeg = jpeg
            latest_person_count = person_count
            latest_motion = motion
            latest_timestamp = timestamp

        # 人物検知時のみ通知
        if person_count > 0:
            save_event_and_notify(
                processed_frame,
                person_count,
                motion,
            )

        time.sleep(1 / max(CAMERA_FPS, 1))

    if camera is not None:
        camera.release()

    print("カメラを停止しました")


def save_event_and_notify(frame, person_count, motion):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    image_path = EVENT_DIR / f"event_{timestamp}.jpg"

    try:
        cv2.imwrite(str(image_path), frame)

        send_alert_async(
            image_path,
            person_count,
            motion,
        )

    except Exception as exc:
        print(f"イベント保存エラー: {exc}")


# =========================================================
# Flaskルート
# =========================================================

@app.route("/")
def index():
    require_auth()

    # 認証に成功したので Cookie を発行し、以後の URL からトークンを外す。
    # クエリ文字列はアクセスログ・ブラウザ履歴・Referer に残るため、
    # 埋め込みリソース (video_feed / snapshot) すべてに載せるのは避ける。
    body = f"""
<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>自室監視カメラ</title>
<style>
body {{
    background: #111;
    color: #eee;
    font-family: sans-serif;
    margin: 20px;
}}
img {{
    width: 100%;
    max-width: 1280px;
    height: auto;
}}
a {{
    color: #8ecaff;
}}
</style>
</head>
<body>
<h1>自室監視カメラ</h1>
<img src="/video_feed" alt="camera stream">
<p>
<a href="/snapshot" target="_blank">
現在の静止画を表示
</a>
</p>
<p>
<a href="/api/status" target="_blank">
状態を表示
</a>
</p>
</body>
</html>
"""

    response = make_response(body)
    response.set_cookie(
        _TOKEN_COOKIE,
        STREAM_TOKEN,
        httponly=True,      # JavaScript から読めないようにする
        samesite="Strict",  # 他サイト起点のリクエストに Cookie を付けない
        max_age=60 * 60 * 12,
    )
    # トークンがクエリに載っている場合に、外部サイトへ Referer で漏れるのを防ぐ
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.route("/video_feed")
def video_feed():
    require_auth()

    def generate():
        while running:
            with frame_lock:
                jpeg = latest_jpeg

            if jpeg is None:
                time.sleep(0.1)
                continue

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + jpeg
                + b"\r\n"
            )

            time.sleep(0.05)

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/snapshot")
def snapshot():
    require_auth()

    with frame_lock:
        jpeg = latest_jpeg

    if jpeg is None:
        return "画像をまだ取得できません", 503

    return Response(
        jpeg,
        mimetype="image/jpeg",
    )


@app.route("/api/status")
def status():
    require_auth()

    with frame_lock:
        return jsonify({
            "camera_index": CAMERA_INDEX,
            "yolo_available": yolo_available,
            "motion_enabled": ENABLE_MOTION,
            "latest_person_count": latest_person_count,
            "latest_motion": latest_motion,
            "latest_timestamp": latest_timestamp,
        })


# =========================================================
# 終了処理
# =========================================================

def cleanup():
    global running
    running = False

    if camera is not None:
        camera.release()


atexit.register(cleanup)


# =========================================================
# 起動
# =========================================================

if __name__ == "__main__":
    load_yolo()

    worker = threading.Thread(
        target=camera_worker,
        daemon=True,
    )
    worker.start()

    print("")
    print(f"ブラウザ: http://127.0.0.1:{PORT}/?token=<STREAM_TOKEN>")

    if STREAM_TOKEN:
        print("STREAM_TOKENによる認証: 有効")
    else:
        print("=" * 60)
        print("エラー: STREAM_TOKEN が未設定です。全リクエストを 503 で拒否します。")
        print("  生成: python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"")
        print("  設定: export STREAM_TOKEN=<生成した値>")
        print("=" * 60)

    if HOST == "0.0.0.0":
        print("警告: 0.0.0.0 で待ち受けます。同一ネットワークの他端末から到達可能です。")

    app.run(
        host=HOST,
        port=PORT,
        debug=False,
        threaded=True,
        use_reloader=False,
    )


