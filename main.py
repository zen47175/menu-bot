import os
import json
import hmac
import hashlib
import base64
import random
import re
import time
import threading
from datetime import datetime, timezone, timedelta

import yt_dlp
from flask import Flask, request, abort
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)

# ── Environment ───────────────────────────────────────────────────────────────
LINE_CHANNEL_SECRET       = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
SPREADSHEET_ID            = os.environ.get("SPREADSHEET_ID", "1KI8MaNWzYegTHrTQNOIUfD26c6b2o6vAOikY2ZqPtlg")
GOOGLE_CREDENTIALS_JSON   = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
GEMINI_API_KEY            = os.environ.get("GEMINI_API_KEY", "")
CRON_SECRET               = os.environ.get("CRON_SECRET", "")
LINE_GROUP_ID             = os.environ.get("LINE_GROUP_ID", "")

SCOPES  = ["https://www.googleapis.com/auth/spreadsheets"]
BKK_TZ  = timezone(timedelta(hours=7))

MENU_POOL_SHEET   = "MenuPool"
SETTINGS_SHEET    = "Settings"
MENU_POOL_HEADERS = [
    "ชื่อเมนู", "มื้อ", "วัตถุดิบ", "วิธีทำ", "แคลอรี่",
    "URL", "วันที่เพิ่ม", "สถานะ", "เป้าหมาย", "ของว่างแนะนำ",
]
STATUS_WAITING = "รอ"
STATUS_USED    = "ใช้แล้ว"

GEMINI_API_BASE  = "https://generativelanguage.googleapis.com/v1beta/models/"
GEMINI_UPLOAD_URL = "https://generativelanguage.googleapis.com/upload/v1beta/files"


# ── Google Sheets ─────────────────────────────────────────────────────────────

def get_sheets_service():
    if GOOGLE_CREDENTIALS_JSON:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    else:
        with open("service-account.json") as f:
            creds_dict = json.load(f)
    creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def ensure_sheet(service, sheet_name: str, headers: list):
    meta  = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    names = [s["properties"]["title"] for s in meta["sheets"]]
    if sheet_name not in names:
        service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
        ).execute()
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{sheet_name}!A1",
            valueInputOption="RAW",
            body={"values": [headers]},
        ).execute()


def get_setting(service, key: str, default: str = "ทั่วไป") -> str:
    try:
        ensure_sheet(service, SETTINGS_SHEET, ["key", "value"])
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range=f"{SETTINGS_SHEET}!A:B"
        ).execute()
        for row in result.get("values", [])[1:]:
            if len(row) >= 2 and row[0] == key:
                return row[1]
    except Exception as e:
        print(f"get_setting error: {e}")
    return default


def set_setting(service, key: str, value: str):
    try:
        ensure_sheet(service, SETTINGS_SHEET, ["key", "value"])
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range=f"{SETTINGS_SHEET}!A:B"
        ).execute()
        rows = result.get("values", [])
        for i, row in enumerate(rows):
            if row and row[0] == key:
                service.spreadsheets().values().update(
                    spreadsheetId=SPREADSHEET_ID,
                    range=f"{SETTINGS_SHEET}!B{i + 1}",
                    valueInputOption="RAW",
                    body={"values": [[value]]},
                ).execute()
                return
        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{SETTINGS_SHEET}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [[key, value]]},
        ).execute()
    except Exception as e:
        print(f"set_setting error: {e}")


def append_menu(service, row: list):
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{MENU_POOL_SHEET}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def get_random_menu(service, meal_type: str):
    """Return (menu_dict, sheet_row_number) for a random unused menu, or (None, None)."""
    goal = get_setting(service, "goal", "ทั่วไป")
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"{MENU_POOL_SHEET}!A:J"
    ).execute()
    rows = result.get("values", [])
    if len(rows) <= 1:
        return None, None

    unused = []
    for i, row in enumerate(rows[1:], start=2):
        status = row[7] if len(row) > 7 else STATUS_WAITING
        if status == STATUS_USED:
            continue
        unused.append((row, i))

    if not unused:
        return None, None

    # Priority: meal_type + goal → meal_type only → goal only → any
    def matches_meal(row):
        return len(row) > 1 and row[1] == meal_type

    def matches_goal(row):
        return goal == "ทั่วไป" or (len(row) > 8 and goal in row[8])

    filtered = [(r, i) for r, i in unused if matches_meal(r) and matches_goal(r)]
    if not filtered:
        filtered = [(r, i) for r, i in unused if matches_meal(r)]
    if not filtered:
        filtered = [(r, i) for r, i in unused if matches_goal(r)]
    if not filtered:
        filtered = unused

    row, row_num = random.choice(filtered)
    menu = {MENU_POOL_HEADERS[j]: row[j] if j < len(row) else "-" for j in range(len(MENU_POOL_HEADERS))}
    return menu, row_num


