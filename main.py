import os
import json
import hmac
import hashlib
import base64
import random
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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")
LINE_GROUP_ID = os.environ.get("LINE_GROUP_ID", "")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
BKK_TZ = timezone(timedelta(hours=7))

MENU_POOL_SHEET = "MenuPool"
MENU_POOL_HEADERS = ["ชื่อเมนู", "มื้อ", "วัตถุดิบ", "วิธีทำ", "แคลอรี่", "URL", "วันที่เพิ่ม"]


# ─── Google Sheets ────────────────────────────────────────────────────────────

def get_sheets_service():
    if GOOGLE_CREDENTIALS_JSON:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    else:
        with open("service-account.json") as f:
            creds_dict = json.load(f)
    creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def ensure_menu_pool_sheet(service):
    """Create MenuPool sheet with headers if it doesn't exist."""
    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    sheet_names = [s["properties"]["title"] for s in meta["sheets"]]
    if MENU_POOL_SHEET not in sheet_names:
        body = {"requests": [{"addSheet": {"properties": {"title": MENU_POOL_SHEET}}}]}
        service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body=body).execute()
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{MENU_POOL_SHEET}!A1",
            valueInputOption="RAW",
            body={"values": [MENU_POOL_HEADERS]},
        ).execute()


def append_to_menu_pool(service, row: list):
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{MENU_POOL_SHEET}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def get_random_menu_from_pool(service, meal_type: str) -> dict | None:
    """Return a random menu dict for the given meal_type (เช้า/กลางวัน/เย็น), or None."""
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{MENU_POOL_SHEET}!A:G"
    ).execute()
    rows = result.get("values", [])
    if len(rows) <= 1:
        return None
    matched = [r for r in rows[1:] if len(r) > 1 and r[1] == meal_type]
    if not matched:
        # fallback: any menu
        matched = rows[1:]
    if not matched:
        return None
    row = random.choice(matched)
    keys = MENU_POOL_HEADERS
    return {keys[i]: row[i] if i < len(row) else "-" for i in range(len(keys))}


# ─── Today's daily menu (Sheet1) ──────────────────────────────────────────────

def get_today_menu() -> str:
    today = datetime.now(BKK_TZ).strftime("%Y-%m-%d")
    try:
        service = get_sheets_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Sheet1!A:E"
        ).execute()
        rows = result.get("values", [])
        for row in rows[1:]:
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


# ─── Gemini AI ────────────────────────────────────────────────────────────────

def analyze_url_with_gemini(url: str) -> dict | None:
    """
    Ask Gemini to extract menu info from the given URL via REST API.
    Returns dict with keys: ชื่อเมนู, มื้อ, วัตถุดิบ, วิธีทำ, แคลอรี่
    or None on failure.
    """
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY not set")
        return None

    prompt = f"""คุณเป็นผู้ช่วยด้านอาหาร โปรดเข้าไปดูเนื้อหาจากลิงก์นี้: {url}

จากนั้นสกัดข้อมูลเมนูอาหารออกมาในรูปแบบ JSON ดังนี้ (ตอบเฉพาะ JSON เท่านั้น ไม่ต้องมีคำอธิบายเพิ่มเติม):
{{
  "ชื่อเมนู": "ชื่ออาหาร",
  "มื้อ": "เช้า หรือ กลางวัน หรือ เย็น (เลือกที่เหมาะสมที่สุด)",
  "วัตถุดิบ": "รายการวัตถุดิบทั้งหมด คั่นด้วยจุลภาค",
  "วิธีทำ": "ขั้นตอนการทำอาหาร",
  "แคลอรี่": "ประมาณกี่แคลอรี่ต่อจาน (ตัวเลขเท่านั้น หรือ ไม่ทราบ)"
}}

หากลิงก์ไม่สามารถเข้าถึงได้หรือไม่ใช่เมนูอาหาร ให้ตอบว่า: {{"error": "ไม่สามารถดึงข้อมูลเมนูจากลิงก์นี้ได้"}}"""

    api_url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    try:
        resp = requests.post(api_url, json=payload, timeout=30)
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()
        data = json.loads(text)
        if "error" in data:
            return None
        return data
    except Exception as e:
        print(f"Gemini error: {e}")
        return None


# ─── LINE helpers ─────────────────────────────────────────────────────────────

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
    r = requests.post("https://api.line.me/v2/bot/message/reply", headers=headers, json=body)
    print(f"Reply status: {r.status_code} {r.text}")


def push_message_to_group(text: str, group_id: str = None):
    target = group_id or LINE_GROUP_ID
    if not target:
        print("LINE_GROUP_ID not set, cannot push message")
        return
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    body = {
        "to": target,
        "messages": [{"type": "text", "text": text}],
    }
    r = requests.post("https://api.line.me/v2/bot/message/push", headers=headers, json=body)
    print(f"Push status: {r.status_code} {r.text}")


# ─── Handle @กิน command ──────────────────────────────────────────────────────

