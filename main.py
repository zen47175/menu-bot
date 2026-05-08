import os
import json
import hmac
import hashlib
import base64
from datetime import datetime, timezone, timedelta

from flask import Flask, request, abort
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "1KI8MaNWzYegTHrTQNOIUfD26c6b2o6vAOikY2ZqPtlg")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
BKK_TZ = timezone(timedelta(hours=7))


def get_sheets_service():
    if GOOGLE_CREDENTIALS_JSON:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    else:
        with open("service-account.json") as f:
            creds_dict = json.load(f)
    creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def get_today_menu() -> str:
    today = datetime.now(BKK_TZ).strftime("%Y-%m-%d")
    try:
        service = get_sheets_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Sheet1!A:E"
        ).execute()
        rows = result.get("values", [])
        for row in rows[1:]:  # skip header
            if row and row[0] == today:
                breakfast = row[1] if len(row) > 1 else "-"
                lunch = row[2] if len(row) > 2 else "-"
                dinner = row[3] if len(row) > 3 else "-"
                note = row[4] if len(row) > 4 and row[4] != "-" else ""
                msg = (
                    f"🍽️ เมนูวันนี้ ({today})\n\n"
                    f"🌅 เช้า: {breakfast}\n"
                    f"☀️ กลางวัน: {lunch}\n"
                    f"🌙 เย็น: {dinner}"
                )
                if note:
                    msg += f"\n📝 หมายเหตุ: {note}"
                return msg
        return f"❌ ยังไม่มีเมนูสำหรับวันที่ {today}\nติดต่อแอดมินเพื่อเพิ่มเมนูค่ะ"
    except Exception as e:
        print(f"Sheets error: {e}")
        return "⚠️ ขออภัย ไม่สามารถดึงข้อมูลเมนูได้ขณะนี้ กรุณาลองใหม่อีกครั้ง"


def verify_signature(body: bytes, signature: str) -> bool:
    hash_val = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(hash_val).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def reply_message(reply_token: str, text: str):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    body = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}],
    }
    requests.post("https://api.line.me/v2/bot/message/reply", headers=headers, json=body)


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not verify_signature(body, signature):
        abort(400)

    events = request.json.get("events", [])
    for event in events:
        if event.get("type") == "message" and event["message"].get("type") == "text":
            reply_token = event["replyToken"]
            reply_message(reply_token, get_today_menu())

    return "OK"


@app.route("/", methods=["GET"])
def health():
    return "วันนี้กินอะไร Bot is running! 🍽️"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