def mark_menu_used(service, row_num: int):
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{MENU_POOL_SHEET}!H{row_num}",
        valueInputOption="RAW",
        body={"values": [[STATUS_USED]]},
    ).execute()


def count_remaining(service) -> int:
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"{MENU_POOL_SHEET}!H:H"
    ).execute()
    rows = result.get("values", [])
    return sum(1 for r in rows[1:] if not r or r[0] != STATUS_USED)


def count_total(service) -> int:
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"{MENU_POOL_SHEET}!A:A"
    ).execute()
    return max(0, len(result.get("values", [])) - 1)


def reset_all_menus(service):
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"{MENU_POOL_SHEET}!A:A"
    ).execute()
    total = len(result.get("values", [])) - 1
    if total <= 0:
        return
    data = [{"range": f"{MENU_POOL_SHEET}!H{i + 2}", "values": [[STATUS_WAITING]]} for i in range(total)]
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()


# ── Video download (yt-dlp) ───────────────────────────────────────────────────

def download_video(url: str) -> str | None:
    """Download video to /tmp using yt-dlp library. Returns file path or None."""
    ts       = int(time.time())
    out_path = f"/tmp/video_{ts}.mp4"
    ydl_opts = {
        "format":        "best[ext=mp4][filesize<80M]/best[filesize<80M]/best",
        "outtmpl":       out_path,
        "quiet":         True,
        "no_warnings":   True,
        "noplaylist":    True,
        "http_headers":  {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"},
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        if os.path.exists(out_path):
            size_mb = os.path.getsize(out_path) / 1024 / 1024
            print(f"Downloaded {size_mb:.1f}MB → {out_path}")
            return out_path
    except Exception as e:
        print(f"download_video error: {e}")
    # yt-dlp sometimes writes with different extension
    for ext in ["mp4", "webm", "mkv", "mov"]:
        alt = f"/tmp/video_{ts}.{ext}"
        if os.path.exists(alt):
            return alt
    return None


# ── Gemini Files API ──────────────────────────────────────────────────────────

def upload_to_gemini_files(video_path: str) -> tuple:
    """Upload video to Gemini Files API. Returns (file_uri, file_name) or (None, None)."""
    if not GEMINI_API_KEY:
        return None, None
    try:
        file_size = os.path.getsize(video_path)
        # Start resumable upload
        init_resp = requests.post(
            f"{GEMINI_UPLOAD_URL}?key={GEMINI_API_KEY}",
            headers={
                "X-Goog-Upload-Protocol":            "resumable",
                "X-Goog-Upload-Command":             "start",
                "X-Goog-Upload-Header-Content-Length": str(file_size),
                "X-Goog-Upload-Header-Content-Type": "video/mp4",
                "Content-Type":                      "application/json",
            },
            json={"file": {"display_name": "menu_video"}},
            timeout=30,
        )
        if init_resp.status_code != 200:
            print(f"Upload init failed: {init_resp.status_code} {init_resp.text[:200]}")
            return None, None

        upload_url = init_resp.headers.get("X-Goog-Upload-URL")
        if not upload_url:
            return None, None

        # Upload bytes
        with open(video_path, "rb") as f:
            video_bytes = f.read()

        up_resp = requests.post(
            upload_url,
            headers={
                "Content-Length":        str(file_size),
                "X-Goog-Upload-Offset":  "0",
                "X-Goog-Upload-Command": "upload, finalize",
            },
            data=video_bytes,
            timeout=300,
        )
        if up_resp.status_code != 200:
            print(f"Upload data failed: {up_resp.status_code} {up_resp.text[:200]}")
            return None, None

        file_meta = up_resp.json().get("file", {})
        file_uri  = file_meta.get("uri")
        file_name = file_meta.get("name", "")

        # Wait until ACTIVE (usually instant for short clips)
        for _ in range(15):
            state = file_meta.get("state", "ACTIVE")
            if state == "ACTIVE":
                break
            if state == "FAILED":
                print("Gemini file processing FAILED")
                return None, None
            time.sleep(3)
            check = requests.get(
                f"https://generativelanguage.googleapis.com/v1beta/{file_name}?key={GEMINI_API_KEY}",
                timeout=10,
            )
            if check.status_code == 200:
                file_meta = check.json()

        return file_uri, file_name
    except Exception as e:
        print(f"upload_to_gemini_files error: {e}")
        return None, None


def delete_gemini_file(file_name: str):
    if not GEMINI_API_KEY or not file_name:
        return
    try:
        requests.delete(
            f"https://generativelanguage.googleapis.com/v1beta/{file_name}?key={GEMINI_API_KEY}",
            timeout=10,
        )
    except Exception:
        pass


def analyze_video_gemini(file_uri: str) -> dict | None:
    """Ask Gemini Vision to identify food menu from uploaded video. Returns dict or None."""
    if not GEMINI_API_KEY:
        return None

    prompt = """คุณเป็นผู้เชี่ยวชาญด้านอาหาร ดูวิดีโอนี้แล้วสกัดข้อมูลเมนูอาหาร

ตอบเป็น JSON เท่านั้น ห้ามมีข้อความอื่นนอกจาก JSON:
{
  "ชื่อเมนู": "ชื่ออาหาร",
  "มื้อ": "เช้า หรือ กลางวัน หรือ เย็น",
  "วัตถุดิบ": "รายการวัตถุดิบทั้งหมด คั่นด้วยจุลภาค",
  "วิธีทำ": "ขั้นตอนการทำอาหารแบบละเอียด",
  "แคลอรี่": "ตัวเลขแคลอรี่ต่อจาน เช่น 450",
  "เป้าหมาย": "ลดน้ำหนัก หรือ เพิ่มกล้าม หรือ ทั่วไป",
  "ของว่างแนะนำ": "ของว่างหรืออาหารเสริมที่กินคู่กันได้"
}

ถ้าวิดีโอไม่ใช่เมนูอาหาร ตอบ: {"error": "ไม่พบเมนูอาหาร"}"""

    payload = {
        "contents": [{
            "parts": [
                {"file_data": {"mime_type": "video/mp4", "file_uri": file_uri}},
                {"text": prompt},
            ]
        }]
    }

    for model in ["gemini-2.0-flash", "gemini-1.5-flash"]:
        for attempt in range(3):
            try:
                resp = requests.post(
                    f"{GEMINI_API_BASE}{model}:generateContent?key={GEMINI_API_KEY}",
                    json=payload,
                    timeout=90,
                )
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                return _parse_json(raw)
            except Exception as e:
                print(f"analyze_video ({model} #{attempt + 1}): {e}")
                time.sleep(2)
    return None


def call_gemini_text(prompt: str) -> str | None:
    """Call Gemini text API with retry and model fallback."""
    if not GEMINI_API_KEY:
        return None
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    for model in ["gemini-1.5-flash-8b", "gemini-1.5-flash", "gemini-2.0-flash"]:
        for attempt in range(3):
            try:
                resp = requests.post(
                    f"{GEMINI_API_BASE}{model}:generateContent?key={GEMINI_API_KEY}",
                    json=payload,
                    timeout=30,
                )
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            except Exception as e:
                print(f"call_gemini_text ({model} #{attempt + 1}): {e}")
                time.sleep(1)
    return None


def _parse_json(raw: str) -> dict | None:
    try:
        text = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`")
        data = json.loads(text)
        if "error" in data:
            print(f"Gemini returned error: {data['error']}")
            return None
        return data
    except Exception as e:
        print(f"JSON parse error: {e} | raw={raw[:300]}")
        return None


# ── @กิน processing (background thread) ──────────────────────────────────────

def process_gin_url(url: str):
    video_path = None
    file_name  = None
    try:
        # 1. Download
        push_message_to_group("⬇️ กำลังดาวน์โหลดวิดีโอ...")
        video_path = download_video(url)
        if not video_path:
            push_message_to_group(
                "❌ ดาวน์โหลดไม่ได้\n"
                "อาจเป็น private video หรือ link หมดอายุ\n"
                "ลองส่ง link อื่นนะคะ"
            )
            return

        # 2. Upload to Gemini
        push_message_to_group("🤖 กำลังให้ AI วิเคราะห์เมนู...")
        file_uri, file_name = upload_to_gemini_files(video_path)
        if not file_uri:
            push_message_to_group("❌ อัพโหลดวิดีโอให้ AI ไม่สำเร็จ กรุณาลองใหม่")
            return

        # 3. Analyze
        menu = analyze_video_gemini(file_uri)
        if not menu:
            push_message_to_group(
                "❌ AI ระบุเมนูอาหารไม่ได้จากวิดีโอนี้\n"
                "ลองส่ง TikTok/IG ที่เห็นอาหารชัดๆ นะคะ 😊"
            )
            return

        # 4. Save to Sheets
        service   = get_sheets_service()
        ensure_sheet(service, MENU_POOL_SHEET, MENU_POOL_HEADERS)
        today_str = datetime.now(BKK_TZ).strftime("%Y-%m-%d")
        row = [
            menu.get("ชื่อเมนู", "-"),
            menu.get("มื้อ", "เย็น"),
            menu.get("วัตถุดิบ", "-"),
            menu.get("วิธีทำ", "-"),
            str(menu.get("แคลอรี่", "-")),
            url,
            today_str,
            STATUS_WAITING,
            menu.get("เป้าหมาย", "ทั่วไป"),
            menu.get("ของว่างแนะนำ", "-"),
        ]
        append_menu(service, row)

        recipe_preview = str(menu.get("วิธีทำ", "-"))
        if len(recipe_preview) > 200:
            recipe_preview = recipe_preview[:200] + "..."

        msg = (
            f"✅ บันทึกเมนูสำเร็จ!\n\n"
            f"🍽️ {menu.get('ชื่อเมนู', '-')}\n"
            f"🕐 มื้อ: {menu.get('มื้อ', '-')}\n"
            f"🔥 แคลอรี่: {menu.get('แคลอรี่', '-')} kcal\n"
            f"🎯 เหมาะสำหรับ: {menu.get('เป้าหมาย', 'ทั่วไป')}\n\n"
            f"🥘 วัตถุดิบ: {menu.get('วัตถุดิบ', '-')}\n\n"
            f"👨‍🍳 วิธีทำ: {recipe_preview}\n\n"
            f"🥗 ของว่างแนะนำ: {menu.get('ของว่างแนะนำ', '-')}"
        )
        push_message_to_group(msg)

    except Exception as e:
        print(f"process_gin_url error: {e}")
        push_message_to_group("⚠️ เกิดข้อผิดพลาดระหว่างวิเคราะห์ กรุณาลองใหม่อีกครั้ง")
    finally:
        if video_path and os.path.exists(video_path):
            os.remove(video_path)
        if file_name:
            delete_gemini_file(file_name)


def handle_gin_command(url: str, reply_token: str):
    reply_message_line(reply_token,
        "🔍 รับลิงก์แล้ว! กำลังดาวน์โหลดและวิเคราะห์...\n"
        "⏳ อาจใช้เวลา 30-90 วินาที กรุณารอสักครู่นะคะ 🙏"
    )
    threading.Thread(target=process_gin_url, args=(url,), daemon=True).start()


# ── LINE helpers ──────────────────────────────────────────────────────────────

def verify_signature(body: bytes, signature: str) -> bool:
    digest   = hmac.new(LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


def reply_message_line(reply_token: str, text: str):
    r = requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
        json={"replyToken": reply_token, "messages": [{"type": "text", "text": text}]},
    )
    print(f"Reply: {r.status_code}")


def push_message_to_group(text: str, group_id: str = None):
    target = group_id or LINE_GROUP_ID
    if not target:
        print("LINE_GROUP_ID not set")
        return
    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
        json={"to": target, "messages": [{"type": "text", "text": text}]},
    )
    print(f"Push: {r.status_code}")


# ── /send-meal (cron: 09:00 and 15:00 BKK) ───────────────────────────────────

@app.route("/send-meal", methods=["GET", "POST"])
def send_meal():
    secret = request.args.get("secret") or (request.json or {}).get("secret", "")
    if CRON_SECRET and secret != CRON_SECRET:
        abort(403)

    now  = datetime.now(BKK_TZ)
    hour = now.hour

    if 6 <= hour < 12:
        meal_type, emoji, label = "เช้า", "🌅", "เช้า"
    elif 12 <= hour < 18:
        meal_type, emoji, label = "เย็น", "🌇", "บ่าย"
    else:
        meal_type, emoji, label = "เย็น", "🌙", "เย็น"

    try:
        service = get_sheets_service()
        ensure_sheet(service, MENU_POOL_SHEET, MENU_POOL_HEADERS)
        menu, row_num = get_random_menu(service, meal_type)
    except Exception as e:
        print(f"send_meal error: {e}")
        return "error", 500

    if not menu:
        total = count_total(service)
        if total > 0:
            push_message_to_group(
                f"🎉 เมนูทั้งหมด {total} เมนูถูก random ครบแล้ว!\n\n"
                f"จะให้ทำยังไงต่อดีคะ?\n"
                f"🔄 พิมพ์ @รีเซ็ต — เริ่ม random ใหม่ทั้งหมด\n"
                f"▶️ พิมพ์ @ต่อ — วน random เมนูเดิมซ้ำไปเรื่อยๆ"
            )
        else:
            push_message_to_group(
                f"{emoji} ถึงเวลา{label}แล้ว! 🍽️\n\n"
                f"ยังไม่มีเมนูใน MenuPool\n"
                f"ลอง @กิน [ลิงก์] เพื่อเพิ่มเมนูก่อนนะคะ 😊"
            )
        return "ok (no menu)", 200

    try:
        mark_menu_used(service, row_num)
        remaining = count_remaining(service)
    except Exception as e:
        print(f"mark_used error: {e}")
        remaining = "?"

    recipe = str(menu.get("วิธีทำ", "-"))
    if len(recipe) > 300:
        recipe = recipe[:300] + "..."

    msg = (
        f"{emoji} ถึงเวลา{label}แล้ว! 🍽️\n\n"
        f"🍳 เมนูวันนี้: {menu.get('ชื่อเมนู', '-')}\n"
        f"🔥 แคลอรี่: {menu.get('แคลอรี่', '-')} kcal\n"
        f"🎯 เหมาะสำหรับ: {menu.get('เป้าหมาย', 'ทั่วไป')}\n\n"
        f"🥘 วัตถุดิบ:\n{menu.get('วัตถุดิบ', '-')}\n\n"
        f"👨‍🍳 วิธีทำ:\n{recipe}"
    )
    if menu.get("ของว่างแนะนำ") and menu["ของว่างแนะนำ"] != "-":
        msg += f"\n\n🥗 ของว่างแนะนำ: {menu['ของว่างแนะนำ']}"
    if menu.get("URL") and menu["URL"] != "-":
        msg += f"\n\n🔗 ดูต้นฉบับ: {menu['URL']}"
    msg += f"\n\n📊 เมนูที่เหลือ: {remaining} เมนู"

    push_message_to_group(msg)
    return "ok", 200


# ── /webhook ──────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body      = request.get_data()

    if not verify_signature(body, signature):
        abort(400)

    for event in request.json.get("events", []):
        event_type = event.get("type")

        if event_type in ("join", "memberJoined"):
            src = event.get("source", {})
            if src.get("type") == "group":
                print(f"[GROUP JOIN] groupId={src.get('groupId', '')}")

        if event_type == "message" and event["message"].get("type") == "text":
            reply_token = event["replyToken"]
            text        = event["message"]["text"].strip()
            src         = event.get("source", {})

            if src.get("type") == "group":
                print(f"[GROUP MESSAGE] groupId={src.get('groupId', '')}")

            # ── @กิน [URL] ──────────────────────────────────────────────────
            if text.startswith("@กิน"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2 or not parts[1].strip().startswith("http"):
                    reply_message_line(reply_token,
                        "📌 วิธีใช้: @กิน [URL]\n"
                        "รองรับ TikTok, Instagram Reels, YouTube Shorts\n\n"
                        "ตัวอย่าง:\n@กิน https://www.tiktok.com/@xxx/video/123"
                    )
                else:
                    handle_gin_command(parts[1].strip(), reply_token)

            # ── @เป้าหมาย [goal] ────────────────────────────────────────────
            elif text.startswith("@เป้าหมาย"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    reply_message_line(reply_token,
                        "🎯 ตั้งเป้าหมายเพื่อให้บอทแนะนำเมนูที่เหมาะ:\n\n"
                        "@เป้าหมาย ลดน้ำหนัก\n"
                        "@เป้าหมาย เพิ่มกล้าม\n"
                        "@เป้าหมาย ทั่วไป"
                    )
                else:
                    goal = parts[1].strip()
                    try:
                        svc = get_sheets_service()
                        set_setting(svc, "goal", goal)
                        reply_message_line(reply_token,
                            f"✅ ตั้งเป้าหมายเป็น: {goal} 🎯\n"
                            f"บอทจะ random เมนูที่เหมาะกับเป้าหมายนี้ก่อนนะคะ"
                        )
                    except Exception as e:
                        print(f"set goal error: {e}")
                        reply_message_line(reply_token, "⚠️ บันทึกเป้าหมายไม่ได้ กรุณาลองใหม่")

            # ── @รีเซ็ต ─────────────────────────────────────────────────────
            elif text == "@รีเซ็ต":
                try:
                    svc = get_sheets_service()
                    reset_all_menus(svc)
                    total = count_total(svc)
                    reply_message_line(reply_token,
                        f"🔄 รีเซ็ตสำเร็จ!\n"
                        f"เมนูทั้งหมด {total} เมนูพร้อม random ใหม่แล้วค่ะ 🎉"
                    )
                except Exception as e:
                    print(f"reset error: {e}")
                    reply_message_line(reply_token, "⚠️ รีเซ็ตไม่สำเร็จ กรุณาลองใหม่")

            # ── @ต่อ (reset + keep cycling) ──────────────────────────────────
            elif text == "@ต่อ":
                try:
                    svc = get_sheets_service()
                    reset_all_menus(svc)
                    total = count_total(svc)
                    reply_message_line(reply_token,
                        f"▶️ โอเค! รีเซ็ตแล้ว วน random เมนู {total} เมนูซ้ำไปเรื่อยๆ นะคะ 😊"
                    )
                except Exception as e:
                    print(f"ต่อ error: {e}")
                    reply_message_line(reply_token, "⚠️ เกิดข้อผิดพลาด กรุณาลองใหม่")

            # ── @เมนูทั้งหมด ─────────────────────────────────────────────────
            elif text == "@เมนูทั้งหมด":
                try:
                    svc = get_sheets_service()
                    result = svc.spreadsheets().values().get(
                        spreadsheetId=SPREADSHEET_ID,
                        range=f"{MENU_POOL_SHEET}!A:H"
                    ).execute()
                    rows = result.get("values", [])
                    if len(rows) <= 1:
                        reply_message_line(reply_token,
                            "📭 ยังไม่มีเมนูใน MenuPool\n"
                            "ลอง @กิน [ลิงก์] เพื่อเพิ่มเมนูนะคะ"
                        )
                    else:
                        lines = ["📋 เมนูทั้งหมด:\n"]
                        for row in rows[1:]:
                            name   = row[0] if row else "?"
                            status = row[7] if len(row) > 7 else STATUS_WAITING
                            icon   = "✅" if status == STATUS_USED else "🔵"
                            lines.append(f"{icon} {name}")
                        remaining = sum(1 for r in rows[1:] if len(r) <= 7 or r[7] != STATUS_USED)
                        lines.append(f"\n📊 เหลือ {remaining}/{len(rows) - 1} เมนู")
                        reply_message_line(reply_token, "\n".join(lines))
                except Exception as e:
                    print(f"list menus error: {e}")
                    reply_message_line(reply_token, "⚠️ ดึงข้อมูลไม่ได้ กรุณาลองใหม่")

            # ── help ─────────────────────────────────────────────────────────
            elif any(kw in text for kw in ["help", "ช่วย", "วิธีใช้", "คำสั่ง"]):
                reply_message_line(reply_token,
                    "🤖 คำสั่งทั้งหมด:\n\n"
                    "🍽️ @กิน [URL]\n   เพิ่มเมนูจาก TikTok / IG / YouTube\n\n"
                    "🎯 @เป้าหมาย [เป้าหมาย]\n   ตัวอย่าง: ลดน้ำหนัก / เพิ่มกล้าม / ทั่วไป\n\n"
                    "📋 @เมนูทั้งหมด\n   ดูรายการเมนูทั้งหมด\n\n"
                    "🔄 @รีเซ็ต — เริ่ม random ใหม่\n"
                    "▶️ @ต่อ — วนซ้ำเมนูเดิม\n\n"
                    "⏰ แจ้งเตือนอัตโนมัติ:\n"
                    "🌅 09:00 น. — เมนูเช้า\n"
                    "🌇 15:00 น. — เมนูบ่าย"
                )

            # ── default ───────────────────────────────────────────────────────
            else:
                try:
                    svc       = get_sheets_service()
                    remaining = count_remaining(svc)
                    total     = count_total(svc)
                except Exception:
                    remaining = total = "?"
                reply_message_line(reply_token,
                    f"🍽️ สวัสดีค่ะ! วันนี้กินอะไรดี?\n\n"
                    f"📊 เมนูในคลัง: {total} เมนู (รอ random: {remaining})\n\n"
                    f"พิมพ์ help เพื่อดูคำสั่งทั้งหมด 😊"
                )

    return "OK"


# ── Health check ──────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return "วันนี้กินอะไร Bot is running! 🍽️"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