def handle_gin_command(url: str, reply_token: str):
    reply_message(reply_token, f"🔍 กำลังวิเคราะห์เมนูจากลิงก์...\n{url}")

    menu_data = analyze_url_with_gemini(url)
    if not menu_data:
        # Send as a new push since reply token was already used
        push_message_to_group("❌ ไม่สามารถดึงข้อมูลเมนูจากลิงก์นี้ได้ กรุณาลองลิงก์อื่น")
        return

    try:
        service = get_sheets_service()
        ensure_menu_pool_sheet(service)
        today_str = datetime.now(BKK_TZ).strftime("%Y-%m-%d")
        row = [
            menu_data.get("ชื่อเมนู", "-"),
            menu_data.get("มื้อ", "เย็น"),
            menu_data.get("วัตถุดิบ", "-"),
            menu_data.get("วิธีทำ", "-"),
            menu_data.get("แคลอรี่", "-"),
            url,
            today_str,
        ]
        append_to_menu_pool(service, row)

        msg = (
            f"✅ บันทึกเมนูสำเร็จ!\n\n"
            f"🍽️ ชื่อ: {menu_data.get('ชื่อเมนู', '-')}\n"
            f"🕐 มื้อ: {menu_data.get('มื้อ', '-')}\n"
            f"🥘 วัตถุดิบ: {menu_data.get('วัตถุดิบ', '-')}\n"
            f"👨‍🍳 วิธีทำ: {menu_data.get('วิธีทำ', '-')}\n"
            f"🔥 แคลอรี่: {menu_data.get('แคลอรี่', '-')} kcal"
        )
        push_message_to_group(msg)
    except Exception as e:
        print(f"Sheets write error: {e}")
        push_message_to_group("⚠️ วิเคราะห์เมนูสำเร็จ แต่บันทึกลง Sheets ไม่ได้ กรุณาตรวจสอบสิทธิ์ service account")


# ─── Scheduled meal push (/send-meal) ─────────────────────────────────────────

@app.route("/send-meal", methods=["GET", "POST"])
def send_meal():
    # Verify secret
    secret = request.args.get("secret") or (request.json or {}).get("secret", "")
    if CRON_SECRET and secret != CRON_SECRET:
        abort(403)

    now = datetime.now(BKK_TZ)
    hour = now.hour

    if 5 <= hour < 11:
        meal_type = "เช้า"
        emoji = "🌅"
    elif 11 <= hour < 15:
        meal_type = "กลางวัน"
        emoji = "☀️"
    else:
        meal_type = "เย็น"
        emoji = "🌙"

    try:
        service = get_sheets_service()
        ensure_menu_pool_sheet(service)
        menu = get_random_menu_from_pool(service, meal_type)
    except Exception as e:
        print(f"send_meal error: {e}")
        return "error", 500

    if not menu:
        push_message_to_group(
            f"{emoji} ถึงเวลา{meal_type}แล้ว! 🍽️\n\n"
            f"ยังไม่มีเมนูใน MenuPool\n"
            f"ลอง @กิน [ลิงก์] เพื่อเพิ่มเมนูเข้าคลังก่อนนะคะ 😊"
        )
        return "ok (no menu)", 200

    msg = (
        f"{emoji} ถึงเวลา{meal_type}แล้ว! 🍽️\n\n"
        f"🍳 เมนูแนะนำ: {menu.get('ชื่อเมนู', '-')}\n"
        f"🥘 วัตถุดิบ: {menu.get('วัตถุดิบ', '-')}\n"
        f"🔥 แคลอรี่: {menu.get('แคลอรี่', '-')} kcal\n\n"
        f"👨‍🍳 วิธีทำ:\n{menu.get('วิธีทำ', '-')}"
    )
    if menu.get("URL") and menu["URL"] != "-":
        msg += f"\n\n🔗 ดูสูตรต้นฉบับ: {menu['URL']}"

    push_message_to_group(msg)
    return "ok", 200


# ─── LINE Webhook ──────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not verify_signature(body, signature):
        abort(400)

    data = request.json
    events = data.get("events", [])

    for event in events:
        event_type = event.get("type")

        # Log group ID when bot joins a group
        if event_type in ("join", "memberJoined"):
            source = event.get("source", {})
            if source.get("type") == "group":
                gid = source.get("groupId", "")
                print(f"[GROUP JOIN] groupId={gid}")

        if event_type == "message" and event["message"].get("type") == "text":
            reply_token = event["replyToken"]
            text = event["message"]["text"].strip()
            source = event.get("source", {})

            # Log group ID from any message
            if source.get("type") == "group":
                gid = source.get("groupId", "")
                print(f"[GROUP MESSAGE] groupId={gid}")

            # @กิน command
            if text.startswith("@กิน"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2 or not parts[1].strip().startswith("http"):
                    reply_message(reply_token, "📌 วิธีใช้: @กิน [URL]\nตัวอย่าง: @กิน https://www.instagram.com/p/xxx")
                else:
                    url = parts[1].strip()
                    handle_gin_command(url, reply_token)

            # เมนูวันนี้ / menu today
            elif any(kw in text for kw in ["เมนู", "กินอะไร", "อาหาร", "วันนี้", "menu"]):
                reply_message(reply_token, get_today_menu())

            # Help
            elif any(kw in text for kw in ["help", "ช่วย", "วิธีใช้", "คำสั่ง"]):
                help_msg = (
                    "🤖 วิธีใช้บอท:\n\n"
                    "📋 ดูเมนูวันนี้:\nพิมพ์ 'เมนู' หรือ 'กินอะไร'\n\n"
                    "➕ เพิ่มเมนูใหม่:\n@กิน [URL]\nตัวอย่าง: @กิน https://www.instagram.com/p/xxx\n\n"
                    "🕐 บอทจะส่งเมนูแนะนำอัตโนมัติทุก:\n"
                    "🌅 เช้า 07:00 น.\n☀️ กลางวัน 12:00 น.\n🌙 เย็น 18:00 น."
                )
                reply_message(reply_token, help_msg)

            else:
                # Default: show today's menu
                reply_message(reply_token, get_today_menu())

    return "OK"


# ─── Health check ──────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return "วันนี้กินอะไร Bot is running! 🍽️"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
