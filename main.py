import os
import io
import logging
import pandas as pd
import ssl
import xmlrpc.client
import asyncio
import socket
import threading
import time
import urllib.request
import urllib.parse
import requests
import random
import calendar
import difflib
import hashlib
import html
from datetime import datetime, timedelta, time as dt_time
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler, CallbackQueryHandler
import pytz
import json
import re
import unicodedata
from groq import Groq

# ---------------- Trạng thái Hội thoại Lên đơn & Chuyển kho ----------------
LENDON_CUSTOMER, LENDON_REF, LENDON_PRODUCTS = range(3)
CK_PRODUCTS = 3

# ---------------- Config Environment ----------------
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')

# Cấu hình Groq AI
# - Giữ tương thích GROQ_API_KEY_1..3 như cũ.
# - Hỗ trợ thêm GROQ_API_KEY (không đánh số) và GROQ_API_KEY_4..10.
# - Có thể đổi model trên Render bằng biến GROQ_MODEL mà không cần sửa code.
GROQ_MODEL = (os.environ.get('GROQ_MODEL') or 'openai/gpt-oss-120b').strip()

_raw_ai_keys = [os.environ.get('GROQ_API_KEY')] + [
    os.environ.get(f'GROQ_API_KEY_{i}') for i in range(1, 11)
]
AI_KEYS = []
for _key in _raw_ai_keys:
    if _key and _key.strip() and _key.strip() not in AI_KEYS:
        AI_KEYS.append(_key.strip())

current_key_index = 0

ODOO_URL_RAW = os.environ.get('ODOO_URL').rstrip('/') if os.environ.get('ODOO_URL') else None
if ODOO_URL_RAW and ODOO_URL_RAW.lower().endswith('/odoo'):
    ODOO_URL_FINAL = ODOO_URL_RAW[:-len('/odoo')]
else:
    ODOO_URL_FINAL = ODOO_URL_RAW

ODOO_DB = os.environ.get('ODOO_DB')
ODOO_USERNAME = os.environ.get('ODOO_USERNAME')
ODOO_PASSWORD = os.environ.get('ODOO_PASSWORD')

TARGET_MIN_QTY = 50

LOCATION_MAP = {
    'HN_STOCK_CODE': '201/201',
    'HCM_STOCK_CODE': '124/124',
    'HN_TRANSIT_NAME': 'Kho nhập Hà Nội',
}

PRIORITY_LOCATIONS = [
    LOCATION_MAP['HN_STOCK_CODE'],
    LOCATION_MAP['HN_TRANSIT_NAME'],
    LOCATION_MAP['HCM_STOCK_CODE'],
]

PRODUCT_CODE_FIELD = 'default_code'

# ---------------- Logging ----------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Không ghi toàn bộ URL HTTP của Telegram/Groq ra log INFO.
# Với Telegram Bot API, URL có chứa bot token nên không nên xuất hiện trong log vận hành.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

TELEGRAM_SAFE_TEXT_LIMIT = 3500


def _tg_html(value):
    """Escape dynamic text before putting it inside Telegram HTML messages."""
    return html.escape(str(value if value is not None else ""), quote=False)

def _split_telegram_text(text, limit=TELEGRAM_SAFE_TEXT_LIMIT):
    """Chia văn bản dài thành các đoạn an toàn dưới giới hạn 4096 ký tự của Telegram.

    Chỉ dùng cho text thường (không parse_mode) để không làm vỡ Markdown/HTML.
    Ưu tiên cắt ở đoạn, xuống dòng hoặc khoảng trắng để câu dễ đọc.
    """
    text = str(text or "")
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]

    chunks = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit + 1]
        candidates = [
            window.rfind("\n\n"),
            window.rfind("\n"),
            window.rfind(". "),
            window.rfind("! "),
            window.rfind("? "),
            window.rfind(" "),
        ]
        cut = max(candidates)
        # Nếu điểm cắt quá sớm thì cắt cứng ở giới hạn an toàn.
        if cut < int(limit * 0.55):
            cut = limit
        else:
            # Giữ dấu câu ở cuối đoạn nếu cắt theo ". ", "! ", "? ".
            if window[cut:cut+2] in (". ", "! ", "? "):
                cut += 1

        chunk = remaining[:cut].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip()

    if remaining.strip():
        chunks.append(remaining.strip())
    return chunks or [text[:limit]]


async def reply_text_safe(message, text):
    """Gửi text thường an toàn; tự chia nếu AI trả lời vượt giới hạn Telegram."""
    for chunk in _split_telegram_text(text):
        await message.reply_text(chunk)


def _advance_ai_key():
    """Chuyển sang API key kế tiếp; không làm gì nếu chưa cấu hình key."""
    global current_key_index
    if AI_KEYS:
        current_key_index = (current_key_index + 1) % len(AI_KEYS)


def call_groq_chat(messages, temperature=0.0, response_format=None):
    """
    Gọi Groq theo một điểm duy nhất để tất cả tính năng AI dùng chung cơ chế:
    - model cấu hình qua GROQ_MODEL;
    - tự xoay toàn bộ key khi một key/model/request gặp lỗi;
    - không làm rơi luồng nghiệp vụ sang tra tồn khi AI lỗi.
    """
    global current_key_index

    if not AI_KEYS:
        raise RuntimeError(
            "Chưa cấu hình GROQ_API_KEY hoặc GROQ_API_KEY_1..10 trên Environment."
        )

    errors = []
    attempts = len(AI_KEYS)

    for _ in range(attempts):
        key_index = current_key_index % len(AI_KEYS)
        api_key = AI_KEYS[key_index]
        try:
            client = Groq(api_key=api_key)
            kwargs = {
                "model": GROQ_MODEL,
                "messages": messages,
                "temperature": temperature,
            }
            if response_format is not None:
                kwargs["response_format"] = response_format

            completion = client.chat.completions.create(**kwargs)
            content = completion.choices[0].message.content
            if not content:
                raise RuntimeError("Groq trả về nội dung rỗng.")
            return content
        except Exception as e:
            # Không ghi API key ra log. Mọi lỗi đều thử key tiếp theo để tránh
            # tình trạng key mới hợp lệ nhưng bot mắc kẹt ở một key cũ bị lỗi.
            err_text = str(e)
            errors.append(f"key#{key_index + 1}: {err_text}")
            logger.warning(
                "Groq lỗi với key #%s / model %s: %s",
                key_index + 1, GROQ_MODEL, err_text
            )
            _advance_ai_key()

    last_error = errors[-1] if errors else "không xác định"
    raise RuntimeError(
        f"Không gọi được Groq sau {attempts} key. Lỗi cuối: {last_error}"
    )

# =====================================================================
# ---> CẤU HÌNH LƯU TRỮ ĐÁM MÂY (JSONBIN) BẢO TOÀN DỮ LIỆU <---
# =====================================================================
JSONBIN_API_KEY = os.environ.get('JSONBIN_API_KEY')
JSONBIN_BIN_ID = os.environ.get('JSONBIN_BIN_ID')

# Bộ nhớ đệm chạy trên RAM
# ai_memory được tách theo Telegram user_id để nhiều người dùng cùng một bot
# không bị lẫn lịch sử hội thoại với nhau.
cloud_data = {
    "sales_mapping": {},
    "ai_memory": {},
    "sales_monitor": {},
    "odoo_daily_report": {}
}

# ---------------- AI MEMORY (KHÔNG THAY ĐỔI NGHIỆP VỤ ODOO) ----------------
# Chỉ phục vụ lớp giao tiếp tự nhiên. Các rule/command Odoo vẫn chạy như cũ.
AI_MEMORY_RECENT_LIMIT = 16          # 16 message gần nhất (~8 lượt hỏi/đáp)
AI_MEMORY_SUMMARY_TRIGGER = 22       # Quá ngưỡng này mới tóm tắt phần cũ
AI_MEMORY_KEEP_AFTER_SUMMARY = 12    # Sau tóm tắt giữ 12 message mới nhất
AI_MEMORY_MESSAGE_MAX_CHARS = 1800   # Chặn một message quá dài làm phình JSONBin/token
AI_MEMORY_SUMMARY_MAX_CHARS = 1800
AI_ACTION_CONTEXT_MAX_AGE_HOURS = 24
AI_MEMORY_LOCK = threading.Lock()

def load_cloud_db():
    global cloud_data, JSONBIN_BIN_ID
    if not JSONBIN_API_KEY:
        logger.info("Chưa có JSONBIN_API_KEY. Bot sẽ không đồng bộ mây.")
        return
    try:
        if JSONBIN_BIN_ID:
            url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
            headers = {"X-Master-Key": JSONBIN_API_KEY}
            res = requests.get(url, headers=headers)
            if res.status_code == 200:
                record = res.json().get('record')
                if record:
                    cloud_data = record if isinstance(record, dict) else {}
                    # Giữ tương thích dữ liệu JSONBin cũ: bin trước đây có thể chỉ
                    # chứa sales_mapping/price_cache và chưa có ai_memory.
                    cloud_data.setdefault("sales_mapping", {})
                    cloud_data.setdefault("ai_memory", {})
                    cloud_data.setdefault("sales_monitor", {})
                    cloud_data.setdefault("odoo_daily_report", {})
                    if "price_cache" in cloud_data:
                        with open("price_cache.json", 'w', encoding='utf-8') as f:
                            json.dump(cloud_data["price_cache"], f, ensure_ascii=False, indent=4)
                    logger.info("✅ Load Cloud DB thành công! Dữ liệu được bảo toàn.")
    except Exception as e:
        logger.error(f"Lỗi tải Cloud DB: {e}")

async def save_cloud_db(context=None, chat_id=None):
    global JSONBIN_BIN_ID
    if not JSONBIN_API_KEY:
        return
        
    headers = {"X-Master-Key": JSONBIN_API_KEY, "Content-Type": "application/json"}
    try:
        if os.path.exists("price_cache.json"):
            with open("price_cache.json", 'r', encoding='utf-8') as f:
                cloud_data["price_cache"] = json.load(f)

        if JSONBIN_BIN_ID:
            requests.put(f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}", headers=headers, json=cloud_data)
            logger.info("Đã cập nhật dữ liệu lên Đám mây JSONBin.")
        else:
            res = requests.post("https://api.jsonbin.io/v3/b", headers=headers, json=cloud_data)
            if res.status_code == 200:
                JSONBIN_BIN_ID = res.json().get('metadata', {}).get('id')
                logger.info(f"New Bin Created: {JSONBIN_BIN_ID}")
                if context and chat_id:
                    msg = (
                        f"☁️ *HỆ THỐNG ĐÃ TẠO Ổ ĐĨA MÂY MỚI!*\n\n"
                        f"Hãy copy đoạn mã ID này: `{JSONBIN_BIN_ID}`\n"
                        f"Và thêm vào Environment trên Render với tên biến là `JSONBIN_BIN_ID` nhé.\n"
                        f"*(Thêm xong thì Render khởi động lại vô tư, không bao giờ mất Bảng giá hay Báo danh nữa!)*"
                    )
                    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Lỗi lưu Cloud DB: {e}")


def _memory_now_iso():
    return datetime.now(pytz.timezone("Asia/Ho_Chi_Minh")).isoformat(timespec="seconds")


def _clean_memory_text(text, max_chars=AI_MEMORY_MESSAGE_MAX_CHARS):
    """Chuẩn hóa text trước khi lưu để JSONBin không phình vô hạn."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def _get_ai_memory_key(update: Update):
    """Memory bắt buộc tách theo Telegram user_id, không dùng group chat_id."""
    user = getattr(update, "effective_user", None)
    if user and getattr(user, "id", None) is not None:
        return str(user.id)

    # Fallback hiếm gặp (service message). Không ảnh hưởng nghiệp vụ Telegram thường.
    chat = getattr(update, "effective_chat", None)
    if chat and getattr(chat, "id", None) is not None:
        return f"chat:{chat.id}"
    return "unknown"


def _sync_ai_memory_profile(update: Update, odoo_profile=None):
    """
    Tạo/cập nhật hồ sơ giao tiếp cho đúng Telegram user_id.
    Chỉ lưu thông tin nhận diện phục vụ hội thoại; không thay sales_mapping cũ.
    """
    memory_key = _get_ai_memory_key(update)
    user = getattr(update, "effective_user", None)

    with AI_MEMORY_LOCK:
        cloud_data.setdefault("ai_memory", {})
        memory = cloud_data["ai_memory"].setdefault(memory_key, {
            "profile": {},
            "summary": "",
            "style_notes": "",
            "recent_messages": [],
            "last_context": {},
            "updated_at": _memory_now_iso(),
        })

        profile = memory.setdefault("profile", {})
        profile["telegram_user_id"] = memory_key
        if user:
            full_name = " ".join(
                x for x in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if x
            ).strip()
            if full_name:
                profile["telegram_name"] = full_name
            username = getattr(user, "username", None)
            if username:
                profile["telegram_username"] = username

        # Nếu người dùng đã /baodanh thì bổ sung danh tính Odoo vào memory.
        # Không sửa khóa hay cách vận hành sales_mapping hiện hữu.
        if odoo_profile:
            if odoo_profile.get("name"):
                profile["odoo_name"] = str(odoo_profile.get("name"))
            if odoo_profile.get("email"):
                profile["odoo_email"] = str(odoo_profile.get("email"))
            if odoo_profile.get("odoo_user_id") is not None:
                profile["odoo_user_id"] = odoo_profile.get("odoo_user_id")

        memory.setdefault("summary", "")
        memory.setdefault("style_notes", "")
        memory.setdefault("recent_messages", [])
        memory.setdefault("last_context", {})
        memory["updated_at"] = _memory_now_iso()
        return memory_key, memory


def _get_odoo_profile_for_memory(update: Update):
    """Đọc danh tính đã báo danh nếu có, nhưng tuyệt đối không thay cơ chế báo danh cũ."""
    mapping = cloud_data.get("sales_mapping", {})
    candidates = []

    chat = getattr(update, "effective_chat", None)
    if chat and getattr(chat, "id", None) is not None:
        candidates.append(str(chat.id))

    user = getattr(update, "effective_user", None)
    if user and getattr(user, "id", None) is not None:
        candidates.append(str(user.id))

    for key in candidates:
        if key in mapping and isinstance(mapping[key], dict):
            return mapping[key]
    return None


def _append_ai_memory_message(memory_key, role, content):
    content = _clean_memory_text(content)
    if not content:
        return

    with AI_MEMORY_LOCK:
        memory = cloud_data.setdefault("ai_memory", {}).setdefault(memory_key, {
            "profile": {}, "summary": "", "style_notes": "",
            "recent_messages": [], "last_context": {}, "updated_at": _memory_now_iso()
        })
        messages = memory.setdefault("recent_messages", [])

        # Không ghi trùng đúng message cuối (hữu ích khi handler retry).
        if messages and messages[-1].get("role") == role and messages[-1].get("content") == content:
            return

        messages.append({
            "role": role,
            "content": content,
            "ts": _memory_now_iso(),
        })
        memory["updated_at"] = _memory_now_iso()


def _memory_prompt_context(memory):
    profile = memory.get("profile", {}) if isinstance(memory, dict) else {}
    profile_parts = []
    for label, key in [
        ("Tên Telegram", "telegram_name"),
        ("Username", "telegram_username"),
        ("Tên nhân viên Odoo", "odoo_name"),
        ("Email Odoo", "odoo_email"),
    ]:
        value = profile.get(key)
        if value:
            profile_parts.append(f"{label}: {value}")

    summary = _clean_memory_text(memory.get("summary", ""), AI_MEMORY_SUMMARY_MAX_CHARS)
    style = _clean_memory_text(memory.get("style_notes", ""), 900)

    last_context = memory.get("last_context", {}) if isinstance(memory, dict) else {}
    if isinstance(last_context, dict) and last_context.get("action"):
        action_context = json.dumps(last_context, ensure_ascii=False)
    else:
        action_context = "Chưa có hành động gần nhất"

    return {
        "profile": "; ".join(profile_parts) if profile_parts else "Chưa có hồ sơ bổ sung",
        "summary": summary or "Chưa có tóm tắt dài hạn",
        "style": style or "Chưa có ghi chú phong cách ổn định",
        "last_context": action_context,
    }


def _recent_memory_messages(memory):
    raw = memory.get("recent_messages", []) if isinstance(memory, dict) else []
    out = []
    for item in raw[-AI_MEMORY_RECENT_LIMIT:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = _clean_memory_text(item.get("content", ""))
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content})
    return out



def _get_last_action_context(memory_key):
    """Lấy ngữ cảnh hành động gần nhất của riêng Telegram user_id hiện tại."""
    with AI_MEMORY_LOCK:
        memory = cloud_data.get("ai_memory", {}).get(str(memory_key), {})
        ctx = memory.get("last_context", {}) if isinstance(memory, dict) else {}
        return dict(ctx) if isinstance(ctx, dict) else {}


def _context_is_fresh(ctx, max_age_hours=AI_ACTION_CONTEXT_MAX_AGE_HOURS):
    if not isinstance(ctx, dict) or not ctx.get("updated_at"):
        return False
    try:
        updated = datetime.fromisoformat(str(ctx["updated_at"]))
        now = datetime.now(pytz.timezone("Asia/Ho_Chi_Minh"))
        if updated.tzinfo is None:
            updated = pytz.timezone("Asia/Ho_Chi_Minh").localize(updated)
        return (now - updated).total_seconds() <= max_age_hours * 3600
    except Exception:
        return False


def _set_last_action_context(memory_key, action, topic="", entities=None):
    """
    Lưu hành động gần nhất để hiểu các câu nối tiếp như "tra lại đi", "còn cái vừa nãy?".
    Chỉ là metadata hội thoại; không thay đổi dữ liệu/nghiệp vụ Odoo.
    """
    if not memory_key or not action:
        return
    ctx = {
        "action": str(action),
        "topic": _clean_memory_text(topic, 500),
        "entities": entities if isinstance(entities, dict) else {},
        "updated_at": _memory_now_iso(),
    }
    with AI_MEMORY_LOCK:
        memory = cloud_data.setdefault("ai_memory", {}).setdefault(str(memory_key), {
            "profile": {}, "summary": "", "style_notes": "",
            "recent_messages": [], "last_context": {}, "updated_at": _memory_now_iso()
        })
        memory["last_context"] = ctx
        memory["updated_at"] = _memory_now_iso()


def _compact_ai_memory(memory_key):
    """
    Tóm tắt phần hội thoại cũ để giữ trí nhớ dài hạn nhưng không gửi lịch sử vô hạn
    lên Groq. Nếu Groq lỗi thì chỉ cắt bớt lịch sử, không ảnh hưởng bot/Odoo.
    """
    with AI_MEMORY_LOCK:
        memory = cloud_data.get("ai_memory", {}).get(memory_key)
        if not isinstance(memory, dict):
            return
        messages = list(memory.get("recent_messages", []))
        previous_summary = str(memory.get("summary", "") or "")
        previous_style = str(memory.get("style_notes", "") or "")

    if len(messages) <= AI_MEMORY_SUMMARY_TRIGGER:
        return

    old_messages = messages[:-AI_MEMORY_KEEP_AFTER_SUMMARY]
    keep_messages = messages[-AI_MEMORY_KEEP_AFTER_SUMMARY:]
    transcript = "\n".join(
        f"{m.get('role', 'unknown')}: {_clean_memory_text(m.get('content', ''), 900)}"
        for m in old_messages if isinstance(m, dict)
    )

    prompt = f"""
Bạn đang quản lý bộ nhớ dài hạn cho một trợ lý công việc bằng tiếng Việt.
Hãy tóm tắt phần hội thoại cũ thành JSON hợp lệ với đúng 2 khóa:
{{"summary":"...", "style_notes":"..."}}

QUY TẮC:
- summary: giữ các sự kiện, chủ đề đang làm, cách gọi tắt, sở thích/ưu tiên mà người dùng thể hiện rõ, và ngữ cảnh hữu ích cho lần nói chuyện sau.
- style_notes: chỉ ghi cách giao tiếp có thể quan sát được (ví dụ thích ngắn gọn, hay dùng tiếng lóng, thích câu trả lời có số liệu).
- Không chẩn đoán tâm lý, không gắn nhãn tính cách/cảm xúc như một sự thật cố định, không suy diễn thông tin nhạy cảm.
- Không bịa. Nếu chưa chắc thì bỏ qua.
- Viết ngắn, thực dụng. summary tối đa khoảng 900 ký tự, style_notes tối đa khoảng 400 ký tự.

TÓM TẮT CŨ:
{previous_summary}

PHONG CÁCH CŨ:
{previous_style}

HỘI THOẠI CẦN GỘP:
{transcript}
"""

    new_summary = previous_summary
    new_style = previous_style
    try:
        content = call_groq_chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            response_format={"type": "json_object"}
        )
        parsed = json.loads(content)
        new_summary = _clean_memory_text(parsed.get("summary", previous_summary), AI_MEMORY_SUMMARY_MAX_CHARS)
        new_style = _clean_memory_text(parsed.get("style_notes", previous_style), 900)
    except Exception as e:
        logger.warning(f"Không tóm tắt được AI memory {memory_key}: {e}")

    with AI_MEMORY_LOCK:
        memory = cloud_data.get("ai_memory", {}).get(memory_key)
        if isinstance(memory, dict):
            memory["summary"] = new_summary
            memory["style_notes"] = new_style
            memory["recent_messages"] = keep_messages
            memory["updated_at"] = _memory_now_iso()


def _remember_user_text(update: Update, text):
    """
    Ghi câu người dùng vào RAM memory trước khi router xử lý.
    Việc ghi này không thay đổi action/command/logic nghiệp vụ.
    """
    odoo_profile = _get_odoo_profile_for_memory(update)
    memory_key, _ = _sync_ai_memory_profile(update, odoo_profile=odoo_profile)
    _append_ai_memory_message(memory_key, "user", text)
    return memory_key


async def flush_ai_memory_job(context: ContextTypes.DEFAULT_TYPE):
    """Đồng bộ memory định kỳ lên JSONBin để Render Free restart/sleep không làm mất lịch sử.
    Không tham gia định tuyến hay nghiệp vụ Odoo.
    """
    if not JSONBIN_API_KEY:
        return
    try:
        await save_cloud_db()
    except Exception as e:
        logger.warning(f"Lỗi đồng bộ AI memory định kỳ: {e}")


async def generate_personal_chat_response(update: Update, context: ContextTypes.DEFAULT_TYPE, user_input, fallback_response=None):
    """Lớp chat riêng có memory; không thực thi nghiệp vụ Odoo."""
    odoo_profile = _get_odoo_profile_for_memory(update)
    memory_key, memory = _sync_ai_memory_profile(update, odoo_profile=odoo_profile)
    ctx = _memory_prompt_context(memory)

    recent_messages = _recent_memory_messages(memory)
    # _remember_user_text() đã ghi câu hiện tại trước khi router chạy. Khi gửi Groq,
    # bỏ bản cuối nếu đúng là câu hiện tại rồi thêm lại một lần ở cuối.
    current_clean = _clean_memory_text(user_input)
    if recent_messages and recent_messages[-1].get("role") == "user" and recent_messages[-1].get("content") == current_clean:
        recent_messages = recent_messages[:-1]

    system_prompt = f"""
Bạn là lớp GIAO TIẾP TỰ NHIÊN của bot Telegram nội bộ đang làm việc với Odoo.
Các nghiệp vụ thật (tồn kho, đơn hàng, báo cáo, lên đơn, chuyển kho...) đã có router riêng xử lý.
Ở đây bạn chỉ trò chuyện, giải thích và duy trì mạch hội thoại; KHÔNG tự bịa rằng đã thao tác Odoo nếu dữ liệu không được cung cấp.

CÁCH XƯNG HÔ HIỆN CÓ CỦA BOT:
- Xưng "Anh". Có thể gọi người dùng là "con vợ"/"các con vợ" theo phong cách bot cũ khi phù hợp, nhưng đừng nhồi vào mọi câu.

HỒ SƠ NGƯỜI ĐANG NÓI:
{ctx['profile']}

TRÍ NHỚ DÀI HẠN ĐÃ TÓM TẮT:
{ctx['summary']}

GHI CHÚ PHONG CÁCH GIAO TIẾP ĐÃ QUAN SÁT:
{ctx['style']}

NGỮ CẢNH HÀNH ĐỘNG GẦN NHẤT:
{ctx['last_context']}

NGUYÊN TẮC GIAO TIẾP:
1. Dùng lịch sử thật để nối tiếp câu chuyện. Không được giả vờ nhớ điều không có trong memory.
2. Tự điều chỉnh độ dài, độ trang trọng, mức hài hước theo cách người này đang nói và thói quen đã quan sát.
3. Có thể nhận ra tín hiệu tạm thời như người dùng đang gấp, khó chịu, vui hoặc đùa để điều chỉnh cách trả lời; nhưng không tuyên bố/đóng nhãn trạng thái tâm lý là sự thật và không lưu chẩn đoán.
4. Nếu người dùng đang bực hoặc cần xử lý nhanh: vào thẳng vấn đề, hạn chế đùa. Nếu đang nói vui: có thể đáp lại tự nhiên.
5. Ưu tiên ngắn gọn, rõ ràng, thực tế. Chỉ giải thích dài khi người dùng thật sự cần.
6. Không dùng lịch sử của người khác. Memory hiện tại chỉ thuộc Telegram user_id này.
7. Nếu câu hỏi cần dữ liệu Odoo/thời gian thực mà không có trong ngữ cảnh, nói rõ cần dùng chức năng tương ứng thay vì tự bịa số liệu.
"""

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(recent_messages)
    messages.append({"role": "user", "content": current_clean})

    try:
        answer = await asyncio.to_thread(call_groq_chat, messages, 0.55)
    except Exception as e:
        logger.error(f"Lỗi AI chat có memory: {e}")
        answer = fallback_response or "⚠️ AI hội thoại đang không phản hồi, nhưng các chức năng Odoo vẫn hoạt động bình thường."

    answer = str(answer).strip()
    _append_ai_memory_message(memory_key, "assistant", answer)
    _compact_ai_memory(memory_key)
    return answer


# ---------------- TÍNH NĂNG: AI & XỬ LÝ EXCEL ----------------
PRICE_DATA_FILE = "price_cache.json"

def process_price_excel(file_bytes):
    try:
        xl = pd.ExcelFile(io.BytesIO(file_bytes))
        sheet_names = xl.sheet_names
        
        target_sheet = None
        max_date = None
        pattern = re.compile(r'T(\d+)[\.,_\-\s](\d+)', re.IGNORECASE)
        
        for name in sheet_names:
            match = pattern.search(name)
            if match:
                try:
                    month = int(match.group(1))
                    year = int(match.group(2))
                    current_date = datetime(year, month, 1)
                    if max_date is None or current_date > max_date:
                        max_date = current_date
                        target_sheet = name
                except ValueError:
                    continue
        
        if not target_sheet:
            target_sheet = sheet_names[0]
            logger.info(f"Dùng sheet đầu tiên: {target_sheet}")
        else:
            logger.info(f"Dùng sheet mới nhất: {target_sheet}")

        df_raw = pd.read_excel(io.BytesIO(file_bytes), sheet_name=target_sheet, header=None)
        
        header_row_idx = 0
        found_header = False
        
        for idx, row in df_raw.iterrows():
            row_list = [str(val).lower() for val in row.values]
            row_str = " ".join(row_list)
            
            if "niêm yết" in row_str:
                header_row_idx = idx
                found_header = True
                break
            elif "mã hàng" in row_str or "mã sp" in row_str:
                if not found_header:
                    header_row_idx = idx
        
        if not found_header:
            return False, f"Không tìm thấy dòng tiêu đề hợp lệ trong sheet {target_sheet}"

        df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=target_sheet, header=header_row_idx)
        
        if header_row_idx > 0:
            for i, col_name in enumerate(df.columns):
                if str(col_name).startswith('Unnamed') or str(col_name).lower() == 'nan':
                    val_above = str(df_raw.iloc[header_row_idx - 1, i]).strip()
                    if val_above and val_above.lower() != 'nan':
                        df.columns.values[i] = val_above

        df.columns = [str(c).strip() for c in df.columns]
        
        ma_hang_col = next((c for c in df.columns if 'mã hàng' in c.lower() or 'mã sp' in c.lower()), None)
        
        if ma_hang_col:
            df = df.dropna(subset=[ma_hang_col])
            data_dict = df.astype(str).to_dict(orient='records')
            
            cache_data = {
                "sheet_name": target_sheet,
                "data": data_dict
            }
            
            with open(PRICE_DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=4)
                
            return True, f"{len(df)} dòng (Sheet: {target_sheet})"
        
        return False, f"Lỗi cấu trúc cột trong sheet {target_sheet}"

    except Exception as e:
        logger.error(f"Lỗi nạp bảng giá: {e}")
        return False, str(e)

def _safe_number(value):
    """Đọc số từ dữ liệu Excel đã ép sang chuỗi; trả None nếu không phải số tiền hợp lệ."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in ('nan', 'none', 'null'):
        return None

    # Excel thường lưu số thành 525909 hoặc 525909.0. Với chuỗi có cả dấu
    # chấm/phẩy, ưu tiên cách đọc phổ biến của dữ liệu số do pandas sinh ra.
    try:
        cleaned = s.replace(' ', '')
        if ',' in cleaned and '.' not in cleaned:
            # 525,909 -> 525909; 525,5 -> 525.5 (phân biệt theo phần sau dấu phẩy)
            tail = cleaned.rsplit(',', 1)[-1]
            cleaned = cleaned.replace(',', '.') if len(tail) <= 2 else cleaned.replace(',', '')
        else:
            cleaned = cleaned.replace(',', '')
        number = float(cleaned)
        return number
    except Exception:
        m = re.search(r'-?\d+(?:[\.,]\d+)?', s)
        if not m:
            return None
        try:
            return float(m.group(0).replace(',', '.'))
        except Exception:
            return None


def _format_money(value):
    number = _safe_number(value)
    if number is None or number < 1000:
        return "Chưa có thông tin"
    rounded = int(round(number / 1000.0) * 1000)
    return f"{rounded:,.0f}".replace(',', '.')


def _fallback_price_response(found_item, sheet_name):
    """Fallback không dùng AI để chức năng báo giá vẫn hoạt động khi Groq lỗi."""
    entries = [(str(k), v) for k, v in found_item.items() if 'unnamed' not in str(k).lower()]

    def norm(s):
        s = unicodedata.normalize('NFD', str(s).lower())
        return ''.join(c for c in s if unicodedata.category(c) != 'Mn')

    def pick(predicate, prefer_last=False):
        found = [(k, v) for k, v in entries if predicate(norm(k))]
        if not found:
            return None
        return found[-1][1] if prefer_last else found[0][1]

    code = None
    for k, v in entries:
        nk = norm(k)
        if 'ma hang' in nk or 'ma sp' in nk or 'ma san pham' in nk:
            code = str(v).strip()
            break
    code = code or "Sản phẩm"

    niem_yet = pick(lambda k: 'niem yet' in k)
    vat10 = pick(lambda k: ('gia nhap' in k and 'vat 10' in k) or '+vat 10' in k)
    vat8 = pick(lambda k: ('gia moi' in k and 'vat 8' in k) or ('gia nhap' in k and 'bao gom vat' in k))

    # Pandas đặt tên cột trùng dạng "- VAT", "- VAT.1"...; ưu tiên cột sau.
    no_vat_candidates = [(k, v) for k, v in entries if norm(k).strip().startswith('- vat')]
    no_vat = no_vat_candidates[-1][1] if no_vat_candidates else None

    return (
        f"📦 *{code}*\n"
        f"📅 Bảng giá tháng ({sheet_name})\n"
        f"💰 *Giá nhập:*\n"
        f"- *VAT 10%: * {_format_money(vat10)} VNĐ\n"
        f"- *VAT 8%: * {_format_money(vat8)} VNĐ\n"
        f"- *Giá niêm yết: * {_format_money(niem_yet)} VNĐ\n"
        f"- *Giá chưa VAT: * {_format_money(no_vat)} VNĐ"
    )


def ask_groq_ai(query):
    if not os.path.exists(PRICE_DATA_FILE):
        return "Anh chưa có dữ liệu bảng giá. Các con vợ gửi file Excel để nạp nhé!"

    try:
        with open(PRICE_DATA_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)

        if isinstance(cache, list):
            full_data = cache
            sheet_name = "Mới nhất"
        else:
            full_data = cache.get("data", [])
            sheet_name = cache.get("sheet_name", "Mới nhất")

        query_upper = query.upper()
        found_item = None

        for item in full_data:
            key_ma = next((k for k in item.keys() if "mã" in k.lower() and ("hàng" in k.lower() or "sp" in k.lower())), None)
            if key_ma:
                ma_sp = str(item[key_ma]).upper().strip()
                if ma_sp and ma_sp in query_upper:
                    found_item = item
                    break

        if not found_item:
            return "Anh không tìm thấy mã hàng này trong bảng giá."

        clean_info = {k: v for k, v in found_item.items() if str(v).lower() != 'nan' and 'unnamed' not in str(k).lower()}

        prompt = f"""
        Dữ liệu sản phẩm: {clean_info}
        Tên bảng giá: {sheet_name}
        Câu hỏi: "{query}"

        NHIỆM VỤ: Trả lời chính xác theo FORM mẫu bên dưới.

        QUY TẮC XỬ LÝ SỐ LIỆU (BẮT BUỘC):
        1. *CHẶN SỐ RÁC:* Bất kỳ con số nào nhỏ hơn 1000 (Ví dụ: 0, 0.3, 0.15, 30, 40) => ĐÓ LÀ CHIẾT KHẤU HOẶC RÁC. BỎ QUA NGAY.
        2. *TÌM CỘT GIÁ:*
           - "Giá niêm yết": Cột 'Niêm Yết'.
           - "Giá nhập (VAT 10%)": Cột 'Giá nhập (+VAT 10%)' hoặc tương tự.
           - "VAT 8%": Cột 'Giá Mới (VAT 8%)' hoặc 'Giá nhập (Bao gồm VAT)'.
           - "Giá chưa VAT": Cột '- VAT' (giá cũ) hoặc '- VAT.1' (giá mới 8%). Ưu tiên lấy giá ở cột '- VAT.1' (cột sau) nếu có.
        3. *LÀM TRÒN:* Luôn làm tròn số đến hàng nghìn (VD: 525909 -> 526.000).
        4. Nếu một loại giá là 0 hoặc không tìm thấy, ghi "Chưa có thông tin".

        FORM TRẢ LỜI (Copy y nguyên):
        📦 *[Mã SP]*
        📅 Bảng giá tháng ({sheet_name})
        💰 *Giá nhập:*
        - *VAT 10%: * [Số tiền] VNĐ
        - *VAT 8%: * [Số tiền] VNĐ
        - *Giá niêm yết: * [Số tiền] VNĐ
        - *Giá chưa VAT: * [Số tiền] VNĐ
        """

        try:
            return call_groq_chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0
            )
        except Exception as e:
            logger.error(f"AI báo giá không khả dụng, dùng fallback cục bộ: {e}")
            return _fallback_price_response(found_item, sheet_name)

    except Exception as e:
        return f"Lỗi hệ thống: {e}"

def _weather_code_to_vi(code):
    mapping = {
        0: "trời quang",
        1: "chủ yếu quang", 2: "có mây", 3: "nhiều mây",
        45: "sương mù", 48: "sương mù đóng băng",
        51: "mưa phùn nhẹ", 53: "mưa phùn", 55: "mưa phùn dày",
        56: "mưa phùn lạnh nhẹ", 57: "mưa phùn lạnh",
        61: "mưa nhẹ", 63: "mưa vừa", 65: "mưa to",
        66: "mưa lạnh nhẹ", 67: "mưa lạnh",
        71: "tuyết nhẹ", 73: "tuyết vừa", 75: "tuyết dày", 77: "hạt tuyết",
        80: "mưa rào nhẹ", 81: "mưa rào", 82: "mưa rào mạnh",
        85: "mưa tuyết nhẹ", 86: "mưa tuyết mạnh",
        95: "dông", 96: "dông kèm mưa đá nhẹ", 99: "dông kèm mưa đá mạnh",
    }
    try:
        return mapping.get(int(code), "thời tiết thay đổi")
    except Exception:
        return "thời tiết thay đổi"


def _extract_weather_location(user_input, default_location="Hà Nội"):
    """
    Lấy địa điểm khỏi câu hỏi thời tiết mà không nhầm các cụm như
    "hôm nay thế nào mày" thành tên địa phương.
    """
    original = str(user_input or "").strip()
    m = re.search(r'(?:thời\s*tiết|thoi\s*tiet)(.*)$', original, flags=re.IGNORECASE)
    if not m:
        return default_location

    rest = m.group(1).strip(" ,:;?!.")
    if not rest:
        return default_location

    # Nếu có "ở/tại" thì phần sau đó có độ tin cậy cao nhất.
    m_explicit = re.search(r'(?:^|\s)(?:ở|o|tại|tai)\s+(.+)$', rest, flags=re.IGNORECASE)
    candidate = m_explicit.group(1).strip() if m_explicit else rest

    # Bỏ các cụm thời gian ở đầu.
    candidate = re.sub(
        r'^(?:hôm\s*nay|hom\s*nay|ngày\s*mai|ngay\s*mai|hôm\s*qua|hom\s*qua)\s*',
        '', candidate, flags=re.IGNORECASE
    ).strip()

    # Cắt phần câu hỏi/filler ở cuối, giữ lại địa danh.
    candidate = re.split(
        r'\b(?:hôm\s*nay|hom\s*nay|ngày\s*mai|ngay\s*mai|hôm\s*qua|hom\s*qua|'
        r'thế\s*nào|the\s*nao|thì\s*sao|thi\s*sao|ra\s*sao|như\s*thế\s*nào|nhu\s*the\s*nao|'
        r'mày|may|vậy|vay|nhé|nhe|đi|di)\b',
        candidate, maxsplit=1, flags=re.IGNORECASE
    )[0].strip(" ,:;?!.")

    norm = _normalize_vn_text(candidate)
    if not candidate or norm in {"hom nay", "ngay mai", "hom qua", "the nao", "ra sao"}:
        return default_location
    return candidate


def _looks_like_html_payload(text, content_type=""):
    """Chặn trường hợp API/fallback trả trang HTML nhưng HTTP vẫn là 200."""
    body = str(text or "").lstrip()
    sample = body[:500].lower()
    ctype = str(content_type or "").lower()
    return (
        "text/html" in ctype
        or sample.startswith("<!doctype html")
        or sample.startswith("<html")
        or "<head" in sample
        or "<body" in sample
    )


def get_realtime_weather(location="Hà Nội"):
    """
    Nguồn 1: Open-Meteo (không cần API key) -> Nguồn 2: wttr.in -> Nguồn 3: web search.
    Trả dict có cấu trúc để phần trả lời không phải nhờ AI bịa/diễn giải số liệu.
    """
    location = str(location or "Hà Nội").strip() or "Hà Nội"

    # --- Nguồn 1: Open-Meteo ---
    try:
        # Một số dịch vụ geocoding ổn định hơn với tên không dấu, nên thử cả hai.
        geo_names = [location]
        location_ascii = _normalize_vn_text(location)
        if location_ascii and location_ascii.casefold() != location.casefold():
            geo_names.append(location_ascii)

        results = []
        for geo_name in geo_names:
            geo_res = requests.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": geo_name, "count": 5, "language": "vi", "format": "json"},
                timeout=7,
            )
            if geo_res.status_code != 200:
                logger.warning(
                    f"Open-Meteo geocoding status={geo_res.status_code} cho {geo_name}"
                )
                continue
            try:
                results = geo_res.json().get("results") or []
            except Exception:
                results = []
            if results:
                break

        if results:
            # Nếu có kết quả ở Việt Nam thì ưu tiên; nếu không dùng kết quả đầu tiên
            # để vẫn hỗ trợ Tokyo, Seoul, Bangkok... khi người dùng hỏi.
            place = next((x for x in results if x.get("country_code") == "VN"), results[0])
            lat = place.get("latitude")
            lon = place.get("longitude")
            tz = place.get("timezone") or "auto"

            if lat is not None and lon is not None:
                forecast_res = requests.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params={
                        "latitude": lat,
                        "longitude": lon,
                        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
                        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                        "timezone": tz,
                        "forecast_days": 1,
                    },
                    timeout=7,
                )
                if forecast_res.status_code == 200:
                    data = forecast_res.json()
                    current = data.get("current") or {}
                    daily = data.get("daily") or {}
                    display_parts = [place.get("name")]
                    admin1 = place.get("admin1")
                    country = place.get("country")
                    if admin1 and admin1 != place.get("name"):
                        display_parts.append(admin1)
                    if country and country not in display_parts:
                        display_parts.append(country)
                    display_name = ", ".join(x for x in display_parts if x) or location

                    def first_value(key):
                        value = daily.get(key)
                        if isinstance(value, list) and value:
                            return value[0]
                        return None

                    return {
                        "ok": True,
                        "source": "Open-Meteo",
                        "location": display_name,
                        "temperature": current.get("temperature_2m"),
                        "apparent_temperature": current.get("apparent_temperature"),
                        "humidity": current.get("relative_humidity_2m"),
                        "wind_speed": current.get("wind_speed_10m"),
                        "weather_code": current.get("weather_code"),
                        "temp_max": first_value("temperature_2m_max"),
                        "temp_min": first_value("temperature_2m_min"),
                        "rain_probability": first_value("precipitation_probability_max"),
                        "observed_at": current.get("time"),
                    }
                logger.warning(
                    f"Open-Meteo forecast status={forecast_res.status_code} cho {location}"
                )
        else:
            logger.warning(f"Open-Meteo không tìm thấy địa điểm: {location}")
    except Exception as e:
        logger.warning(f"Open-Meteo lỗi cho {location}: {e}")

    # --- Nguồn 2: wttr.in ---
    try:
        # wttr.in đôi lúc trả nguyên trang HTML với HTTP 200. Chỉ nhận plain text thật.
        url = f"https://wttr.in/{urllib.parse.quote(location, safe='')}"
        res = requests.get(
            url,
            params={
                "format": "%l: %C, Nhiệt độ: %t, Cảm giác như: %f, Độ ẩm: %h",
                "m": "",
            },
            timeout=7,
            headers={
                "User-Agent": "curl/8.5.0",
                "Accept": "text/plain",
            },
        )
        body = res.text.strip()
        content_type = res.headers.get("Content-Type", "")
        if (
            res.status_code == 200
            and body
            and not _looks_like_html_payload(body, content_type)
        ):
            return {
                "ok": True,
                "source": "wttr.in",
                "location": location,
                "raw": body,
            }

        logger.warning(
            "wttr.in trả dữ liệu không hợp lệ cho %s: status=%s, content-type=%s, sample=%r",
            location,
            res.status_code,
            content_type,
            body[:100],
        )
    except Exception as e:
        logger.warning(f"wttr.in lỗi cho {location}: {e}")

    # --- Nguồn 3: web search ---
    try:
        web_data = perform_web_search(f"thời tiết {location} hôm nay")
        if web_data and not web_data.startswith("Lỗi") and "Không tìm thấy" not in web_data:
            return {
                "ok": False,
                "source": "web_search",
                "location": location,
                "raw": web_data,
                "error": "Hai nguồn thời tiết trực tiếp không phản hồi.",
            }
    except Exception as e:
        logger.warning(f"Weather web fallback lỗi cho {location}: {e}")

    return {
        "ok": False,
        "source": "none",
        "location": location,
        "error": "Không lấy được dữ liệu thời tiết từ các nguồn hiện có.",
    }


def _fmt_weather_number(value, digits=0):
    if value is None:
        return None
    try:
        value = float(value)
        return f"{value:.{digits}f}"
    except Exception:
        return None


def format_weather_response(weather_data):
    """Trả lời thời tiết trực tiếp, ngắn và dựa trên số liệu; không cần Groq."""
    if not isinstance(weather_data, dict):
        return str(weather_data)

    if weather_data.get("ok") and weather_data.get("source") == "Open-Meteo":
        loc = weather_data.get("location") or "địa điểm đã chọn"
        temp = _fmt_weather_number(weather_data.get("temperature"))
        feel = _fmt_weather_number(weather_data.get("apparent_temperature"))
        hum = _fmt_weather_number(weather_data.get("humidity"))
        wind = _fmt_weather_number(weather_data.get("wind_speed"))
        tmin = _fmt_weather_number(weather_data.get("temp_min"))
        tmax = _fmt_weather_number(weather_data.get("temp_max"))
        rain = _fmt_weather_number(weather_data.get("rain_probability"))
        condition = _weather_code_to_vi(weather_data.get("weather_code"))

        first = f"🌤 {loc}: {condition}"
        if temp is not None:
            first += f", {temp}°C"
        if feel is not None:
            first += f" (cảm giác {feel}°C)"
        first += "."

        second_bits = []
        if hum is not None:
            second_bits.append(f"Độ ẩm {hum}%")
        if wind is not None:
            second_bits.append(f"gió {wind} km/h")
        second = ", ".join(second_bits)
        if second:
            second = second[0].upper() + second[1:] + "."

        third_bits = []
        if tmin is not None and tmax is not None:
            third_bits.append(f"Hôm nay khoảng {tmin}–{tmax}°C")
        if rain is not None:
            third_bits.append(f"khả năng mưa cao nhất {rain}%")
        third = ", ".join(third_bits)
        if third:
            third += "."

        return " ".join(x for x in [first, second, third] if x)

    if weather_data.get("ok") and weather_data.get("raw"):
        return f"🌤 {weather_data['raw']}"

    if weather_data.get("source") == "web_search" and weather_data.get("raw"):
        # Web search chỉ là phương án cuối; không giả vờ đây là số liệu thời tiết chính xác.
        lines = [x.strip() for x in str(weather_data["raw"]).splitlines() if x.strip()]
        snippets = [x for x in lines if not x.startswith("📰") and not x.startswith("🌐")][:2]
        detail = " ".join(snippets)
        if len(detail) > 500:
            detail = detail[:500].rstrip() + "…"
        return f"⚠️ Nguồn thời tiết trực tiếp đang lỗi. Kết quả web gần nhất cho {weather_data.get('location')}: {detail}"

    return f"⚠️ Chưa lấy được thời tiết của {weather_data.get('location', 'địa điểm này')} lúc này."

def perform_web_search(query):
    """Sử dụng duckduckgo-search phiên bản mở rộng để lấy nhiều tin tức hơn"""
    try:
        from duckduckgo_search import DDGS
        info = ""
        with DDGS() as ddgs:
            news_results = list(ddgs.news(query, region='wt-wt', safesearch='off', timelimit='d', max_results=5))
            if news_results:
                info += "📰 *TIN TỨC MỚI NHẤT:*\n"
                for res in news_results:
                    info += f"- {res.get('title', '')}: {res.get('body', '')}\n"
            
            web_results = list(ddgs.text(query, region='wt-wt', safesearch='off', timelimit='d', max_results=3))
            if web_results:
                info += "\n🌐 *THÔNG TIN WEB BỔ SUNG:*\n"
                for res in web_results:
                    info += f"- {res.get('title', '')}: {res.get('body', '')}\n"
        
        if not info.strip():
            return "Không tìm thấy thông tin mới nhất trên mạng cho từ khóa này."
        
        return info
    except ImportError:
        return "Các con vợ ơi, Anh chưa lướt web được! Thêm 'duckduckgo-search' vào file requirements.txt rồi deploy lại nhé."
    except Exception as e:
        return f"Lỗi khi lướt web tìm kiếm: {e}"

def _normalize_vn_text(value):
    """Chuẩn hóa tiếng Việt để so khớp câu lệnh nhưng không làm thay đổi nội dung gốc."""
    s = unicodedata.normalize('NFD', str(value).lower())
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    s = s.replace('đ', 'd')
    return re.sub(r'\s+', ' ', s).strip()


def _looks_like_product_code(text):
    """Chỉ coi chuỗi liền có ít nhất một chữ số là mã SP; tránh biến câu tự nhiên thành mã."""
    s = str(text).strip()
    if not s or len(s) > 40 or re.search(r'\s', s):
        return False
    if not re.search(r'\d', s):
        return False
    return re.fullmatch(r'[A-Za-z0-9._/\-]+', s) is not None


def _contains_product_code_in_text(text):
    """Nhận diện mã SP nằm trong câu hỏi giá, ví dụ: 'AC-161 giá bao nhiêu'."""
    for token in re.findall(r'[A-Za-z][A-Za-z0-9._/\-]*\d[A-Za-z0-9._/\-]*', str(text)):
        if _looks_like_product_code(token):
            return True
    return False


def _parse_date_token(token, default_month, default_year):
    nums = [int(x) for x in re.findall(r'\d+', token)]
    if not nums:
        return None

    if len(nums) >= 3:
        a, b, c = nums[0], nums[1], nums[2]
        # Hỗ trợ cả YYYY-MM-DD lẫn DD/MM/YYYY.
        if a >= 1000:
            year, month, day = a, b, c
        else:
            day, month, year = a, b, c
            if year < 100:
                year += 2000
    elif len(nums) == 2:
        day, month = nums
        year = default_year
    else:
        day = nums[0]
        month = default_month
        year = default_year

    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def _extract_date_range(user_input):
    """Hiểu các kiểu: từ ngày 2 đến ngày 20, 2/9 đến 20/9, 2026-09-02 đến 2026-09-20."""
    tz_vn = pytz.timezone("Asia/Ho_Chi_Minh")
    now = datetime.now(tz_vn)
    norm = _normalize_vn_text(user_input)

    m = re.search(
        r'(?:tu\s+(?:ngay\s+)?)'
        r'(?P<start>\d{1,4}(?:[./-]\d{1,2})?(?:[./-]\d{1,4})?)'
        r'\s*(?:den|toi)\s*(?:ngay\s+)?'
        r'(?P<end>\d{1,4}(?:[./-]\d{1,2})?(?:[./-]\d{1,4})?)',
        norm
    )
    if not m:
        return None

    start_token = m.group('start')
    end_token = m.group('end')
    start_dt = _parse_date_token(start_token, now.month, now.year)
    if not start_dt:
        return None

    # Nếu ngày kết thúc không ghi tháng/năm thì dùng tháng/năm của ngày bắt đầu.
    end_dt = _parse_date_token(end_token, start_dt.month, start_dt.year)
    if not end_dt:
        return None

    return start_dt.strftime('%Y-%m-%d'), end_dt.strftime('%Y-%m-%d')


def _looks_like_followup(user_input):
    norm = _normalize_vn_text(user_input).strip(" ?!.,;:")
    if not norm or len(norm) > 90:
        return False
    patterns = [
        r'^(tra|tim|xem|kiem tra|cap nhat|thu|lam)\s+(lai|cho ro)',
        r'^(tra cho ro|tra ro hon|noi ro hon|xem ro hon)',
        r'^(con|the con|vay con|the|vay)\b',
        r'^(o|tai)\s+.+(?:thi sao|the nao|ra sao)$',
        r'^(hom qua|hom nay|ngay mai)\b.*(?:thi sao|the nao|ra sao|sao)',
        r'^(roi sao|sao roi|the nao|ra sao|co gi moi|moi nhat)$',
        r'^(cai vua nay|cai luc nay|cai tren|no)\b.*',
    ]
    return any(re.search(p, norm) for p in patterns)


def _followup_intent(user_input, last_context):
    """Nối câu ngắn với hành động gần nhất, chỉ với các action đọc dữ liệu an toàn."""
    if not _looks_like_followup(user_input):
        return None
    if not _context_is_fresh(last_context):
        return None

    action = last_context.get("action")
    entities = dict(last_context.get("entities") or {})
    safe_reuse_actions = {
        "weather", "web_search", "export_customer_orders",
        "check_single_order", "export_report", "stock_search"
    }
    if action not in safe_reuse_actions:
        return None

    norm = _normalize_vn_text(user_input)

    if action == "weather":
        # "Ở Đà Nẵng thì sao?" sau một câu hỏi thời tiết.
        m_loc = re.search(r'^(?:o|tai)\s+(.+?)(?:\s+thi\s+sao|\s+the\s+nao|\s+ra\s+sao)?$', norm)
        if m_loc:
            entities["location"] = _extract_weather_location(
                f"thời tiết {str(user_input).strip()}",
                default_location=entities.get("location") or "Hà Nội"
            )
        return {
            "action": "weather",
            "location": entities.get("location") or "Hà Nội",
            "source": "followup"
        }

    if action == "web_search":
        query = entities.get("query") or last_context.get("topic") or str(user_input)
        if any(x in norm for x in ["moi nhat", "cap nhat", "co gi moi", "tra lai", "tra cho ro"]):
            query = f"{query} mới nhất"
        return {"action": "web_search", "query": query, "source": "followup"}

    if action == "export_customer_orders" and entities.get("customer_name"):
        return {"action": action, "customer_name": entities["customer_name"], "source": "followup"}

    if action == "check_single_order" and entities.get("order_code"):
        return {"action": action, "order_code": entities["order_code"], "source": "followup"}

    if action == "export_report" and entities.get("start_date") and entities.get("end_date"):
        return {
            "action": action,
            "start_date": entities["start_date"],
            "end_date": entities["end_date"],
            "source": "followup"
        }

    if action == "stock_search" and entities.get("product_code"):
        return {"action": action, "product_code": entities["product_code"], "source": "followup"}

    return None


def _deterministic_intent(user_input):
    """
    Điều hướng các nghiệp vụ cốt lõi bằng rule trước khi gọi AI.
    Mục đích: Groq lỗi/đổi model vẫn không làm mất các chức năng Odoo cũ.
    """
    original = str(user_input).strip()
    norm = _normalize_vn_text(original)

    # Báo cáo đơn hàng theo khoảng ngày - ưu tiên trước "Đơn hàng <khách>".
    if ('don hang' in norm or 'bao cao' in norm or 'tong hop' in norm) and (' tu ' in f' {norm} ' and (' den ' in f' {norm} ' or ' toi ' in f' {norm} ')):
        date_range = _extract_date_range(original)
        if date_range:
            return {
                "action": "export_report",
                "start_date": date_range[0],
                "end_date": date_range[1],
                "source": "rule"
            }

    # Kiểm tra chi tiết một mã đơn.
    m_order = re.search(
        r'^(?:kiem tra|check|xem)(?:\s+chi tiet)?\s+(?:don hang|don)\s+(.+)$',
        norm
    )
    if m_order:
        # Lấy lại phần mã từ câu gốc để không mất ký tự / - .
        original_match = re.search(
            r'^(?:kiểm\s*tra|kiem\s*tra|check|xem)(?:\s+chi\s*tiết|\s+chi\s*tiet)?\s+'
            r'(?:đơn\s*hàng|don\s*hang|đơn|don)\s+(.+)$',
            original,
            flags=re.IGNORECASE
        )
        code = (original_match.group(1) if original_match else m_order.group(1)).strip()
        return {"action": "check_single_order", "order_code": code, "source": "rule"}

    # Xuất đơn hàng theo khách: "Đơn hàng HC", "đơn hàng của HC".
    m_customer = re.match(r'^don hang(?: cua)?\s+(.+)$', norm)
    if m_customer:
        original_match = re.match(
            r'^đơn\s*hàng(?:\s+của)?\s+(.+)$|^don\s*hang(?:\s+cua)?\s+(.+)$',
            original,
            flags=re.IGNORECASE
        )
        if original_match:
            customer = next((g for g in original_match.groups() if g), '').strip()
        else:
            customer = m_customer.group(1).strip()
        if customer:
            return {"action": "export_customer_orders", "customer_name": customer, "source": "rule"}

    # Mã sản phẩm được nhận dạng cục bộ, không cần tốn một lượt AI.
    if _looks_like_product_code(original):
        return {"action": "stock_search", "source": "rule"}

    # Thời tiết: rule cục bộ, không nhờ AI đoán địa điểm từ các từ như "hôm nay thế nào".
    if 'thoi tiet' in norm:
        loc = _extract_weather_location(original, default_location="Hà Nội")
        return {"action": "weather", "location": loc, "source": "rule"}

    # Một số nhóm tra cứu web rõ ràng.
    if any(k in norm for k in ['tin tuc', 'thoi su', 'gia vang', 'world cup', 'bong da']):
        return {"action": "web_search", "query": original, "source": "rule"}

    return None


def analyze_chat_intent(user_input, last_context=None):
    # Luồng nghiệp vụ rõ ràng chạy bằng rule trước, để không phụ thuộc uptime/model của Groq.
    deterministic = _deterministic_intent(user_input)
    if deterministic:
        return deterministic

    followup = _followup_intent(user_input, last_context or {})
    if followup:
        return followup

    tz_vn = pytz.timezone("Asia/Ho_Chi_Minh")
    current_time_str = datetime.now(tz_vn).strftime("%Y-%m-%d %H:%M:%S")

    system_prompt = f"""
    Bạn là bộ não điều hướng. Thời gian hiện tại: {current_time_str}.
    Bạn xưng "Anh" và gọi người dùng là "con vợ" hoặc "các con vợ".
    Nhiệm vụ của bạn là phân tích câu nói của người dùng và trả về DUY NHẤT một JSON object hợp lệ. KHÔNG giải thích.

    Hành động gần nhất của chính người dùng này (có thể rỗng):
    {json.dumps(last_context or {}, ensure_ascii=False)}
    Nếu câu hiện tại là câu nối tiếp ngắn như "tra lại", "còn cái đó?", "thế sao?" thì có thể dùng ngữ cảnh này.
    Không được dùng ngữ cảnh cũ nếu câu hiện tại đã nêu yêu cầu mới rõ ràng.

    Quy tắc phân loại (QUAN TRỌNG):
    1. Nếu yêu cầu THỐNG KÊ / BÁO CÁO ĐƠN HÀNG từ ngày này đến ngày khác:
    -> {{"action": "export_report", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"}}

    2. Nếu yêu cầu XUẤT ĐƠN HÀNG của MỘT KHÁCH HÀNG cụ thể:
    -> {{"action": "export_customer_orders", "customer_name": "Tên khách hàng cần tìm"}}

    3. Nếu yêu cầu KIỂM TRA CHI TIẾT 1 MÃ ĐƠN HÀNG cụ thể:
    -> {{"action": "check_single_order", "order_code": "Mã đơn hàng"}}

    4. Nếu người dùng hỏi về THỜI TIẾT:
    -> {{"action": "weather", "location": "Tên địa phương"}}

    5. Nếu người dùng hỏi TIN TỨC, thời sự, thể thao, giá vàng, hoặc cần tra cứu kiến thức mạng:
    -> {{"action": "web_search", "query": "Từ khóa tìm kiếm tối ưu (ngắn gọn)"}}

    6. Nếu câu lệnh CHỈ LÀ MÃ SẢN PHẨM (chuỗi ngắn, liền nhau, vd: 'SP01', 'IPHONE12'):
    -> {{"action": "stock_search"}}

    7. Nếu là câu giao tiếp bình thường (chào hỏi, tâm sự, trêu đùa không cần cào mạng):
    -> {{"action": "chat", "response": "Câu trả lời dí dỏm, thông minh của Anh dành cho các con vợ"}}
    """

    try:
        content = call_groq_chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_input}
            ],
            temperature=0.1,
            response_format={"type": "json_object"}
        )
        result = json.loads(content)
        action = result.get('action')
        allowed_actions = {
            'export_report', 'export_customer_orders', 'check_single_order',
            'weather', 'web_search', 'news', 'stock_search', 'chat'
        }
        if action not in allowed_actions:
            raise ValueError(f"AI trả action không hợp lệ: {action}")
        return result
    except Exception as e:
        logger.error(f"Lỗi phân tích ý định AI: {e}")
        return {
            "action": "error",
            "response": (
                "⚠️ Bộ AI hiện không phản hồi nên Anh không tự đoán câu lệnh này. "
                "Các lệnh Odoo cố định và các mẫu như 'Đơn hàng HC', 'Kiểm tra đơn SO001', "
                "mã sản phẩm vẫn hoạt động bình thường."
            ),
            "detail": str(e)
        }

def generate_witty_response(user_input, topic, real_data):
    system_prompt = f"""
    Bạn là một trợ lý AI thông minh, dí dỏm. Bạn xưng "Anh" và gọi người dùng là "con vợ" hoặc "các con vợ".
    Người dùng vừa hỏi về: {topic}.
    Dưới đây là THÔNG TIN THỰC TẾ CHÍNH XÁC được cào từ Internet:
    ---
    {real_data}
    ---
    Nhiệm vụ: Trả lời câu hỏi '{user_input}'.

    LUẬT THÉP:
    1. Tổng hợp thông tin từ dữ liệu được cung cấp một cách khéo léo, tự nhiên như người thật đang đọc báo cho các con vợ nghe. KHÔNG copy paste nguyên xi.
    2. Nếu thông tin cào được bị thiếu hoặc không rõ ràng, hãy trả lời dựa trên những gì tốt nhất có được và thành thật báo các con vợ là tin này chưa đầy đủ.
    3. Nếu là THỜI TIẾT: Phải bắt buộc dùng đúng ĐỘ C (°C). Tùy vào nhiệt độ mà than vãn hoặc trêu đùa.
    4. Giọng văn dí dỏm, chuyên nghiệp nhưng mặn mòi. Có thể trêu đùa các con vợ nhẹ nhàng 1 câu ở cuối.
    5. KHÔNG VIẾT DÀI DÒNG. Câu hỏi dữ liệu đơn giản: 1-3 câu. Chỉ tối đa 4 câu khi thật sự cần.
    6. TUYỆT ĐỐI KHÔNG dùng 2 dấu sao để in đậm. Chỉ dùng 1 dấu sao (*Nội dung*) để in đậm theo chuẩn Telegram.
    """
    try:
        return call_groq_chat(
            messages=[{"role": "system", "content": system_prompt}],
            temperature=0.5
        )
    except Exception as e:
        logger.error(f"Lỗi AI tổng hợp câu trả lời: {e}")
        return f"Thông tin nguyên bản đây con vợ ơi:\n{real_data}"

async def auto_troll_message(context: ContextTypes.DEFAULT_TYPE):
    """Hàm tự động gọi AI để sinh tin nhắn cà khịa/tâm linh ngẫu nhiên"""
    chat_ids = get_registered_chat_ids()
    if not chat_ids:
        return

    # Tỷ lệ 30% kích hoạt mỗi lần chạy để tạo sự ngẫu nhiên thực sự
    if random.random() > 0.3:
        return

    prompt = """
    Bạn là một trợ lý AI quản lý kho Odoo đanh đá, xéo xắt, xưng "Anh" và gọi "con vợ" hoặc "các con vợ".
    Bây giờ, hãy chủ động gửi MỘT tin nhắn ngắn (2-3 câu) vào group chat phòng Sales.
    Ngẫu nhiên chọn 1 trong 2 chủ đề:
    1. Tâm linh: Bói một quẻ vui, phán hướng chốt đơn, hoặc giờ hoàng đạo để gọi khách.
    2. Cà khịa: Trêu chọc các con vợ lười biếng, ế đơn, mải lướt điện thoại, khịa doanh số.
    Bắt buộc: Giọng điệu hài hước, mặn mòi, dùng từ lóng mạng. Không cần chào hỏi, vào thẳng vấn đề luôn.
    TUYỆT ĐỐI KHÔNG dùng 2 dấu sao để in đậm. Chỉ dùng 1 dấu sao (*Nội dung*) để in đậm theo chuẩn Telegram.
    """

    ai_msg = "Nay Anh bị đau họng không chửi được..."
    try:
        ai_msg = call_groq_chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8
        )
    except Exception as e:
        logger.error(f"Lỗi AI auto troll: {e}")

    for cid in chat_ids:
        try:
            await context.bot.send_message(
                chat_id=cid,
                text=f"🤖 *[Góc Vô Tri]*\n_{ai_msg}_",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Lỗi gửi auto troll cho {cid}: {e}")

def keep_port_open():
    try:
        s = socket.socket()
        s.bind(("0.0.0.0", 10000))
        s.listen(1)
        while True:
            conn, _ = s.accept()
            conn.close()
    except Exception:
        pass

threading.Thread(target=keep_port_open, daemon=True).start()

# ---------------- Odoo connect ----------------
def connect_odoo():
    try:
        if not ODOO_URL_FINAL:
            return None, None, "odoo url không được thiết lập."

        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "service": "common",
                "method": "login",
                "args": [ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD]
            },
            "id": 1
        }

        r = requests.post(
            f"{ODOO_URL_FINAL}/jsonrpc",
            json=payload,
            timeout=15
        )

        uid = r.json().get("result")
        if not uid:
            return None, None, "Đăng nhập thất bại. Kiểm tra DB/user/pass."

        class Models:
            def execute_kw(self, db, uid, pwd, model, method, args, kwargs=None):
                payload = {
                    "jsonrpc": "2.0",
                    "method": "call",
                    "params": {
                        "service": "object",
                        "method": "execute_kw",
                        "args": [
                            db, uid, pwd, model, method, args, kwargs or {}
                        ]
                    },
                    "id": 2
                }

                r = requests.post(
                    f"{ODOO_URL_FINAL}/jsonrpc",
                    json=payload,
                    timeout=60
                )
                return r.json().get("result")

        return uid, Models(), "OK"

    except Exception as e:
        return None, None, f"Lỗi kết nối: {e}"

# ---------------- Location helpers ----------------
def find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD):
    out = {}

    def search(key):
        locs = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.location', 'search_read',
            [[('display_name', 'ilike', key)]],
            {'fields': ['id', 'display_name', 'complete_name']}
        )
        if not locs:
            return None

        for l in locs:
            if key.lower() in (l['display_name'] or '').lower():
                return {'id': l['id'], 'name': l['display_name']}
        return {'id': locs[0]['id'], 'name': locs[0]['display_name']}

    hn = search(LOCATION_MAP['HN_STOCK_CODE'])
    if hn:
        out['HN_STOCK'] = hn

    hcm = search(LOCATION_MAP['HCM_STOCK_CODE'])
    if hcm:
        out['HCM_STOCK'] = hcm

    tran = search(LOCATION_MAP['HN_TRANSIT_NAME'])
    if tran:
        out['HN_TRANSIT'] = tran

    return out

# ---------------- Kho Nhập HN – quantity ----------------
def get_transit_quantity(models, uid, product_id, transit_location_id):
    if not transit_location_id:
        return 0

    quant_data = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'stock.quant', 'search_read',
        [[('product_id', '=', product_id),
          ('location_id', '=', transit_location_id)]],
        {'fields': ['quantity']}
    )

    total = 0
    for q in quant_data:
        total += int(q.get('quantity') or 0)
    return total

def escape_markdown(text):
    chars = ['\\','_','*','[',']','(',')','~','`','>','#','+','-','=','|','{','}','.','!']
    text = str(text)
    for c in chars:
        text = text.replace(c, f"\\{c}")
    return text.replace('\\`', '`')

# ---------------- Chat ID Registry ----------------
REGISTERED_CHAT_IDS = set()
CHAT_IDS_LOCK = threading.Lock()

def register_chat_id(chat_id):
    if chat_id is None:
        return
    try:
        cid = int(chat_id)
    except Exception:
        cid = chat_id

    with CHAT_IDS_LOCK:
        REGISTERED_CHAT_IDS.add(cid)

def get_registered_chat_ids():
    with CHAT_IDS_LOCK:
        return list(REGISTERED_CHAT_IDS)

# ---------------- Report /keohang ----------------
def get_stock_data():
    uid, models, error_msg = connect_odoo()
    if not uid:
        return None, 0, error_msg

    try:
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        if len(location_ids) < 3:
            error_msg = f"không tìm thấy đủ 3 kho cần thiết: {list(location_ids.keys())}"
            logger.error(error_msg)
            return None, 0, error_msg

        hn_id   = location_ids.get('HN_STOCK', {}).get('id')
        hcm_id  = location_ids.get('HCM_STOCK', {}).get('id')
        tran_id = location_ids.get('HN_TRANSIT', {}).get('id')

        quant_data_raw = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.quant', 'search_read',
            [[('location_id', 'in', [hn_id, hcm_id, tran_id])]],
            {'fields': ['product_id', 'location_id', 'quantity',
                        'reserved_quantity', 'available_quantity']}
        )

        stock_map = {}

        for q in quant_data_raw:
            pid = q['product_id'][0]
            loc = q['location_id'][0]

            if loc == tran_id:
                real_qty = float(q.get('quantity', 0))
            else:
                if 'available_quantity' in q and q.get('available_quantity') is not None:
                    real_qty = float(q.get('available_quantity', 0))
                else:
                    real_qty = float(q.get('quantity', 0)) - float(q.get('reserved_quantity', 0))

            if real_qty <= 0:
                continue

            if pid not in stock_map:
                stock_map[pid] = {'hn': 0, 'tran': 0, 'hcm': 0}

            if loc == hn_id:
                stock_map[pid]['hn'] += real_qty
            elif loc == tran_id:
                stock_map[pid]['tran'] += real_qty
            elif loc == hcm_id:
                stock_map[pid]['hcm'] += real_qty

        if not stock_map:
            df_empty = pd.DataFrame(columns=[
                'Mã SP', 'Tên SP', 'Tồn Kho HN',
                'Tồn Kho HCM', 'Kho Nhập HN', 'Số Lượng Đề Xuất'
            ])
            buf = io.BytesIO()
            df_empty.to_excel(buf, index=False, sheet_name='DeXuatKeoHang')
            buf.seek(0)
            return buf, 0, "không có SP nào cần kéo"

        pids = list(stock_map.keys())
        product_info = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [[('id', 'in', pids)]],
            {'fields': ['display_name', PRODUCT_CODE_FIELD]}
        )
        product_map = {p['id']: p for p in product_info}

        report = []
        for pid, qtys in stock_map.items():
            prod = product_map.get(pid)
            if not prod:
                continue

            code = prod.get(PRODUCT_CODE_FIELD, '')
            name = prod.get('display_name', '')

            ton_hn   = int(round(qtys['hn']))
            ton_tran = int(round(qtys['tran']))
            ton_hcm  = int(round(qtys['hcm']))

            tong_hn = ton_hn + ton_tran

            if tong_hn < TARGET_MIN_QTY:
                need = TARGET_MIN_QTY - tong_hn
                de_xuat = min(need, ton_hcm)
                if de_xuat > 0:
                    report.append({
                        'Mã SP': code,
                        'Tên SP': name,
                        'Tồn Kho HN': ton_hn,
                        'Tồn Kho HCM': ton_hcm,
                        'Kho Nhập HN': ton_tran,
                        'Số Lượng Đề Xuất': de_xuat
                    })

        df = pd.DataFrame(report)
        cols = [
            'Mã SP', 'Tên SP', 'Tồn Kho HN',
            'Tồn Kho HCM', 'Kho Nhập HN', 'Số Lượng Đề Xuất'
        ]

        if not df.empty:
            df = df[cols]
        else:
            df = pd.DataFrame(columns=cols)

        buf = io.BytesIO()
        df.to_excel(buf, index=False, sheet_name="DeXuatKeoHang")
        buf.seek(0)

        return buf, len(df), "thành công"

    except Exception as e:
        logger.error(f"lỗi khi xử lý kéo hàng: {e}")
        return None, 0, f"lỗi khi xử lý kéo hàng: {e}"

# ---------------- PO /checkpo helpers ----------------
def _read_po_with_auto_header(file_bytes: bytes):
    try:
        df_tmp = pd.read_excel(io.BytesIO(file_bytes), header=None)
    except Exception as e:
        return None, f"Không đọc được file Excel PO: {e}"

    header_row_idx = None
    for idx in range(len(df_tmp)):
        row_values = df_tmp.iloc[idx].astype(str).str.lower()
        row_text = " ".join(row_values)
        if any(key in row_text for key in [
            "model", "mã sp", "ma sp", "mã hàng", "ma hang",
            "mã sản phẩm", "ma san pham"
        ]):
            header_row_idx = idx
            break

    if header_row_idx is None:
        header_row_idx = 0

    try:
        df_raw = pd.read_excel(io.BytesIO(file_bytes), header=header_row_idx)
        return df_raw, None
    except Exception as e:
        return None, f"Không đọc được file Excel PO với header tại dòng {header_row_idx + 1}: {e}"


def _detect_po_columns(df: pd.DataFrame):
    cols_lower = {col: str(col).strip().lower() for col in df.columns}

    code_col = None
    for col, lower in cols_lower.items():
        if lower == "model":
            code_col = col
            break

    if code_col is None:
        for col, lower in cols_lower.items():
            if lower.strip() == "model":
                code_col = col
                break

    def find_col(candidates):
        for col, lower in cols_lower.items():
            for key in candidates:
                if key in lower:
                    return col
        return None

    if code_col is None:
        code_col = find_col([
            'mã sp', 'ma sp', 'mã hàng', 'ma hang',
            'mã sản phẩm', 'ma san pham'
        ])

    qty_col = find_col([
        'sl', 'số lượng', 'so luong', 's.l', 'sl đặt', 'sl dat'
    ])

    recv_col = find_col([
        'đv nhận', 'dv nhận', 'đơn vị nhận', 'don vi nhan',
        'đv nhận hàng', 'dv nhận hang',
        'cửa hàng nhận', 'cua hang nhan'
    ])

    return code_col, qty_col, recv_col


def _get_stock_for_product_with_cache(models, uid, product_id, location_ids, cache):
    if product_id in cache:
        return cache[product_id]

    hn_id      = location_ids.get('HN_STOCK', {}).get('id')
    transit_id = location_ids.get('HN_TRANSIT', {}).get('id')
    hcm_id     = location_ids.get('HCM_STOCK', {}).get('id')

    def _get_qty(location_id):
        if not location_id:
            return 0
        stock_product_info = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'read',
            [[product_id]],
            {'fields': ['qty_available'], 'context': {'location': location_id}}
        )
        if stock_product_info and stock_product_info[0]:
            return int(round(stock_product_info[0].get('qty_available', 0.0)))
        return 0

    result = {
        'hn': _get_qty(hn_id),
        'transit': _get_qty(transit_id),
        'hcm': _get_qty(hcm_id),
    }
    cache[product_id] = result
    return result


def process_po_and_build_report(file_bytes: bytes):
    df_raw, err = _read_po_with_auto_header(file_bytes)
    if df_raw is None:
        return None, err

    if df_raw.empty:
        return None, "File PO không có dữ liệu."

    code_col, qty_col, recv_col = _detect_po_columns(df_raw)
    if not code_col or not qty_col or not recv_col:
        return None, (
            "Không xác định được Model – Số lượng – ĐV nhận.\n"
            f"Các cột hiện có: {list(df_raw.columns)}"
        )

    df = df_raw[[code_col, qty_col, recv_col]].copy()
    df.columns = ['Mã SP', 'SL cần giao', 'ĐV nhận']

    df['Mã SP'] = df['Mã SP'].astype(str).str.strip().str.upper()
    df['SL cần giao'] = pd.to_numeric(df['SL cần giao'], errors='coerce').fillna(0)
    df = df[(df['Mã SP'] != "") & (df['SL cần giao'] > 0)]

    if df.empty:
        return None, "Không có dòng hợp lệ."

    uid, models, error_msg = connect_odoo()
    if not uid:
        return None, error_msg

    try:
        codes = sorted(df['Mã SP'].unique().tolist())
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [[(PRODUCT_CODE_FIELD, 'in', codes)]],
            {'fields': ['id', 'display_name', PRODUCT_CODE_FIELD]}
        )

        code_map = {}
        for p in products:
            c = str(p.get(PRODUCT_CODE_FIELD) or "").strip().upper()
            code_map[c] = p

        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        stock_cache = {}
        rows = []

        for _, r in df.iterrows():
            code = r['Mã SP']
            need_qty = int(round(r['SL cần giao']))
            receiver = r['ĐV nhận']

            prod = code_map.get(code)
            if not prod:
                rows.append({
                    'Mã SP': code,
                    'Tên SP': 'KHÔNG TÌM THẤY',
                    'ĐV nhận': receiver,
                    'SL cần giao': need_qty,
                    'Tồn HN': 0,
                    'Tồn Kho Nhập': 0,
                    'Tổng tồn HN': 0,
                    'Tồn HCM': 0,
                    'Trạng thái': 'KHÔNG TÌM THẤY MÃ',
                    'SL cần kéo từ HCM': 0,
                    'SL thiếu': need_qty,
                })
                continue

            pid = prod['id']
            name = prod['display_name']

            stock = _get_stock_for_product_with_cache(
                models, uid, pid, location_ids, stock_cache
            )

            hn  = stock['hn']
            hcm = stock['hcm']

            tr = get_transit_quantity(
                models, uid, pid,
                location_ids.get('HN_TRANSIT', {}).get('id')
            )

            total_hn = hn + tr
            pull = 0
            shortage = 0

            if need_qty <= hn:
                status = "ĐỦ tại kho HN (201/201)"
            elif need_qty <= total_hn:
                status = "ĐỦ (HN + Kho nhập HN)"
            else:
                req = need_qty - total_hn
                if req <= hcm:
                    pull = req
                    status = "CẦN KÉO HÀNG TỪ HCM"
                else:
                    pull = hcm
                    shortage = req - hcm
                    status = "THIẾU DÙ ĐÃ KÉO TỐI ĐA"

            rows.append({
                'Mã SP': code,
                'Tên SP': name,
                'ĐV nhận': receiver,
                'SL cần giao': need_qty,
                'Tồn HN': hn,
                'Tồn Kho Nhập': tr,
                'Tổng tồn HN': total_hn,
                'Tồn HCM': hcm,
                'Trạng thái': status,
                'SL cần kéo từ HCM': pull,
                'SL thiếu': shortage,
            })

        df_out = pd.DataFrame(rows)
        cols = [
            'Mã SP','Tên SP','ĐV nhận','SL cần giao',
            'Tồn HN','Tồn Kho Nhập','Tổng tồn HN','Tồn HCM',
            'Trạng thái','SL cần kéo từ HCM','SL thiếu'
        ]
        df_out = df_out[cols]

        buf = io.BytesIO()
        df_out.to_excel(buf, index=False, sheet_name='KiemTraPO')
        buf.seek(0)
        return buf, None

    except Exception as e:
        return None, f"Lỗi khi xử lý PO: {e}"

# =====================================================================
# ---> TẠO FILE EXCEL TỒN KHO CHI TIẾT <---
# =====================================================================
async def process_export_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE, loc_id: int, loc_name: str):
    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi kết nối Odoo: {error_msg}")
        return

    try:
        quants = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.quant', 'search_read',
            [[('location_id', '=', loc_id), ('quantity', '>', 0)]],
            {'fields': ['product_id', 'quantity', 'available_quantity', 'reserved_quantity']}
        )

        if not quants:
            await update.message.reply_text(f"📭 Kho *{loc_name}* hiện đang trống, không có sản phẩm nào tồn kho.", parse_mode='Markdown')
            return

        stock_map = {}
        for q in quants:
            pid = q['product_id'][0]
            if pid not in stock_map:
                stock_map[pid] = {'qty': 0, 'available': 0, 'reserved': 0}
            stock_map[pid]['qty'] += float(q.get('quantity', 0))
            stock_map[pid]['available'] += float(q.get('available_quantity', 0))
            stock_map[pid]['reserved'] += float(q.get('reserved_quantity', 0))

        pids = list(stock_map.keys())
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [[('id', 'in', pids)]],
            {'fields': ['id', 'display_name', PRODUCT_CODE_FIELD]}
        )
        product_map = {p['id']: p for p in products}

        rows = []
        for pid, qtys in stock_map.items():
            prod = product_map.get(pid, {})
            rows.append({
                'Mã SP': prod.get(PRODUCT_CODE_FIELD, 'N/A'),
                'Tên SP': prod.get('display_name', 'Không xác định'),
                'Tồn thực tế (Quantity)': qtys['qty'],
                'Có sẵn (Available)': qtys['available'],
                'Đã giữ (Reserved)': qtys['reserved']
            })

        df = pd.DataFrame(rows)
        df = df.sort_values(by='Mã SP')

        buf = io.BytesIO()
        df.to_excel(buf, index=False, sheet_name='Ton_Kho')
        buf.seek(0)

        safe_loc_name = "".join(c for c in loc_name if c.isalnum() or c in (' ', '_')).replace(' ', '_')
        today_str = datetime.now().strftime('%d%m%Y')
        filename = f"Ton_Kho_{safe_loc_name}_{today_str}.xlsx"

        await update.message.reply_document(
            document=buf,
            filename=filename,
            caption=f"📊 Anh gửi file thống kê tồn kho của *{loc_name}* nhé!\nTổng cộng có {len(df)} mã sản phẩm đang có hàng.",
            parse_mode='Markdown'
        )

    except Exception as e:
        logger.error(f"Lỗi khi đổ tồn kho: {e}")
        await update.message.reply_text(f"❌ Lỗi khi xuất dữ liệu tồn kho: {e}")


# =====================================================================
# ---> HÀM TÌM VÀ QUÉT KHO THEO TỪ KHÓA <---
# =====================================================================
async def dotonkho_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    register_chat_id(chat_id)

    keyword = " ".join(context.args).strip()

    if not keyword:
        msg = (
            "💡 Danh sách kho trên Odoo thường rất dài. Để tìm và xuất dữ liệu nhanh nhất, "
            "Các con vợ vui lòng gõ lệnh kèm theo *từ khóa* tên kho nhé!\n\n"
            "👉 *Ví dụ:* `/dotonkho 201` hoặc `/dotonkho hcm`"
        )
        await update.message.reply_text(msg, parse_mode='Markdown')
        return

    await update.message.reply_text(f"🔍 Đang tìm các kho chứa từ khóa *{keyword}*...", parse_mode='Markdown')
    
    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi kết nối Odoo: {error_msg}")
        return

    try:
        domain = [('usage', '=', 'internal'), ('display_name', 'ilike', keyword)]
        locations = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.location', 'search_read',
            [domain],
            {'fields': ['id', 'display_name']}
        )

        if not locations:
            await update.message.reply_text(f"📭 Không tìm thấy kho nào có tên chứa từ khóa *{keyword}*.", parse_mode='Markdown')
            return

        if len(locations) == 1:
            loc = locations[0]
            await update.message.reply_text(f"✅ Tìm thấy đúng 1 kho: *{loc['display_name']}*\n⌛️ Anh đang gom số liệu tồn...", parse_mode='Markdown')
            await process_export_inventory(update, context, loc['id'], loc['display_name'])
            return

        loc_dict = {str(loc['id']): loc for loc in locations}
        context.user_data['waiting_for_location'] = True
        context.user_data['available_locations'] = loc_dict

        msg = f"📦 *TÌM THẤY {len(locations)} KHO PHÙ HỢP:*\n\n"
        for loc in locations:
            msg += f"🔹 Gõ `{loc['id']}` - Kho: {loc['display_name']}\n"

        msg += "\n👉 *Vui lòng gõ ID kho muốn xem (Gõ 'hủy' để thoát).* "

        await update.message.reply_text(msg, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Lỗi khi lấy danh sách kho: {e}")
        await update.message.reply_text(f"❌ Lỗi quét danh sách kho: {e}")

# =====================================================================
# ---> HÀM XUẤT ĐƠN HÀNG THEO TỪ NGÀY TỚI NGÀY <---
# =====================================================================
async def export_orders_by_date_range(update: Update, context: ContextTypes.DEFAULT_TYPE, start_date: str, end_date: str):
    await update.message.reply_text(f"🔍 Đang tổng hợp các đơn hàng từ `{start_date}` đến `{end_date}`...", parse_mode='Markdown')
    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi kết nối Odoo: {error_msg}")
        return

    try:
        domain = [
            ('date_order', '>=', f"{start_date} 00:00:00"),
            ('date_order', '<=', f"{end_date} 23:59:59")
        ]
        
        orders = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'search_read',
            [domain],
            {'fields': ['name', 'partner_id', 'state', 'date_order', 'amount_total'], 'order': 'date_order asc'}
        )

        if not orders:
            await update.message.reply_text(f"📭 Anh không tìm thấy đơn hàng nào trong khoảng từ {start_date} đến {end_date} nhé.")
            return

        rows = []
        state_map = {'draft': 'Nháp', 'sent': 'Đã gửi báo giá', 'sale': 'Đã chốt', 'done': 'Hoàn thành', 'cancel': 'Đã hủy'}
        
        for o in orders:
            rows.append({
                'Mã Đơn Hàng': o['name'],
                'Khách Hàng': o['partner_id'][1] if o.get('partner_id') else 'N/A',
                'Ngày Lên Đơn': o['date_order'],
                'Trạng Thái': state_map.get(o['state'], o['state']),
                'Tổng Tiền': o.get('amount_total', 0)
            })

        df = pd.DataFrame(rows)
        buf = io.BytesIO()
        df.to_excel(buf, index=False, sheet_name='Thống Kê Đơn Hàng')
        buf.seek(0)

        await update.message.reply_document(
            document=buf,
            filename=f"Thong_Ke_Don_Hang_{start_date}_den_{end_date}.xlsx",
            caption=f"📊 Anh đã tổng hợp xong! Tổng cộng có {len(orders)} đơn hàng trong khoảng thời gian này nhé."
        )

    except Exception as e:
        logger.error(f"Lỗi xuất đơn hàng theo ngày: {e}")
        await update.message.reply_text(f"❌ Lỗi xuất Excel: {e}")

# =====================================================================
# ---> [NEW FEATURE] KIỂM TRA ĐƠN HÀNG <---
# =====================================================================
async def check_single_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_code: str):
    await update.message.reply_text(f"🔍 Đang truy xuất thông tin đơn hàng *{order_code}*...", parse_mode='Markdown')
    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi kết nối Odoo: {error_msg}")
        return

    try:
        orders = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'search_read',
            [[('name', 'ilike', order_code)]],
            {'fields': ['name', 'partner_id', 'state', 'date_order', 'amount_total', 'order_line']}
        )

        if not orders:
            await update.message.reply_text(f"📭 Anh không tìm thấy đơn hàng nào khớp với mã *{order_code}* trên hệ thống.", parse_mode='Markdown')
            return

        o = orders[0]
        state_map = {
            'draft': 'Nháp / Báo giá',
            'sent': 'Đã gửi báo giá',
            'sale': 'Đã chốt (Sale Order)',
            'done': 'Đã khóa / Hoàn thành',
            'cancel': 'Đã hủy'
        }
        state_vn = state_map.get(o['state'], o['state'])
        
        lines = []
        if o.get('order_line'):
            lines = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'sale.order.line', 'read',
                [o['order_line']],
                {'fields': ['product_id', 'product_uom_qty', 'qty_delivered', 'price_subtotal']}
            )

        msg = f"🧾 *THÔNG TIN ĐƠN HÀNG: {o['name']}*\n"
        msg += f"👤 Khách hàng: {o['partner_id'][1] if o.get('partner_id') else 'Không xác định'}\n"
        msg += f"📅 Ngày lập: {o['date_order']}\n"
        msg += f"✅ Trạng thái: {state_vn}\n"
        msg += f"💰 Tổng tiền: {o['amount_total']:,.0f} VNĐ\n\n"
        msg += "📦 *CHI TIẾT SẢN PHẨM:*\n"
        
        if not lines:
            msg += "Đơn hàng chưa có sản phẩm nào."
        else:
            for i, l in enumerate(lines, 1):
                pname = l['product_id'][1] if l.get('product_id') else 'Không rõ'
                qty = l.get('product_uom_qty', 0)
                deliv = l.get('qty_delivered', 0)
                msg += f"{i}. {pname}\n   ▫️ SL đặt: {qty} | Đã giao: {deliv}\n"

        await update.message.reply_text(msg, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Lỗi kiểm tra đơn hàng: {e}")
        await update.message.reply_text(f"❌ Lỗi truy xuất đơn hàng: {e}")


async def export_customer_orders(update: Update, context: ContextTypes.DEFAULT_TYPE, customer_name: str):
    await update.message.reply_text(f"🔍 Anh đang tìm kiếm tối đa 20 đơn hàng gần nhất của khách hàng *{customer_name}*...", parse_mode='Markdown')
    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi kết nối Odoo: {error_msg}")
        return

    try:
        partners = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'res.partner', 'search_read',
            [[('name', 'ilike', customer_name)]],
            {'fields': ['id', 'name']}
        )
        
        if not partners:
            await update.message.reply_text(f"📭 Anh không tìm thấy khách hàng nào tên là *{customer_name}* trên hệ thống.", parse_mode='Markdown')
            return
            
        p_ids = [p['id'] for p in partners]

        orders = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'search_read',
            [[('partner_id', 'in', p_ids)]],
            {'fields': ['name', 'partner_id', 'state', 'date_order', 'amount_total', 'order_line'], 'limit': 20, 'order': 'date_order desc'}
        )

        if not orders:
            await update.message.reply_text(f"📭 Khách hàng *{customer_name}* chưa có đơn đặt hàng nào.", parse_mode='Markdown')
            return

        line_ids = []
        for o in orders:
            line_ids.extend(o.get('order_line', []))

        lines_dict = {}
        if line_ids:
            lines_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'sale.order.line', 'read',
                [line_ids],
                {'fields': ['order_id', 'product_id', 'product_uom_qty', 'qty_delivered', 'price_unit', 'price_subtotal']}
            )
            for l in lines_info:
                oid = l['order_id'][0] if l.get('order_id') else 0
                if oid not in lines_dict: 
                    lines_dict[oid] = []
                lines_dict[oid].append(l)

        state_map = {'draft': 'Nháp', 'sent': 'Đã gửi BG', 'sale': 'Đã chốt', 'done': 'Hoàn thành', 'cancel': 'Đã hủy'}

        rows = []
        for o in orders:
            oid = o['id']
            oname = o['name']
            cname = o['partner_id'][1] if o.get('partner_id') else ''
            date_str = o['date_order']
            st = state_map.get(o['state'], o['state'])
            total_amount = o.get('amount_total', 0)

            o_lines = lines_dict.get(oid, [])
            if not o_lines:
                rows.append({
                    'Mã Đơn': oname, 'Khách Hàng': cname, 'Ngày Đặt': date_str, 'Trạng Thái': st,
                    'Sản Phẩm': 'Không có SP', 'SL Đặt': 0, 'SL Đã Giao': 0, 'Đơn Giá': 0, 'Thành Tiền': 0, 'Tổng Đơn': total_amount
                })
            else:
                for l in o_lines:
                    pname = l['product_id'][1] if l.get('product_id') else ''
                    rows.append({
                        'Mã Đơn': oname, 'Khách Hàng': cname, 'Ngày Đặt': date_str, 'Trạng Thái': st,
                        'Sản Phẩm': pname, 
                        'SL Đặt': l.get('product_uom_qty', 0), 
                        'SL Đã Giao': l.get('qty_delivered', 0), 
                        'Đơn Giá': l.get('price_unit', 0), 
                        'Thành Tiền': l.get('price_subtotal', 0),
                        'Tổng Đơn': total_amount
                    })

        df = pd.DataFrame(rows)
        buf = io.BytesIO()
        df.to_excel(buf, index=False, sheet_name='Lich_Su_Don_Hang')
        buf.seek(0)

        safe_name = "".join(c for c in customer_name if c.isalnum() or c in (' ', '_')).replace(' ', '_')
        await update.message.reply_document(
            document=buf,
            filename=f"Don_Hang_{safe_name}.xlsx",
            caption=f"📊 Anh đã tổng hợp xong {len(orders)} đơn hàng gần nhất của khách *{customer_name}* rồi nhé!",
            parse_mode='Markdown'
        )

    except Exception as e:
        logger.error(f"Lỗi xuất đơn hàng khách hàng: {e}")
        await update.message.reply_text(f"❌ Lỗi khi xuất Excel: {e}")

# =====================================================================
# ---> LỆNH BÁO DANH ĐỊNH DANH NHÂN VIÊN <---
# =====================================================================
async def baodanh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    register_chat_id(chat_id)
    
    args = context.args
    if not args:
        await update.message.reply_text(
            "💡 *Hướng dẫn Báo Danh:*\nCác con vợ vui lòng gõ lệnh kèm theo *Email đăng nhập Odoo* của mình nhé.\n"
            "👉 *Ví dụ:* `/baodanh kinhdoanh09@nguonsongviet.vn`", 
            parse_mode='Markdown'
        )
        return
        
    email = args[0].strip()
    await update.message.reply_text(f"🔍 Đang tra cứu tài khoản nhân viên `{email}` trên Odoo...", parse_mode='Markdown')
    
    uid, models, err = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi kết nối Odoo: {err}")
        return
        
    try:
        users = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.users', 'search_read',
                                  [[('login', '=', email)]], {'fields': ['id', 'name'], 'limit': 1})
        if not users:
            await update.message.reply_text(f"❌ Không tìm thấy nhân viên nào sử dụng Email đăng nhập là `{email}` trên hệ thống Odoo.", parse_mode='Markdown')
            return
            
        odoo_user = users[0]
        
        # Ghi vào Mây RAM
        cloud_data['sales_mapping'][chat_id] = {
            'odoo_user_id': odoo_user['id'],
            'name': odoo_user['name'],
            'email': email
        }

        # Bổ sung danh tính Odoo vào AI memory theo Telegram user_id.
        # sales_mapping và nghiệp vụ /baodanh vẫn giữ nguyên như cũ.
        _sync_ai_memory_profile(update, odoo_profile=cloud_data['sales_mapping'][chat_id])
        
        await update.message.reply_text(
            f"✅ *BÁO DANH THÀNH CÔNG!*\n\n"
            f"Hệ thống đã kết nối tài khoản Telegram này với hồ sơ Chuyên viên Sales: *{odoo_user['name']}* (Odoo ID: {odoo_user['id']}).\n"
            f"Từ giờ các con vợ có thể dùng lệnh `/lendon` hoặc `/chuyenkho` được rồi nhé!", 
            parse_mode='Markdown'
        )
        
        # Đồng bộ lưu trữ Mây
        await save_cloud_db(context, update.message.chat_id)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi tra cứu: {e}")


# =====================================================================
# ---> LOGIC TÍNH NĂNG FORM CHUYỂN KHO NỘI BỘ (/chuyenkho) <---
# =====================================================================
async def start_chuyenkho_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    register_chat_id(chat_id)
    
    # Check nếu chưa báo danh
    if chat_id not in cloud_data.get('sales_mapping', {}):
        await update.message.reply_text(
            "❌ *Con vợ chưa Báo danh Chuyên viên Sales!*\n\n"
            "Vui lòng gõ lệnh `/baodanh <email_odoo_của_bạn>` để hệ thống nhận diện danh tính trước khi làm phiếu nhé.\n"
            "*(Ví dụ: /baodanh kinhdoanh09@nguonsongviet.vn)*",
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    context.user_data['odoo_salesperson'] = cloud_data['sales_mapping'][chat_id]
    context.user_data['chuyenkho_data'] = {}
    
    await update.message.reply_text(
        "🚚 *TẠO PHIẾU CHUYỂN KHO NỘI BỘ*\n"
        "Con vợ copy paste danh sách mã hàng kèm số lượng cần chuyển nhé:\n"
        "*(Ví dụ:\nI-28: 3\nAC-350: 5)*\n"
        "*(Hoặc gõ /cancel để hủy bỏ Form)*",
        parse_mode='Markdown'
    )
    return CK_PRODUCTS

async def ck_products_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products_raw = update.message.text.strip()
    loading_msg = await update.message.reply_text("⌛️ Anh đang bóc tách hàng hóa, chờ tí...")
    
    parsed_items = parse_order_products_ai(products_raw)
    if not parsed_items:
        await loading_msg.edit_text("❌ Anh không nhận diện được sản phẩm nào hợp lệ. Các con vợ gõ lại hoặc /cancel nhé.")
        return CK_PRODUCTS

    context.user_data['chuyenkho_form'] = {
        'src_id': None,
        'src_name': "❌ CHƯA CHỌN",
        'dest_id': None,
        'dest_name': "❌ CHƯA CHỌN",
        'products': parsed_items,
        'odoo_salesperson': context.user_data['odoo_salesperson']
    }
    
    await loading_msg.delete()
    await send_chuyenkho_inline_form(update, context)
    return ConversationHandler.END

async def send_chuyenkho_inline_form(update: Update, context: ContextTypes.DEFAULT_TYPE, query=None):
    form = context.user_data['chuyenkho_form']
    
    prod_txt = ""
    for idx, p in enumerate(form['products'], 1):
        prod_txt += f"   {idx}. {p['code']} | SL: *{p['qty']}*\n"
        
    text_form = (
        f"🚚 *FORM ĐIỀU KHIỂN CHUYỂN KHO NỘI BỘ*\n\n"
        f"👤 *Người lập:* {form['odoo_salesperson']['name']}\n"
        f"📤 *Kho đi (Xuất):* {form['src_name']}\n"
        f"📥 *Kho đến (Nhập):* {form['dest_name']}\n\n"
        f"📦 *Chi tiết hàng hóa:*\n{prod_txt}\n"
    )
    
    keyboard = [
        [InlineKeyboardButton("🔍 Tìm & Chọn Kho ĐI...", callback_data="ck_search_src")],
        [InlineKeyboardButton("🔍 Tìm & Chọn Kho ĐẾN...", callback_data="ck_search_dest")]
    ]
    
    if form['src_id'] and form['dest_id']:
        keyboard.append([InlineKeyboardButton("✅ XÁC NHẬN TẠO PHIẾU CHUYỂN", callback_data="ck_submit")])
    keyboard.append([InlineKeyboardButton("❌ HỦY BỎ", callback_data="ck_cancel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if query:
        await query.edit_message_text(text_form, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(text_form, reply_markup=reply_markup, parse_mode='Markdown')

async def ck_warehouse_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    keyword = update.message.text.strip()
    if mode == 'src':
        context.user_data['waiting_ck_src_kw'] = False
    else:
        context.user_data['waiting_ck_dest_kw'] = False
    
    uid, models, err = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi kết nối Odoo: {err}")
        return
        
    try:
        locs = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read',
                                 [[('usage', '=', 'internal'), ('display_name', 'ilike', keyword)]],
                                 {'fields': ['id', 'display_name'], 'limit': 5})
        if not locs:
            await update.message.reply_text(f"📭 Anh không tìm thấy kho nào chứa chữ *{keyword}*, các con vợ bấm lại nút chọn kho nhé.", parse_mode='Markdown')
            return
            
        keyboard = []
        for l in locs:
            keyboard.append([InlineKeyboardButton(f"🏭 {l['display_name']}", callback_data=f"selectck_{mode}_{l['id']}_{l['display_name'][:20]}")])
        keyboard.append([InlineKeyboardButton("❌ Hủy tìm kiếm", callback_data="back_to_form_ck")])
        
        await update.message.reply_text("✅ Các kho phù hợp đây, con vợ chọn để nạp vào Form Chuyển Kho nhé:", 
                                        reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi tìm kho động: {e}")

def execute_create_transfer_odoo(form):
    uid, models, err = connect_odoo()
    if not uid:
        return False, f"❌ *Lỗi Hệ thống Odoo:* {err}"
        
    try:
        # Lấy picking type nội bộ (internal)
        p_types = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.picking.type', 'search_read', 
                                    [[('code', '=', 'internal')]], {'fields': ['id'], 'limit': 1})
        type_id = p_types[0]['id'] if p_types else 1

        pick_vals = {
            'location_id': int(form['src_id']),
            'location_dest_id': int(form['dest_id']),
            'picking_type_id': type_id,
            'origin': f"Bot Telegram ({form['odoo_salesperson']['name']})"
        }
        pick_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.picking', 'create', [pick_vals])

        for p in form['products']:
            prods = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read', 
                                      [[('default_code', '=', p['code'])]], {'fields': ['id', 'uom_id'], 'limit': 1})
            if not prods:
                return False, f"❌ Thất bại: Không tìm thấy Mã sản phẩm `{p['code']}` trên Odoo."
            
            move_vals = {
                'name': f"Chuyển {p['code']}",
                'picking_id': pick_id,
                'product_id': prods[0]['id'],
                'product_uom_qty': float(p['qty']),
                'product_uom': prods[0]['uom_id'][0] if prods[0].get('uom_id') else 1,
                'location_id': int(form['src_id']),
                'location_dest_id': int(form['dest_id']),
            }
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.move', 'create', [move_vals])
        
        created_pick = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.picking', 'read', 
                                          [[pick_id]], {'fields': ['name']})
        
        success_msg = (
            f"🎉 *ĐÃ TẠO PHIẾU CHUYỂN KHO THÀNH CÔNG!*\n\n"
            f"🔖 *Mã phiếu:* `{created_pick[0]['name']}`\n"
            f"📤 *Từ kho:* {form['src_name']}\n"
            f"📥 *Đến kho:* {form['dest_name']}\n"
            f"👤 *Người lập:* {form['odoo_salesperson']['name']}\n\n"
            f"👉 Phiếu đang ở trạng thái _Sẵn sàng / Nháp_, các con vợ vào Odoo xác nhận nhé!"
        )
        return True, success_msg

    except Exception as e:
        logger.error(f"Lỗi khởi tạo phiếu chuyển: {e}")
        return False, f"❌ *Lỗi Hệ thống Odoo:* {str(e)}"

async def cancel_chuyenkho_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Đã hủy biểu mẫu tạo phiếu chuyển kho.")
    return ConversationHandler.END


# =====================================================================
# ---> XỬ LÝ TEXT CHÍNH: CỔNG ĐIỀU HƯỚNG AI & TÌM KẾM <---
# =====================================================================
async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    register_chat_id(chat_id)

    user_input = update.message.text.strip()
    user_input_lower = user_input.lower()

    # Ghi lịch sử theo Telegram user_id. Đây chỉ là lớp memory, không tham gia
    # quyết định nghiệp vụ nên không làm thay đổi command/flow Odoo hiện có.
    memory_key = _remember_user_text(update, user_input)
    last_context = _get_last_action_context(memory_key)

    # --- 1. Lọc Lệnh Chọn ID Kho cho Đổ Tồn Kho ---
    if context.user_data.get('waiting_for_location'):
        loc_dict = context.user_data.get('available_locations', {})
        if user_input in loc_dict:
            context.user_data['waiting_for_location'] = False
            selected_loc = loc_dict[user_input]
            await update.message.reply_text(f"⌛️ Anh đang gom số liệu tồn cho kho *{selected_loc['display_name']}*...", parse_mode='Markdown')
            await process_export_inventory(update, context, selected_loc['id'], selected_loc['display_name'])
            return
        elif user_input_lower in ['huy', 'hủy', 'cancel']:
            context.user_data['waiting_for_location'] = False
            await update.message.reply_text("✅ Đã hủy lệnh đổ tồn kho nha!")
            return
        else:
            await update.message.reply_text("❌ Mã kho không hợp lệ. Nhập đúng ID kho trong danh sách hoặc gõ 'hủy' để thoát.")
            return

    # --- 2. Báo Giá (Luồng tĩnh ưu tiên - giữ nguyên cách gọi cũ) ---
    if (
        any(k in user_input_lower for k in ['giá', 'bao nhiêu', 'vat', 'bảng giá', 'price'])
        and _contains_product_code_in_text(user_input)
    ):
        await update.message.reply_text("⌛️ Anh đang tra bảng giá xíu...")
        answer = ask_groq_ai(user_input)
        await update.message.reply_text(answer, parse_mode='Markdown')
        return

    # --- 3. PHÂN TÍCH Ý ĐỊNH: rule nghiệp vụ trước, Groq cho câu linh hoạt sau ---
    ai_intent = await asyncio.to_thread(analyze_chat_intent, user_input, last_context)
    action = ai_intent.get("action")

    if action == "export_customer_orders":
        customer_name = ai_intent.get("customer_name", "").strip()
        if customer_name:
            _set_last_action_context(memory_key, "export_customer_orders", f"Đơn hàng khách {customer_name}", {"customer_name": customer_name})
            await export_customer_orders(update, context, customer_name)
        else:
            await update.message.reply_text("Con vợ muốn tra đơn của khách nào? Gõ tên khách cho Anh với nhé!")
        return

    elif action == "check_single_order":
        order_code = ai_intent.get("order_code", "").strip().upper()
        if order_code:
            _set_last_action_context(memory_key, "check_single_order", f"Đơn hàng {order_code}", {"order_code": order_code})
            await check_single_order(update, context, order_code)
        else:
            await update.message.reply_text("Con vợ ném mã đơn (VD: SO001) đây để Anh check cho nóng!")
        return

    elif action == "export_report":
        start_d = ai_intent.get("start_date")
        end_d = ai_intent.get("end_date")
        if start_d and end_d:
            _set_last_action_context(memory_key, "export_report", f"Báo cáo đơn {start_d} đến {end_d}", {"start_date": start_d, "end_date": end_d})
            await export_orders_by_date_range(update, context, start_d, end_d)
        else:
            await update.message.reply_text("❌ Anh chưa hiểu đủ khoảng ngày. Ví dụ: `Tổng hợp đơn hàng từ ngày 2 đến ngày 20`", parse_mode='Markdown')
        return

    elif action == "weather":
        loc = (ai_intent.get("location") or "Hà Nội").strip()
        _set_last_action_context(memory_key, "weather", f"Thời tiết {loc}", {"location": loc})
        await update.message.reply_text(f"🌤 Đang lấy thời tiết {loc}...")
        weather_data = await asyncio.to_thread(get_realtime_weather, loc)
        final_answer = format_weather_response(weather_data)
        await reply_text_safe(update.message, final_answer)
        return

    elif action == "news" or action == "web_search":
        search_query = ai_intent.get("query", user_input)
        _set_last_action_context(memory_key, "web_search", str(search_query), {"query": str(search_query)})
        await update.message.reply_text(f"📰 Đang lướt mạng tra cứu '{search_query}' cho các con vợ...")
        news_data = await asyncio.to_thread(perform_web_search, search_query)
        final_answer = await asyncio.to_thread(generate_witty_response, user_input, "Thông tin mạng hiện tại", news_data)
        await reply_text_safe(update.message, final_answer)
        return

    elif action == "chat":
        # Tách hẳn lớp chat khỏi router: router chỉ xác định đây là hội thoại,
        # còn câu trả lời dùng memory riêng của Telegram user_id hiện tại.
        answer = await generate_personal_chat_response(
            update, context, user_input,
            fallback_response=ai_intent.get("response", "Lỗi rồi con vợ ơi!")
        )
        await reply_text_safe(update.message, answer)

        # Lưu memory sau khi đã trả lời để người dùng không phải chờ JSONBin mới thấy phản hồi.
        await save_cloud_db(context, update.message.chat_id)
        return

    elif action == "error":
        # QUAN TRỌNG: AI lỗi không được phép rơi xuống nhánh tra tồn như bản cũ.
        await update.message.reply_text(ai_intent.get("response", "⚠️ AI hiện không phản hồi, thử lại sau nhé."))
        return

    elif action != "stock_search":
        # Chặn mọi action lạ để không biến câu tự nhiên thành mã sản phẩm.
        await update.message.reply_text("❌ Anh chưa xác định được yêu cầu này. Gõ /help để xem các mẫu lệnh đang hỗ trợ nhé.")
        return

    # --- 4. LOGIC ODOO: Tra tồn kho sản phẩm (GIỮ NGUYÊN THUẬT TOÁN CŨ) ---
    product_code = str(ai_intent.get("product_code") or user_input).strip().upper()
    _set_last_action_context(memory_key, "stock_search", f"Tồn kho {product_code}", {"product_code": product_code})
    await update.message.reply_text(f"Đang tra tồn cho `{product_code}`, vui lòng chờ!", parse_mode='Markdown')

    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ lỗi kết nối odoo. chi tiết: `{escape_markdown(error_msg)}`", parse_mode='Markdown')
        return

    try:
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)

        hn_stock_id   = location_ids.get('HN_STOCK', {}).get('id')
        hn_transit_id = location_ids.get('HN_TRANSIT', {}).get('id')
        hcm_stock_id  = location_ids.get('HCM_STOCK', {}).get('id')

        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [[(PRODUCT_CODE_FIELD, '=', product_code)]],
            {'fields': ['display_name', 'id']}
        )

        if not products:
            await update.message.reply_text(f"❌ Anh không tìm thấy sản phẩm nào có mã `{product_code}`")
            return

        product = products[0]
        product_id = product['id']
        product_name = product['display_name']

        def get_qty_available(location_id):
            if not location_id:
                return 0
            stock_product_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'product.product', 'read',
                [[product_id]],
                {'fields': ['qty_available'], 'context': {'location': location_id}}
            )
            if stock_product_info and stock_product_info[0]:
                return int(round(stock_product_info[0].get('qty_available', 0.0)))
            return 0

        hn_stock_qty  = get_qty_available(hn_stock_id)
        hcm_stock_qty = get_qty_available(hcm_stock_id)
        hn_transit_qty = get_transit_quantity(models, uid, product_id, hn_transit_id)

        quant_domain = [('product_id', '=', product_id), ('available_quantity', '>', 0)]
        quant_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.quant', 'search_read',
            [quant_domain],
            {'fields': ['location_id', 'available_quantity']}
        )

        location_ids_list = list({q['location_id'][0] for q in quant_data if q.get('location_id')})
        if location_ids_list:
            location_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'stock.location', 'read',
                [location_ids_list],
                {'fields': ['id', 'display_name', 'complete_name', 'usage']}
            )
        else:
            location_info = []

        loc_map = {l['id']: l for l in location_info}
        stock_details = {}

        for q in quant_data:
            loc_field = q.get('location_id')
            if not loc_field:
                continue

            loc_id = loc_field[0]
            qty = float(q.get('available_quantity', 0.0))
            if qty <= 0:
                continue

            name_loc = (
                loc_map.get(loc_id, {}).get('complete_name')
                or loc_map.get(loc_id, {}).get('display_name')
                or f"ID:{loc_id}"
            )

            stock_details[name_loc] = stock_details.get(name_loc, 0) + int(qty)

        total_hn = hn_stock_qty + hn_transit_qty

        recommend = 0
        if total_hn < TARGET_MIN_QTY:
            need = TARGET_MIN_QTY - total_hn
            recommend = min(need, hcm_stock_qty)

        priority_items = []
        other_items = []
        used_names = set()

        for code in PRIORITY_LOCATIONS:
            for name, qty in stock_details.items():
                if code.lower() in name.lower() and name not in used_names:
                    priority_items.append((name, qty))
                    used_names.add(name)
                    break

        for name, qty in sorted(stock_details.items()):
            if name not in used_names:
                other_items.append((name, qty))
                used_names.add(name)

        final_list = priority_items + other_items

        msg = (
            f"📦 <b>{_tg_html(product_code)} — {_tg_html(product_name)}</b>\n\n"
            "<b>TỒN CHÍNH</b>\n"
            f"• Hà Nội: <b>{int(hn_stock_qty)}</b>\n"
            f"• HCM: <b>{int(hcm_stock_qty)}</b>\n"
            f"• Kho nhập Hà Nội: <b>{int(hn_transit_qty)}</b>\n\n"
            f"🚚 <b>Đề xuất kéo:</b> {int(recommend)} sp để HN đạt mức {TARGET_MIN_QTY} sp.\n\n"
            "🏭 <b>TỒN CHI TIẾT — CÓ HÀNG</b>"
        )

        if final_list:
            for loc_name, qty in final_list:
                msg += f"\n• {_tg_html(loc_name)}: <b>{qty}</b>"
        else:
            msg += "\n• Không có tồn kho chi tiết lớn hơn 0."

        await update.message.reply_text(msg.strip(), parse_mode="HTML")

    except Exception as e:
        logger.error(f"lỗi khi tra tồn: {e}")
        await update.message.reply_text(f"❌ lỗi khi tra tồn: {e}")

def get_daily_movement_report():
    uid, models, error_msg = connect_odoo()
    if not uid:
        return None, error_msg

    try:
        tz_vn = pytz.timezone("Asia/Ho_Chi_Minh")
        now_vn = datetime.now(tz_vn)
        start_date_vn = now_vn.replace(hour=0, minute=0, second=0, microsecond=0)
        
        start_date_utc = start_date_vn.astimezone(pytz.utc).strftime('%Y-%m-%d %H:%M:%S')
        end_date_utc = now_vn.astimezone(pytz.utc).strftime('%Y-%m-%d %H:%M:%S')

        domain = [
            ('state', '=', 'done'),
            ('date', '>=', start_date_utc),
            ('date', '<=', end_date_utc)
        ]
        
        moves = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.move', 'search_read',
            [domain],
            {'fields': [
                'product_id', 'product_uom_qty', 'date', 
                'location_id', 'location_dest_id', 'picking_id', 'write_uid'
            ]}
        )

        if not moves:
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine='openpyxl') as writer:
                pd.DataFrame(columns=['Mã SP', 'Tên SP', 'Số lượng', 'Nhập từ đâu', 'Thời gian', 'Người thao tác', 'Mã lệnh']).to_excel(writer, index=False, sheet_name='NHẬP KHO')
                pd.DataFrame(columns=['Mã SP', 'Tên SP', 'Số lượng', 'Xuất đi đâu', 'Thời gian', 'Người thao tác', 'Mã lệnh']).to_excel(writer, index=False, sheet_name='XUẤT KHO')
            buf.seek(0)
            return buf, "Không có giao dịch Nhập/Xuất nào trong ngày hôm nay."

        product_ids = list(set([m['product_id'][0] for m in moves if m.get('product_id')]))
        product_map = {}
        if product_ids:
            products_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'product.product', 'search_read',
                [[('id', 'in', product_ids)]],
                {'fields': ['display_name', PRODUCT_CODE_FIELD]}
            )
            product_map = {p['id']: p for p in products_info}

        import_rows = []
        export_rows = []
        hn_stock_name = LOCATION_MAP.get('HN_STOCK_CODE', "201/201") 

        for m in moves:
            pid = m['product_id'][0] if m.get('product_id') else None
            prod = product_map.get(pid, {})
            
            code = prod.get(PRODUCT_CODE_FIELD, "N/A")
            name = prod.get('display_name', "Không tên")
            qty = int(m.get('product_uom_qty') or 0)
            
            from_location = m['location_id'][1] if m.get('location_id') else "N/A"
            to_location = m['location_dest_id'][1] if m.get('location_dest_id') else "N/A"
            
            picking_name = m['picking_id'][1] if m.get('picking_id') else "N/A"
            actor = m['write_uid'][1] if m.get('write_uid') else "Hệ thống"
            
            utc_time = datetime.strptime(m['date'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=pytz.utc)
            vn_time_str = utc_time.astimezone(tz_vn).strftime('%H:%M:%S')

            row_data = {
                'Mã SP': code,
                'Tên SP': name,
                'Số lượng': qty,
                'Thời gian': vn_time_str,
                'Người thao tác': actor,
                'Mã lệnh': picking_name
            }

            if hn_stock_name.lower() in to_location.lower():
                row_data['Nhập từ đâu'] = from_location
                import_rows.append(row_data)
            elif hn_stock_name.lower() in from_location.lower():
                row_data['Xuất đi đâu'] = to_location
                export_rows.append(row_data)

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine='openpyxl') as writer:
            df_in = pd.DataFrame(import_rows)
            in_cols = ['Mã SP', 'Tên SP', 'Số lượng', 'Nhập từ đâu', 'Thời gian', 'Người thao tác', 'Mã lệnh']
            if df_in.empty:
                df_in = pd.DataFrame(columns=in_cols)
            else:
                df_in = df_in[in_cols]
            df_in.to_excel(writer, index=False, sheet_name='NHẬP KHO')

            df_out = pd.DataFrame(export_rows)
            out_cols = ['Mã SP', 'Tên SP', 'Số lượng', 'Xuất đi đâu', 'Thời gian', 'Người thao tác', 'Mã lệnh']
            if df_out.empty:
                df_out = pd.DataFrame(columns=out_cols)
            else:
                df_out = df_out[out_cols]
            df_out.to_excel(writer, index=False, sheet_name='XUẤT KHO')

        buf.seek(0)
        return buf, "Thành công"
    except Exception as e:
        logger.error(f"Lỗi tạo báo cáo ngày: {e}")
        return None, str(e)


async def daily_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    register_chat_id(chat_id)
    
    await update.message.reply_text("⌛️ Anh đang tổng hợp dữ liệu Xuất/Nhập kho hôm nay...")
    
    excel_buffer, error_msg = get_daily_movement_report()
    
    if excel_buffer:
        today_str = datetime.now(pytz.timezone("Asia/Ho_Chi_Minh")).strftime("%d-%m-%Y")
        await update.message.reply_document(
            document=excel_buffer,
            filename=f"Bao_cao_kho_ngay_{today_str}.xlsx",
            caption=f"📊 Báo cáo luồng hàng Nhập/Xuất ngày {today_str} đã sẵn sàng ạ!"
        )
    else:
        await update.message.reply_text(f"❌ Không thể tạo báo cáo. Chi tiết: {error_msg}")


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    register_chat_id(chat_id)

    await update.message.reply_text("Đang kiểm tra kết nối odoo, xin chờ...")
    uid, _, error_msg = connect_odoo()
    if uid:
        await update.message.reply_text(f"✅ Thành công! Kết nối Odoo DB: {ODOO_DB}")
    else:
        await update.message.reply_text(f"❌ Lỗi: {error_msg}")


async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    register_chat_id(chat_id)

    await update.message.reply_text("⌛️ Anh đang xử lý dữ liệu và tạo báo cáo Excel...")
    excel_buffer, item_count, error_msg = get_stock_data()

    if excel_buffer is None:
        await update.message.reply_text(f"❌ Lỗi: {error_msg}")
        return

    if item_count > 0:
        await update.message.reply_document(
            document=excel_buffer,
            filename="de_xuat_keo_hang.xlsx",
            caption=f"Đã tìm thấy {item_count} sản phẩm cần kéo hàng."
        )
    else:
        await update.message.reply_text(
            f"Không có sản phẩm nào cần kéo hàng (đủ tồn {TARGET_MIN_QTY})."
        )



# =====================================================================
# ---> GIAO DIỆN MENU PHÂN CẤP TELEGRAM <---
# Chỉ là lớp điều hướng UI. Tất cả command/nghiệp vụ cũ vẫn được giữ nguyên.
# =====================================================================
MENU_MAIN = "🏠 Menu chính"
MENU_BACK = "⬅️ Menu chính"


def _main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📦 Kho & PO", "🧾 Đơn hàng"],
            ["📊 Doanh số", "🔎 Tra cứu & AI"],
            ["👤 Nhân viên", "⚙️ Hệ thống"],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Chọn nhóm chức năng...",
    )


def _warehouse_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🔍 Tra tồn sản phẩm", "📥 Đề xuất kéo hàng"],
            ["📄 Kiểm tra PO", "📊 Báo cáo kho ngày"],
            ["🏭 Tồn kho theo kho", "🔄 Chuyển kho"],
            [MENU_BACK],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Chọn chức năng kho...",
    )


def _orders_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🧾 Lên đơn Odoo", "👥 Đơn theo khách"],
            ["🔎 Kiểm tra mã đơn", "📅 Tổng hợp theo ngày"],
            [MENU_BACK],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Chọn chức năng đơn hàng...",
    )


def _sales_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📊 Tổng quan doanh số", "🚨 Điểm yếu"],
            ["🧾 Odoo hôm nay", "🔔 Báo cáo Odoo ngày"],
            ["📍 Chọn điểm bán", "📈 Xu hướng 7 ngày"],
            ["⚠️ Thiếu dữ liệu", "🔔 Theo dõi cảnh báo"],
            [MENU_BACK],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Chọn báo cáo doanh số...",
    )


def _lookup_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["💰 Hỏi giá sản phẩm", "📤 Cập nhật bảng giá"],
            ["🌐 Hỏi AI / Internet", "🌤 Thời tiết / Tin tức"],
            [MENU_BACK],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Chọn chức năng tra cứu...",
    )


def _staff_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["👤 Báo danh Odoo"],
            [MENU_BACK],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Chọn chức năng nhân viên...",
    )


def _system_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🔌 Kiểm tra Odoo", "ℹ️ Hướng dẫn nhanh"],
            [MENU_BACK],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Chọn chức năng hệ thống...",
    )


def _set_menu_input_mode(context, mode):
    context.user_data['menu_input_mode'] = mode


def _clear_menu_input_mode(context):
    context.user_data.pop('menu_input_mode', None)


async def _menu_sales_store_picker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mở danh sách điểm từ snapshot; chỉ lần đầu mới cần đọc/parse Sheet."""
    msg = await update.message.reply_text("📍 Đang mở doanh số các điểm...")
    try:
        analysis = await _get_sales_ui_analysis(context, force=False)
        text, markup = build_sales_store_page(analysis, page=0)
        await msg.edit_text(
            text,
            reply_markup=markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Lỗi menu chọn điểm bán: {e}")
        await msg.edit_text(f"❌ Không đọc được dữ liệu doanh số: {e}")
        await msg.edit_text(f"❌ Không đọc được dữ liệu doanh số: {e}")


async def _menu_sales_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    enabled = str(chat_id) in {str(x) for x in _sales_monitor_state().get('subscribers', [])}
    await update.message.reply_text(
        "🔔 <b>THEO DÕI DOANH SỐ</b>\n\n"
        f"Trạng thái chat này: <b>{'ĐANG BẬT' if enabled else 'ĐANG TẮT'}</b>\n"
        "Bấm nút bên dưới để thay đổi. <i>Bot chỉ đọc Google Sheet.</i>",
        reply_markup=_sales_monitor_buttons(chat_id),
        parse_mode="HTML",
    )


async def menu_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Điều hướng các nút menu. Không thay đổi logic của các command nghiệp vụ."""
    text = (update.message.text or '').strip()

    # ---- Menu cấp 1 ----
    if text in {MENU_MAIN, MENU_BACK}:
        _clear_menu_input_mode(context)
        await update.message.reply_text("🏠 <b>MENU CHÍNH</b>\nChọn nhóm chức năng:", reply_markup=_main_menu_keyboard(), parse_mode="HTML")
        return
    if text == "📦 Kho & PO":
        _clear_menu_input_mode(context)
        await update.message.reply_text("📦 <b>KHO & PO</b>\nChọn chức năng:", reply_markup=_warehouse_menu_keyboard(), parse_mode="HTML")
        return
    if text == "🧾 Đơn hàng":
        _clear_menu_input_mode(context)
        await update.message.reply_text("🧾 <b>ĐƠN HÀNG</b>\nChọn chức năng:", reply_markup=_orders_menu_keyboard(), parse_mode="HTML")
        return
    if text == "📊 Doanh số":
        _clear_menu_input_mode(context)
        await update.message.reply_text("📊 <b>DOANH SỐ</b>\nChọn chức năng:", reply_markup=_sales_menu_keyboard(), parse_mode="HTML")
        return
    if text == "🔎 Tra cứu & AI":
        _clear_menu_input_mode(context)
        await update.message.reply_text("🔎 <b>TRA CỨU & AI</b>\nChọn chức năng:", reply_markup=_lookup_menu_keyboard(), parse_mode="HTML")
        return
    if text == "👤 Nhân viên":
        _clear_menu_input_mode(context)
        await update.message.reply_text("👤 <b>NHÂN VIÊN</b>\nChọn chức năng:", reply_markup=_staff_menu_keyboard(), parse_mode="HTML")
        return
    if text == "⚙️ Hệ thống":
        _clear_menu_input_mode(context)
        await update.message.reply_text("⚙️ <b>HỆ THỐNG</b>\nChọn chức năng:", reply_markup=_system_menu_keyboard(), parse_mode="HTML")
        return

    # ---- Kho & PO ----
    if text == "🔍 Tra tồn sản phẩm":
        _set_menu_input_mode(context, 'stock')
        await update.message.reply_text("🔍 Nhập mã sản phẩm cần tra tồn. Ví dụ: AC-281")
        return
    if text == "📥 Đề xuất kéo hàng":
        await excel_report_command(update, context)
        return
    if text == "📄 Kiểm tra PO":
        await checkpo_command(update, context)
        return
    if text == "📊 Báo cáo kho ngày":
        await daily_report_command(update, context)
        return
    if text == "🏭 Tồn kho theo kho":
        _set_menu_input_mode(context, 'warehouse')
        await update.message.reply_text("🏭 Nhập từ khóa tên/mã kho. Ví dụ: 201 hoặc HCM")
        return
    # "🔄 Chuyển kho" được ConversationHandler bắt trực tiếp ở entry_point.

    # ---- Đơn hàng ----
    # "🧾 Lên đơn Odoo" được ConversationHandler bắt trực tiếp ở entry_point.
    if text == "👥 Đơn theo khách":
        _set_menu_input_mode(context, 'customer_orders')
        await update.message.reply_text("👥 Nhập tên khách hàng cần tổng hợp đơn. Ví dụ: HC")
        return
    if text == "🔎 Kiểm tra mã đơn":
        _set_menu_input_mode(context, 'order_code')
        await update.message.reply_text("🔎 Nhập mã đơn cần kiểm tra. Ví dụ: SO001")
        return
    if text == "📅 Tổng hợp theo ngày":
        _set_menu_input_mode(context, 'date_range')
        await update.message.reply_text("📅 Nhập khoảng ngày. Ví dụ: 2 đến 20 hoặc 02/09/2026 đến 20/09/2026")
        return

    # ---- Doanh số ----
    if text == "📊 Tổng quan doanh số":
        await doanhso_command(update, context)
        return
    if text == "🚨 Điểm yếu":
        await canhbao_command(update, context)
        return
    if text == "🧾 Odoo hôm nay":
        await baocaoodoo_command(update, context)
        return
    if text == "🔔 Báo cáo Odoo ngày":
        await _menu_odoo_daily_monitor(update, context)
        return
    if text == "📍 Chọn điểm bán":
        await _menu_sales_store_picker(update, context)
        return
    if text == "📈 Xu hướng 7 ngày":
        await xuhuong_command(update, context)
        return
    if text == "⚠️ Thiếu dữ liệu":
        await thieudulieu_command(update, context)
        return
    if text == "🔔 Theo dõi cảnh báo":
        await _menu_sales_monitor(update, context)
        return

    # ---- Tra cứu & AI ----
    if text == "💰 Hỏi giá sản phẩm":
        _set_menu_input_mode(context, 'price')
        await update.message.reply_text("💰 Nhập câu hỏi giá kèm mã sản phẩm. Ví dụ: Giá AC-281 bao nhiêu?")
        return
    if text == "📤 Cập nhật bảng giá":
        _clear_menu_input_mode(context)
        await update.message.reply_text("📤 Gửi file Excel .xlsx bảng giá vào đây. Bot sẽ xử lý theo luồng bảng giá hiện có.")
        return
    if text in {"🌐 Hỏi AI / Internet", "🌤 Thời tiết / Tin tức"}:
        _set_menu_input_mode(context, 'ai')
        hint = "Nhập câu hỏi cần tra cứu." if text.startswith("🌐") else "Nhập câu hỏi thời tiết hoặc tin tức."
        await update.message.reply_text(f"🌐 {hint}")
        return

    # ---- Nhân viên / hệ thống ----
    if text == "👤 Báo danh Odoo":
        _set_menu_input_mode(context, 'attendance')
        await update.message.reply_text("👤 Nhập email đăng nhập Odoo của nhân viên.")
        return
    if text == "🔌 Kiểm tra Odoo":
        await ping_command(update, context)
        return
    if text == "ℹ️ Hướng dẫn nhanh":
        await update.message.reply_text(
            "ℹ️ CÁCH DÙNG NHANH\n\n"
            "• Chọn nhóm ở Menu chính rồi bấm chức năng cần dùng.\n"
            "• Chức năng cần mã/tên sẽ hỏi tiếp ngay trong chat.\n"
            "• Các lệnh / cũ vẫn hoạt động bình thường để dự phòng.\n"
            "• Google Sheet doanh số chỉ được đọc, bot không có luồng ghi/sửa dữ liệu.",
            reply_markup=_system_menu_keyboard(),
        )
        return

    # Nếu không phải nút menu thì để global_text_filter xử lý như trước.
    await handle_product_code(update, context)


async def handle_menu_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý câu trả lời tiếp theo sau các nút cần nhập tham số."""
    mode = context.user_data.get('menu_input_mode')
    if not mode:
        return False

    text = (update.message.text or '').strip()
    if not text:
        return True
    if text in {MENU_MAIN, MENU_BACK}:
        _clear_menu_input_mode(context)
        await update.message.reply_text("🏠 <b>MENU CHÍNH</b>\nChọn nhóm chức năng:", reply_markup=_main_menu_keyboard(), parse_mode="HTML")
        return True

    # Mỗi prompt chỉ dùng một lần để tránh khóa người dùng trong chế độ nhập.
    _clear_menu_input_mode(context)

    if mode == 'price':
        # Luồng báo giá cũ chỉ kích hoạt khi câu hỏi có từ khóa giá + mã sản phẩm.
        # Không tự biến mã hàng thành một nghiệp vụ khác; nếu người dùng chỉ gõ mã,
        # nhắc nhập rõ câu hỏi giá rồi giữ nguyên mode để tránh bị chuyển sang tra tồn.
        low = text.lower()
        if not any(k in low for k in ['giá', 'bao nhiêu', 'vat', 'bảng giá', 'price']):
            _set_menu_input_mode(context, 'price')
            await update.message.reply_text(
                "💰 Vui lòng nhập rõ câu hỏi giá. Ví dụ: Giá AC-281 bao nhiêu?"
            )
            return True
        await handle_product_code(update, context)
        return True

    if mode in {'stock', 'ai'}:
        # Giữ nguyên cổng AI/tra tồn cũ.
        await handle_product_code(update, context)
        return True

    if mode == 'warehouse':
        old_args = getattr(context, 'args', None)
        context.args = text.split()
        try:
            await dotonkho_command(update, context)
        finally:
            context.args = old_args or []
        return True

    if mode == 'attendance':
        old_args = getattr(context, 'args', None)
        context.args = [text]
        try:
            await baodanh_command(update, context)
        finally:
            context.args = old_args or []
        return True

    if mode == 'customer_orders':
        await export_customer_orders(update, context, text)
        return True

    if mode == 'order_code':
        await check_single_order(update, context, text.upper())
        return True

    if mode == 'date_range':
        date_range = _extract_date_range(f"từ {text}")
        if not date_range:
            _set_menu_input_mode(context, 'date_range')
            await update.message.reply_text(
                "❌ Chưa hiểu khoảng ngày. Nhập lại theo dạng: 2 đến 20 hoặc 02/09/2026 đến 20/09/2026"
            )
            return True
        await export_orders_by_date_range(update, context, date_range[0], date_range[1])
        return True

    return False

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    register_chat_id(chat_id)
    _clear_menu_input_mode(context)

    name = update.message.from_user.first_name or "bạn"
    await update.message.reply_text(
        f"👋 <b>Chào {_tg_html(name)}!</b>\n\n"
        "Chọn nhóm chức năng bên dưới. <i>Các lệnh cũ vẫn hoạt động nhưng không cần nhớ nữa.</i>",
        reply_markup=_main_menu_keyboard(),
        parse_mode="HTML",
    )


async def checkpo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    register_chat_id(chat_id)

    context.user_data['waiting_for_po'] = True
    await update.message.reply_text(
        "Ok, gửi file PO Excel (.xlsx) để Anh kiểm tra tồn kho theo mẫu đối tác gửi nha!"
    )


async def handle_po_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    register_chat_id(chat_id)

    document = update.message.document
    if not document:
        return

    file_name = (document.file_name or "").lower()
    if not file_name.endswith(".xlsx"):
        await update.message.reply_text("Chỉ hỗ trợ file Excel định dạng .xlsx thôi nha các con vợ.")
        return

    if context.user_data.get('waiting_for_po'):
        context.user_data['waiting_for_po'] = False
        await update.message.reply_text("⌛️ Anh đang xử lý file PO, chờ Anh xíu nha...")

        try:
            file = await document.get_file()
            file_bytes = await file.download_as_bytearray()
            excel_buffer, error_msg = process_po_and_build_report(bytes(file_bytes))
            if excel_buffer:
                await update.message.reply_document(
                    document=excel_buffer,
                    filename="kiem_tra_po.xlsx",
                    caption="❤️ Anh gửi file kiểm tra PO và đối chiếu tồn kho đây ạ!"
                )
            else:
                await update.message.reply_text(f"❌ Lỗi: {error_msg}")
        except Exception as e:
            await update.message.reply_text(f"❌ Lỗi khi tải file PO: {e}")
        return
    else:
        await update.message.reply_text("📥 Đang nạp bảng giá mới cho AI...")
        try:
            file = await document.get_file()
            file_bytes = await file.download_as_bytearray()
            success, info = process_price_excel(bytes(file_bytes))
            if success:
                # Đồng bộ JSONBin sau khi nạp bảng giá
                await save_cloud_db(context, chat_id)
                await update.message.reply_text(f"✅ Đã nạp thành công bảng giá ({info}). Các con vợ có thể bắt đầu hỏi giá rồi nha!")
            else:
                await update.message.reply_text(f"❌ Lỗi nạp bảng giá: {info}")
        except Exception as e:
            await update.message.reply_text(f"❌ Lỗi xử lý file: {e}")


# ---------------- HTTP Ping Server ----------------
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def log_message(self, format, *args):
        return

def start_http():
    try:
        server = HTTPServer(("0.0.0.0", 10001), PingHandler)
        logger.info("HTTP ping server chạy port 10001")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Lỗi HTTP server: {e}")

threading.Thread(target=start_http, daemon=True).start()

# ---------------- AUTO-PING ----------------
PING_URL = "https://google.com"

def keep_alive_ping():
    while True:
        try:
            urllib.request.urlopen(PING_URL, timeout=10)
            logger.info("Keep-alive ping sent.")
        except Exception as e:
            logger.warning(f"Keep-alive ping failed: {e}")
        time.sleep(300)

threading.Thread(target=keep_alive_ping, daemon=True).start()


# =====================================================================
# ---> WATCHDOG GOM NHÓM (BATCHING) CHO TẤT CẢ CÁC KHO HÀ NỘI <---
# =====================================================================
last_move_id = 0

def watchdog_batch():
    global last_move_id
    tz = pytz.timezone("Asia/Ho_Chi_Minh")
    WATCH_INTERVAL = 60

    while True:
        try:
            uid, models, err = connect_odoo()
            if not uid:
                logger.error(f"Watchdog không kết nối được Odoo: {err}")
                time.sleep(WATCH_INTERVAL)
                continue

            # 1. Khởi tạo mốc ID mới nhất khi Bot vừa chạy
            if last_move_id == 0:
                latest_move = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    'stock.move', 'search_read',
                    [[('state', '=', 'done')]],
                    {'fields': ['id'], 'limit': 1, 'order': 'id desc'}
                )
                if latest_move:
                    last_move_id = latest_move[0]['id']
                else:
                    last_move_id = -1
                time.sleep(WATCH_INTERVAL)
                continue

            # 2. Tìm các lệnh Done mới sinh ra sau mốc ID
            new_moves = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'stock.move', 'search_read',
                [[('id', '>', last_move_id), ('state', '=', 'done')]],
                {'fields': ['id', 'product_id', 'product_uom_qty', 'location_id', 'location_dest_id', 'picking_id', 'write_uid', 'date']}
            )

            if not new_moves:
                time.sleep(WATCH_INTERVAL)
                continue

            # Cập nhật ID lớn nhất
            max_id = max(m['id'] for m in new_moves)
            last_move_id = max(last_move_id, max_id)

            # Hàm nhận diện kho Hà Nội (Chứa 201 hoặc chữ HN)
            def is_hn_loc(name):
                n = str(name).upper()
                return '201' in n or 'HN' in n or 'HÀ NỘI' in n or 'HA NOI' in n

            # 3. Gom nhóm theo Picking (Phiếu)
            groups = {}
            for m in new_moves:
                src = m.get('location_id')
                dest = m.get('location_dest_id')
                if not src or not dest: continue

                src_name = src[1]
                dest_name = dest[1]

                is_src_hn = is_hn_loc(src_name)
                is_dest_hn = is_hn_loc(dest_name)

                # Chỉ lấy giao dịch dính dáng tới kho Hà Nội
                if not is_src_hn and not is_dest_hn:
                    continue 

                pick = m.get('picking_id')
                pick_id = pick[0] if pick else f"NOPICK_{src[0]}_{dest[0]}"
                pick_name = pick[1] if pick else "N/A"

                group_key = (pick_id, pick_name, src[0], src_name, dest[0], dest_name)
                if group_key not in groups:
                    groups[group_key] = []
                groups[group_key].append(m)

            if not groups:
                time.sleep(WATCH_INTERVAL)
                continue

            # 4. Xử lý từng nhóm Phiếu và gửi thông báo
            for g_key, moves in groups.items():
                pick_id, pick_name, src_id, src_name, dest_id, dest_name = g_key
                is_src_hn = is_hn_loc(src_name)
                is_dest_hn = is_hn_loc(dest_name)

                # Xét hướng biến động của kho HN
                if is_src_hn and not is_dest_hn:
                    direction = "XUẤT KHO"
                    target_loc_id = src_id
                    target_loc_name = src_name
                    sign = -1
                elif is_dest_hn and not is_src_hn:
                    direction = "NHẬP KHO"
                    target_loc_id = dest_id
                    target_loc_name = dest_name
                    sign = 1
                else: 
                    direction = "ĐIỀU CHUYỂN NỘI BỘ"
                    target_loc_id = dest_id 
                    target_loc_name = f"{src_name} ➡️ {dest_name}"
                    sign = 1

                # Tính tổng biến động từng mã SP
                prod_qtys = {}
                for m in moves:
                    pid = m['product_id'][0]
                    pname = m['product_id'][1]
                    qty = float(m.get('product_uom_qty') or 0.0)
                    if pid not in prod_qtys:
                        prod_qtys[pid] = {'name': pname, 'qty': 0}
                    prod_qtys[pid]['qty'] += qty

                # Lấy chi tiết thông tin Phiếu (Trạng thái & Người thao tác)
                state_vn = "Đã duyệt (Hoàn thành)"
                w_uid = moves[0].get('write_uid')
                actor = w_uid[1] if isinstance(w_uid, list) and len(w_uid) > 1 else "Hệ thống"
                
                move_date = moves[0].get('date')
                if move_date:
                    utc_time = datetime.strptime(move_date, '%Y-%m-%d %H:%M:%S').replace(tzinfo=pytz.utc)
                    vn_time_str = utc_time.astimezone(tz).strftime('%H:%M %d/%m/%Y')
                else:
                    vn_time_str = datetime.now(tz).strftime('%H:%M %d/%m/%Y')

                if pick_id and isinstance(pick_id, int):
                    p_info = models.execute_kw(
                        ODOO_DB, uid, ODOO_PASSWORD,
                        "stock.picking", "read",
                        [[pick_id]],
                        {"fields": ["state", "write_uid"]}
                    )
                    if p_info:
                        raw_state = p_info[0].get('state')
                        state_map = {
                            'draft': 'Nháp (Chưa duyệt)',
                            'waiting': 'Đang chờ (Chưa duyệt)',
                            'confirmed': 'Chờ có hàng (Chưa duyệt)',
                            'assigned': 'Sẵn sàng (Chưa duyệt)',
                            'done': 'Đã duyệt (Hoàn thành)',
                            'cancel': 'Đã hủy'
                        }
                        state_vn = state_map.get(raw_state, raw_state) if raw_state else state_vn
                        p_w_uid = p_info[0].get('write_uid')
                        if p_w_uid: actor = p_w_uid[1]

                # Truy vấn tồn kho Odoo để lấy Tồn Mới
                pids = list(prod_qtys.keys())
                prod_info = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    'product.product', 'search_read',
                    [[('id', 'in', pids)]],
                    {'fields': ['id', PRODUCT_CODE_FIELD]}
                )
                pcode_map = {p['id']: p.get(PRODUCT_CODE_FIELD, 'N/A') for p in prod_info}

                loc_to_check = target_loc_id if sign == 1 or direction == "XUẤT KHO" else dest_id
                quants = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    'stock.quant', 'search_read',
                    [[('location_id', '=', loc_to_check), ('product_id', 'in', pids)]],
                    {'fields': ['product_id', 'available_quantity']}
                )
                
                quant_map = {}
                for q in quants:
                    pid = q['product_id'][0]
                    quant_map[pid] = quant_map.get(pid, 0) + float(q.get('available_quantity', 0))

                # Build nội dung thông báo
                msg_header = (
                    f"📦 *Cập nhật tồn kho {target_loc_name} – {direction}*\n\n"
                    f"🔖 *Mã lệnh:* {pick_name}\n"
                    f"🏢 *Lệnh đi cho kho:* {dest_name}\n"
                    f"✅ *Trạng thái lệnh:* {state_vn}\n"
                    f"👤 *Người thao tác:* {actor}\n"
                    f"🕒 *Thời gian:* {vn_time_str}\n\n"
                    f"📝 *CHI TIẾT BIẾN ĐỘNG ({len(prod_qtys)} Mã sản phẩm):*\n\n"
                )

                number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
                
                current_msg = msg_header
                idx = 1
                
                for pid, data in prod_qtys.items():
                    code = pcode_map.get(pid, 'N/A')
                    name = data['name']
                    qty_diff = data['qty']
                    new_ton = int(quant_map.get(pid, 0))
                    
                    emoji = number_emojis[idx-1] if idx <= 10 else f"{idx}."
                    
                    if direction == "XUẤT KHO":
                        diff_str = f"-{int(qty_diff)} SP"
                        icon = "🔻"
                    elif direction == "NHẬP KHO":
                        diff_str = f"+{int(qty_diff)} SP"
                        icon = "🔺"
                    else:
                        diff_str = f"Chuyển {int(qty_diff)} SP"
                        icon = "🔄"

                    line = (
                        f"{emoji} *[{code}]* {name}\n"
                        f"{icon} Biến động: {diff_str}  |  📦 Tồn mới: {new_ton} SP\n\n"
                    )

                    # Băm nhỏ tin nhắn nếu quá dài
                    if len(current_msg) + len(line) > 3800:
                        for chat_id in get_registered_chat_ids():
                            try:
                                bot = Bot(token=TELEGRAM_TOKEN)
                                asyncio.run(bot.send_message(chat_id, current_msg, parse_mode="Markdown"))
                            except Exception as e:
                                logger.error(f"Lỗi gửi thông báo: {e}")
                        current_msg = "" 
                        
                    current_msg += line
                    idx += 1

                # Gửi đoạn tin nhắn cuối cùng
                if current_msg:
                    for chat_id in get_registered_chat_ids():
                        try:
                            bot = Bot(token=TELEGRAM_TOKEN)
                            asyncio.run(bot.send_message(chat_id, current_msg, parse_mode="Markdown"))
                        except Exception as e:
                            logger.error(f"Lỗi gửi thông báo: {e}")

            time.sleep(WATCH_INTERVAL)

        except Exception as e:
            logger.error(f"Lỗi watchdog batch: {e}")
            time.sleep(WATCH_INTERVAL)

threading.Thread(target=watchdog_batch, daemon=True).start()

# =====================================================================
# ---> LOGIC TÍNH NĂNG FORM LÊN ĐƠN (NÚT BẤM) <---
# =====================================================================

def _parse_order_products_local(raw_text):
    """Fallback parser cho /lendon khi Groq không phản hồi."""
    results = []
    chunks = [c.strip() for c in re.split(r'[\n;,]+', str(raw_text)) if c.strip()]

    for chunk in chunks:
        # Mã sản phẩm: chuỗi không có khoảng trắng, có ít nhất một chữ số.
        code_match = re.search(r'(?i)\b(?=[A-Z0-9._/\-]*\d)[A-Z][A-Z0-9._/\-]*\b', chunk)
        if not code_match:
            continue
        code = code_match.group(0).upper()

        # Chiết khấu.
        discount = 0.0
        ck = re.search(r'(?i)(?:ck|chi[eế]t\s*kh[aấ]u)\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*%?', chunk)
        if ck:
            try:
                discount = float(ck.group(1).replace(',', '.'))
            except Exception:
                discount = 0.0

        # Số lượng: ưu tiên ký hiệu x/sl/số lượng, sau đó số ngay sau mã.
        qty = None
        qm = re.search(r'(?i)(?:\bx\s*|\bsl\s*[:=]?\s*|s[oố]\s*l[uư][oợ]ng\s*[:=]?\s*)(\d+)', chunk)
        if qm:
            qty = int(qm.group(1))
        else:
            tail = chunk[code_match.end():]
            qm = re.search(r'^\s*[:xX*\-]?\s*(\d+)\b', tail)
            if qm:
                qty = int(qm.group(1))

        if qty is None or qty <= 0:
            qty = 1

        results.append({"code": code, "qty": qty, "discount": discount})

    return results


def parse_order_products_ai(raw_text):
    prompt = f"""
    Văn bản sản phẩm thô: "{raw_text}"
    Nhiệm vụ: Hãy trích xuất các sản phẩm, số lượng, và phần trăm chiết khấu (nếu có) thành JSON object hợp lệ.
    Định dạng BẮT BUỘC:
    {{
      "products": [
        {{"code": "MÃ_SP_VIẾT_HOA", "qty": SỐ_LƯỢNG_SỐ_NGUYÊN, "discount": PHẦN_TRĂM_CK_SỐ_THỰC_HOẶC_0}}
      ]
    }}
    KHÔNG GIẢI THÍCH, CHỈ TRẢ VỀ JSON OBJECT.
    """
    try:
        content = call_groq_chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            response_format={"type": "json_object"}
        )
        res_json = json.loads(content)
        products = res_json.get('products', []) if isinstance(res_json, dict) else []
        if isinstance(products, list) and products:
            cleaned = []
            for p in products:
                try:
                    code = str(p.get('code', '')).strip().upper()
                    qty = int(float(p.get('qty', 0)))
                    discount = float(p.get('discount', 0) or 0)
                    if code and qty > 0:
                        cleaned.append({"code": code, "qty": qty, "discount": discount})
                except Exception:
                    continue
            if cleaned:
                return cleaned
    except Exception as e:
        logger.error(f"Lỗi AI parse hàng hóa, chuyển sang parser cục bộ: {e}")

    return _parse_order_products_local(raw_text)

async def start_lendon_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    register_chat_id(chat_id)
    
    if chat_id not in cloud_data.get('sales_mapping', {}):
        await update.message.reply_text(
            "❌ *Con vợ chưa Báo danh Chuyên viên Sales!*\n\n"
            "Vui lòng gõ lệnh `/baodanh <email_odoo_của_bạn>` để hệ thống nhận diện danh tính trước khi lên đơn nhé.\n"
            "*(Ví dụ: /baodanh kinhdoanh09@nguonsongviet.vn)*",
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    context.user_data['odoo_salesperson'] = cloud_data['sales_mapping'][chat_id]
    context.user_data['lendon_data'] = {}
    
    await update.message.reply_text(
        "📝 *[BƯỚC 1/3] - KHÁCH HÀNG*\n"
        "Các con vợ vui lòng gõ tên Khách Hàng hoặc chuỗi điện máy cần lên đơn nhé:\n"
        "*(Hoặc gõ /cancel để hủy bỏ Form)*",
        parse_mode='Markdown'
    )
    return LENDON_CUSTOMER

async def lendon_customer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['lendon_data']['customer_raw'] = update.message.text.strip()
    await update.message.reply_text(
        "📝 *[BƯỚC 2/3] - THAM CHIẾU & GIAO HÀNG*\n"
        "Con vợ nhập thông tin tham chiếu, địa chỉ giao hoặc lời dặn kho (Gõ `0` nếu muốn bỏ qua):",
        parse_mode='Markdown'
    )
    return LENDON_REF

async def lendon_ref_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ref_txt = update.message.text.strip()
    context.user_data['lendon_data']['ref_raw'] = "" if ref_txt == "0" else ref_txt
    await update.message.reply_text(
        "📦 *[BƯỚC 3/3] - DANH SÁCH SẢN PHẨM*\n"
        "Con vợ copy paste danh sách mã hàng kèm số lượng và chiết khấu (nếu có) nhé:\n"
        "*(Ví dụ:\nI-28: 3\nAC-350: 5 ck 2%)*",
        parse_mode='Markdown'
    )
    return LENDON_PRODUCTS

async def lendon_products_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products_raw = update.message.text.strip()
    loading_msg = await update.message.reply_text("⌛️ Anh đang đối chiếu dữ liệu Odoo và bóc tách AI, chờ tí...")
    
    cust_raw = context.user_data['lendon_data']['customer_raw']
    ref_raw = context.user_data['lendon_data']['ref_raw']
    
    parsed_items = parse_order_products_ai(products_raw)
    
    uid, models, err = connect_odoo()
    partner_id, partner_name = None, cust_raw
    if uid:
        try:
            partners = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner', 'search_read', 
                                        [[('name', 'ilike', cust_raw)]], {'fields': ['id', 'name'], 'limit': 1})
            if partners:
                partner_id = partners[0]['id']
                partner_name = partners[0]['name']
        except Exception as e:
            logger.error(f"Lỗi tìm đối tác Odoo: {e}")

    context.user_data['lendon_form'] = {
        'partner_id': partner_id,
        'customer_name': partner_name,
        'ref': ref_raw,
        'products': parsed_items,
        'odoo_salesperson': context.user_data['odoo_salesperson'],
        'warehouse_id': None,
        'warehouse_name': "❌ CHƯA CHỌN",
        'channel_id': None,
        'channel_name': "❌ CHƯA CHỌN",
        'pos_id': None,
        'pos_name': "❌ CHƯA CHỌN"
    }
    
    await loading_msg.delete()
    await send_lendon_inline_form(update, context)
    return ConversationHandler.END

async def send_lendon_inline_form(update: Update, context: ContextTypes.DEFAULT_TYPE, query=None):
    form = context.user_data['lendon_form']
    
    prod_txt = ""
    for idx, p in enumerate(form['products'], 1):
        ck_txt = f" (CK: {p['discount']}% )" if p['discount'] > 0 else ""
        prod_txt += f"   {idx}. {p['code']} | SL: *{p['qty']}*{ck_txt}\n"
        
    text_form = (
        f"🧾 *FORM ĐIỀU KHIỂN LÊN ĐƠN HÀNG ODOO*\n\n"
        f"👤 *Sales:* {form['odoo_salesperson']['name']}\n"
        f"🏢 *Khách hàng:* {form['customer_name']}\n"
        f"📝 *Tham chiếu:* {form['ref'] if form['ref'] else '⚙️ Tự động'}\n"
        f"🏭 *Kho xuất:* {form['warehouse_name']}\n"
        f"🏷 *Kênh bán:* {form['channel_name']}\n"
        f"🏬 *Mã điểm POS:* {form['pos_name']}\n\n"
        f"📦 *Chi tiết hàng hóa:*\n{prod_txt}\n"
    )
    
    keyboard = []
    
    keyboard.append([
        InlineKeyboardButton("🏭 Kho HN (201)", callback_data="set_wh_201"),
        InlineKeyboardButton("🏭 Kho HCM (124)", callback_data="set_wh_124"),
        InlineKeyboardButton("🔍 Tìm kho khác...", callback_data="search_wh_open")
    ])
    
    keyboard.append([
        InlineKeyboardButton("🏷 Kênh: ĐIỆN MÁY", callback_data="set_chan_dienmay"),
        InlineKeyboardButton("🏷 Kênh: ONLINE", callback_data="set_chan_online")
    ])
    
    # Nút bấm mới: Tải danh sách POS ĐỘNG từ hệ thống
    keyboard.append([
        InlineKeyboardButton("🔍 Tìm & Chọn POS từ Odoo...", callback_data="search_pos_open")
    ])
        
    if form['warehouse_id'] and form['channel_name'] != "❌ CHƯA CHỌN" and form['pos_name'] != "❌ CHƯA CHỌN":
        keyboard.append([InlineKeyboardButton("✅ XÁC NHẬN - TẠO ĐƠN NHÁP ODOO", callback_data="submit_order_odoo")])
    keyboard.append([InlineKeyboardButton("❌ HỦY BỎ FORM", callback_data="cancel_lendon_form")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if query:
        await query.edit_message_text(text_form, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(text_form, reply_markup=reply_markup, parse_mode='Markdown')

async def lendon_pos_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyword = update.message.text.strip()
    context.user_data['waiting_custom_pos'] = False
    
    uid, models, err = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Không kết nối được Odoo để tìm POS: {err}")
        return
        
    try:
        # Sửa chữ 'pos.config' nếu con vợ dùng model custom khác cho POS
        pos_list = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'pos.config', 'search_read',
                                 [[('name', 'ilike', keyword)]],
                                 {'fields': ['id', 'name'], 'limit': 10})
        if not pos_list:
            await update.message.reply_text(f"📭 Anh không tìm thấy Điểm Bán (POS) nào chứa chữ *{keyword}*.")
            return
            
        keyboard = []
        for p in pos_list:
            keyboard.append([InlineKeyboardButton(f"🏬 {p['name']}", callback_data=f"selectpos_{p['id']}_{p['name'][:20]}")])
        keyboard.append([InlineKeyboardButton("❌ Hủy tìm kiếm", callback_data="back_to_form")])
        
        await update.message.reply_text("✅ Các Điểm bán được tìm thấy, con vợ bấm chọn để nạp vào Form nhé:", 
                                        reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi tìm POS động (Kiểm tra lại tên model trong code nhé): {e}")

async def lendon_warehouse_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyword = update.message.text.strip()
    context.user_data['waiting_custom_wh'] = False
    
    uid, models, err = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Không kết nối được Odoo để tìm kho: {err}")
        return
        
    try:
        locs = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read',
                                 [[('usage', '=', 'internal'), ('display_name', 'ilike', keyword)]],
                                 {'fields': ['id', 'display_name'], 'limit': 5})
        if not locs:
            await update.message.reply_text(f"📭 Không tìm thấy kho nào chứa chữ *{keyword}*, các con vợ bấm lại nút chọn kho nhé.", parse_mode='Markdown')
            return
            
        keyboard = []
        for l in locs:
            keyboard.append([InlineKeyboardButton(f"🏭 {l['display_name']}", callback_data=f"selectwh_{l['id']}_{l['display_name'][:20]}")])
        keyboard.append([InlineKeyboardButton("❌ Hủy tìm kiếm", callback_data="back_to_form")])
        
        await update.message.reply_text("✅ Các kho phù hợp được tìm thấy, con vợ bấm chọn để nạp vào Form nhé:", 
                                        reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi tìm kho động: {e}")

async def lendon_dynamic_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    form = context.user_data.get('lendon_form')
    
    if not form:
        if data != "back_to_form" and not data.startswith("selectwh_") and not data.startswith("selectpos_"):
            await query.message.edit_text("❌ Phiên làm việc đã hết hạn. Vui lòng bấm /lendon để tạo lại.")
        return

    # Lọc luồng select từ tin nhắn search
    if data == "back_to_form":
        await query.message.delete()
        return

    if data.startswith("selectwh_"):
        parts = data.split("_", 2)
        if form:
            form['warehouse_id'] = parts[1]
            form['warehouse_name'] = parts[2]
        await query.message.delete()
            
    elif data.startswith("selectpos_"):
        parts = data.split("_", 2)
        if form:
            form['pos_id'] = parts[1]
            form['pos_name'] = parts[2]
        await query.message.delete()
        
    elif data == "set_wh_201":
        form['warehouse_id'] = '201' 
        form['warehouse_name'] = "201 KHO HÀ NỘI"
    elif data == "set_wh_124":
        form['warehouse_id'] = '124'
        form['warehouse_name'] = "124 KHO HỒ CHÍ MINH"
    elif data == "search_wh_open":
        context.user_data['lendon_msg_id'] = query.message.message_id
        await query.message.reply_text("🔍 Con vợ gõ một phần tên kho hoặc mã kho cần tìm nhé (VD: gia lam, thanh hoa...):")
        context.user_data['waiting_custom_wh'] = True
        return
    elif data == "search_pos_open":
        context.user_data['lendon_msg_id'] = query.message.message_id
        await query.message.reply_text("🔍 Con vợ gõ tên Điểm bán (POS) cần tìm trên Odoo (VD: ECO, HC, Shopee...):")
        context.user_data['waiting_custom_pos'] = True
        return
    elif data == "set_chan_dienmay":
        form['channel_name'] = "ĐIỆN MÁY"
    elif data == "set_chan_online":
        form['channel_name'] = "ONLINE"
    elif data == "cancel_lendon_form":
        context.user_data.pop('lendon_form', None)
        await query.message.edit_text("❌ Đã hủy bỏ biểu mẫu lên đơn hàng!")
        return
    elif data == "submit_order_odoo":
        await query.message.edit_text("⌛️ Đang đẩy đơn hàng nháp trực tiếp lên hệ thống Odoo...")
        success, msg = execute_create_order_odoo(form)
        await query.message.reply_text(msg, parse_mode='Markdown')
        return

    # Re-render the form
    bot = context.bot
    msg_id = context.user_data.get('lendon_msg_id') if data.startswith("select") else query.message.message_id
    
    if msg_id and form:
        prod_txt = ""
        for idx, p in enumerate(form['products'], 1):
            ck_txt = f" (CK: {p['discount']}% )" if p['discount'] > 0 else ""
            prod_txt += f"   {idx}. {p['code']} | SL: *{p['qty']}*{ck_txt}\n"
        text_form = (
            f"🧾 *FORM ĐIỀU KHIỂN LÊN ĐƠN HÀNG ODOO*\n\n"
            f"👤 *Sales:* {form['odoo_salesperson']['name']}\n"
            f"🏢 *Khách hàng:* {form['customer_name']}\n"
            f"📝 *Tham chiếu:* {form['ref'] if form['ref'] else '⚙️ Tự động'}\n"
            f"🏭 *Kho xuất:* {form['warehouse_name']}\n"
            f"🏷 *Kênh bán:* {form['channel_name']}\n"
            f"🏬 *Mã điểm POS:* {form['pos_name']}\n\n"
            f"📦 *Chi tiết hàng hóa:*\n{prod_txt}\n"
        )
        keyboard = [
            [InlineKeyboardButton("🏭 Kho HN (201)", callback_data="set_wh_201"),
             InlineKeyboardButton("🏭 Kho HCM (124)", callback_data="set_wh_124"),
             InlineKeyboardButton("🔍 Tìm kho khác...", callback_data="search_wh_open")],
            [InlineKeyboardButton("🏷 Kênh: ĐIỆN MÁY", callback_data="set_chan_dienmay"),
             InlineKeyboardButton("🏷 Kênh: ONLINE", callback_data="set_chan_online")],
            [InlineKeyboardButton("🔍 Tìm & Chọn POS từ Odoo...", callback_data="search_pos_open")]
        ]
        
        if form['warehouse_id'] and form['channel_name'] != "❌ CHƯA CHỌN" and form['pos_name'] != "❌ CHƯA CHỌN":
            keyboard.append([InlineKeyboardButton("✅ XÁC NHẬN - TẠO ĐƠN NHÁP ODOO", callback_data="submit_order_odoo")])
        keyboard.append([InlineKeyboardButton("❌ HỦY BỎ FORM", callback_data="cancel_lendon_form")])
        
        try:
            await bot.edit_message_text(text_form, chat_id=query.message.chat_id if data.startswith("select") else query.message.chat_id, 
                                        message_id=msg_id, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        except Exception:
            pass

def execute_create_order_odoo(form):
    uid, models, err = connect_odoo()
    if not uid:
        return False, f"❌ *Lỗi Hệ thống Odoo:* {err}"
        
    try:
        partner_id = form['partner_id']
        if not partner_id:
            partners = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner', 'search_read', 
                                        [[('name', 'ilike', form['customer_name'])]], {'fields': ['id'], 'limit': 1})
            if not partners:
                return False, f"❌ Thất bại: Không tìm thấy Đối tác/Khách hàng `{form['customer_name']}` trên Odoo."
            partner_id = partners[0]['id']

        order_lines = []
        for p in form['products']:
            products = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read', 
                                        [[('default_code', '=', p['code'])]], {'fields': ['id'], 'limit': 1})
            if not products:
                return False, f"❌ Thất bại: Không tìm thấy Mã sản phẩm `{p['code']}` trên Odoo."
            
            product_id = products[0]['id']
            line_vals = {
                'product_id': product_id,
                'product_uom_qty': float(p['qty']),
            }
            if p['discount'] > 0:
                line_vals['discount'] = float(p['discount'])
                
            order_lines.append((0, 0, line_vals))

        if not order_lines:
            return False, "❌ Đơn hàng trống, không có hàng hóa hợp lệ."

        order_vals = {
            'partner_id': partner_id,
            'user_id': form['odoo_salesperson']['odoo_user_id'], 
            'client_order_ref': form['ref'] if form['ref'] else f"Bot Telegram ({form['odoo_salesperson']['name']})",
            'state': 'draft', 
            'order_line': order_lines
        }
        
        if form['warehouse_id'] and form['warehouse_id'].isdigit():
            order_vals['warehouse_id'] = int(form['warehouse_id'])

        try:
            order_vals['x_channel'] = form['channel_name']
            if form['pos_id']:
                order_vals['x_pos_branch'] = int(form['pos_id'])
            else:
                order_vals['x_pos_branch'] = form['pos_name']
            order_vals['x_brand'] = "NguonSongViet"
            new_order_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'create', [order_vals])
        except Exception:
            order_vals.pop('x_channel', None)
            order_vals.pop('x_pos_branch', None)
            order_vals.pop('x_brand', None)
            new_order_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'create', [order_vals])

        created_order = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'read', 
                                          [[new_order_id]], {'fields': ['name', 'amount_total']})
        
        order_name = created_order[0]['name']
        total_money = created_order[0]['amount_total']
        
        success_msg = (
            f"🎉 *ĐÃ TẠO ĐƠN HÀNG NHÁP THÀNH CÔNG!*\n\n"
            f"🔖 *Mã đơn Odoo:* `{order_name}`\n"
            f"🏢 *Khách hàng:* {form['customer_name']}\n"
            f"🏭 *Kho xuất:* {form['warehouse_name']}\n"
            f"💰 *Tổng tiền (Odoo tự áp giá chuỗi):* {total_money:,.0f} VNĐ\n"
            f"👤 *Người lập đơn:* {form['odoo_salesperson']['name']}\n\n"
            f"👉 Đơn đã nằm ở trạng thái _Báo Giá / Nháp_. Các con vợ có thể duyệt trên Odoo nhé!"
        )
        return True, success_msg

    except Exception as e:
        logger.error(f"Lỗi khởi tạo đơn RPC: {e}")
        return False, f"❌ *Lỗi Hệ thống Odoo:* {str(e)}"

async def cancel_lendon_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Đã hủy biểu mẫu nhập đơn hàng từng bước.")
    return ConversationHandler.END


# =====================================================================
# ---> [NEW] CỔNG ĐIỀU HƯỚNG CHUYỂN KHO NỘI BỘ (/chuyenkho) <---
# =====================================================================
async def start_chuyenkho_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    register_chat_id(chat_id)
    
    if chat_id not in cloud_data.get('sales_mapping', {}):
        await update.message.reply_text(
            "❌ *Con vợ chưa Báo danh Chuyên viên Sales!*\n\n"
            "Vui lòng gõ lệnh `/baodanh <email_odoo_của_bạn>` để hệ thống nhận diện danh tính trước khi làm phiếu nhé.\n"
            "*(Ví dụ: /baodanh kinhdoanh09@nguonsongviet.vn)*",
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    context.user_data['odoo_salesperson'] = cloud_data['sales_mapping'][chat_id]
    context.user_data['chuyenkho_data'] = {}
    
    await update.message.reply_text(
        "🚚 *TẠO PHIẾU CHUYỂN KHO NỘI BỘ*\n"
        "Con vợ copy paste danh sách mã hàng kèm số lượng cần chuyển nhé:\n"
        "*(Ví dụ:\nI-28: 3\nAC-350: 5)*\n"
        "*(Hoặc gõ /cancel để hủy bỏ Form)*",
        parse_mode='Markdown'
    )
    return CK_PRODUCTS

async def ck_products_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products_raw = update.message.text.strip()
    loading_msg = await update.message.reply_text("⌛️ Anh đang bóc tách hàng hóa, chờ tí...")
    
    parsed_items = parse_order_products_ai(products_raw)
    if not parsed_items:
        await loading_msg.edit_text("❌ Anh không nhận diện được sản phẩm nào hợp lệ. Các con vợ gõ lại hoặc /cancel nhé.")
        return CK_PRODUCTS

    context.user_data['chuyenkho_form'] = {
        'src_id': None,
        'src_name': "❌ CHƯA CHỌN",
        'dest_id': None,
        'dest_name': "❌ CHƯA CHỌN",
        'products': parsed_items,
        'odoo_salesperson': context.user_data['odoo_salesperson']
    }
    
    await loading_msg.delete()
    await send_chuyenkho_inline_form(update, context)
    return ConversationHandler.END

async def send_chuyenkho_inline_form(update: Update, context: ContextTypes.DEFAULT_TYPE, query=None):
    form = context.user_data['chuyenkho_form']
    
    prod_txt = ""
    for idx, p in enumerate(form['products'], 1):
        prod_txt += f"   {idx}. {p['code']} | SL: *{p['qty']}*\n"
        
    text_form = (
        f"🚚 *FORM ĐIỀU KHIỂN CHUYỂN KHO NỘI BỘ*\n\n"
        f"👤 *Người lập:* {form['odoo_salesperson']['name']}\n"
        f"📤 *Kho đi (Xuất):* {form['src_name']}\n"
        f"📥 *Kho đến (Nhập):* {form['dest_name']}\n\n"
        f"📦 *Chi tiết hàng hóa:*\n{prod_txt}\n"
    )
    
    keyboard = [
        [InlineKeyboardButton("🔍 Tìm & Chọn Kho ĐI...", callback_data="ck_search_src")],
        [InlineKeyboardButton("🔍 Tìm & Chọn Kho ĐẾN...", callback_data="ck_search_dest")]
    ]
    
    if form['src_id'] and form['dest_id']:
        keyboard.append([InlineKeyboardButton("✅ XÁC NHẬN TẠO PHIẾU CHUYỂN", callback_data="ck_submit")])
    keyboard.append([InlineKeyboardButton("❌ HỦY BỎ", callback_data="ck_cancel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if query:
        await query.edit_message_text(text_form, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(text_form, reply_markup=reply_markup, parse_mode='Markdown')

async def chuyenkho_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    form = context.user_data.get('chuyenkho_form')
    
    if not form and not data.startswith("selectck_"):
        await query.message.edit_text("❌ Phiên làm việc đã hết hạn.")
        return

    if data == "ck_cancel":
        context.user_data.pop('chuyenkho_form', None)
        await query.message.edit_text("❌ Đã hủy bỏ phiếu chuyển kho!")
        return

    elif data == "ck_search_src":
        context.user_data['ck_msg_id'] = query.message.message_id
        await query.message.reply_text("🔍 Gõ một phần tên KHO ĐI cần tìm:")
        context.user_data['waiting_ck_src_kw'] = True
        return

    elif data == "ck_search_dest":
        context.user_data['ck_msg_id'] = query.message.message_id
        await query.message.reply_text("🔍 Gõ một phần tên KHO ĐẾN cần tìm:")
        context.user_data['waiting_ck_dest_kw'] = True
        return

    elif data == "ck_submit":
        await query.message.edit_text("⌛️ Đang đẩy phiếu chuyển lên hệ thống Odoo...")
        success, msg = execute_create_transfer_odoo(form)
        await query.message.reply_text(msg, parse_mode='Markdown')
        return

    elif data.startswith("selectck_"):
        parts = data.split("_", 3)
        mode = parts[1]
        wh_id = parts[2]
        wh_name = parts[3]
        if form:
            if mode == 'src':
                form['src_id'] = wh_id
                form['src_name'] = wh_name
            else:
                form['dest_id'] = wh_id
                form['dest_name'] = wh_name
        
        await query.message.delete()
        msg_id = context.user_data.get('ck_msg_id')
        if msg_id and form:
            await send_chuyenkho_inline_form(update, context, query)

async def ck_warehouse_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    keyword = update.message.text.strip()
    if mode == 'src':
        context.user_data['waiting_ck_src_kw'] = False
    else:
        context.user_data['waiting_ck_dest_kw'] = False
    
    uid, models, err = connect_odoo()
    if not uid:
        return await update.message.reply_text(f"❌ Lỗi kết nối Odoo: {err}")
        
    try:
        locs = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read',
                                 [[('usage', '=', 'internal'), ('display_name', 'ilike', keyword)]],
                                 {'fields': ['id', 'display_name'], 'limit': 5})
        if not locs:
            return await update.message.reply_text(f"📭 Anh không tìm thấy kho nào chứa chữ *{keyword}*.", parse_mode='Markdown')
            
        keyboard = []
        for l in locs:
            keyboard.append([InlineKeyboardButton(f"🏭 {l['display_name']}", callback_data=f"selectck_{mode}_{l['id']}_{l['display_name'][:20]}")])
        keyboard.append([InlineKeyboardButton("❌ Hủy", callback_data="ck_cancel")])
        
        await update.message.reply_text("✅ Các kho phù hợp đây, con vợ chọn nhé:", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi tìm kho: {e}")

def execute_create_transfer_odoo(form):
    uid, models, err = connect_odoo()
    if not uid:
        return False, f"❌ *Lỗi:* {err}"
        
    try:
        p_types = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.picking.type', 'search_read', 
                                    [[('code', '=', 'internal')]], {'fields': ['id'], 'limit': 1})
        type_id = p_types[0]['id'] if p_types else 1

        pick_vals = {
            'location_id': int(form['src_id']),
            'location_dest_id': int(form['dest_id']),
            'picking_type_id': type_id,
            'origin': f"Bot Telegram ({form['odoo_salesperson']['name']})"
        }
        pick_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.picking', 'create', [pick_vals])

        for p in form['products']:
            prods = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read', 
                                      [[('default_code', '=', p['code'])]], {'fields': ['id', 'uom_id'], 'limit': 1})
            if not prods:
                return False, f"❌ Không tìm thấy mã SP `{p['code']}` trên Odoo."
            
            move_vals = {
                'name': f"Chuyển {p['code']}",
                'picking_id': pick_id,
                'product_id': prods[0]['id'],
                'product_uom_qty': float(p['qty']),
                'product_uom': prods[0]['uom_id'][0] if prods[0].get('uom_id') else 1,
                'location_id': int(form['src_id']),
                'location_dest_id': int(form['dest_id']),
            }
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.move', 'create', [move_vals])
        
        created_pick = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.picking', 'read', [[pick_id]], {'fields': ['name']})
        return True, f"🎉 *ĐÃ TẠO PHIẾU CHUYỂN KHO THÀNH CÔNG!*\n🔖 *Mã phiếu:* `{created_pick[0]['name']}`\n📤 *Từ:* {form['src_name']}\n📥 *Đến:* {form['dest_name']}\n👉 Phiếu đang ở trạng thái Nháp/Sẵn sàng, các con vợ vào Odoo duyệt nhé!"
    except Exception as e:
        logger.error(f"Lỗi tạo phiếu chuyển: {e}")
        return False, f"❌ Lỗi hệ thống: {e}"

async def cancel_chuyenkho_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Đã hủy biểu mẫu tạo phiếu chuyển kho.")
    return ConversationHandler.END



# =====================================================================
# ---> SALES PERFORMANCE MONITOR - GOOGLE SHEET READ ONLY <---
# =====================================================================
# NGUYÊN TẮC CỨNG:
# - Chỉ HTTP GET file XLSX từ Google Sheet. Không có bất kỳ API ghi/sửa/xóa nào.
# - Sheet tháng được chọn theo ngày giờ Việt Nam: T{tháng}.{năm}.
# - Target lấy nguyên từ Sheet; KHÔNG nhân/chia theo số nhân viên.
# - Công nhân viên: S=1, C=1, FULL=2, N/NT/OFF=0.
# - Điểm 2 nhân viên S+C và điểm 1 nhân viên FULL đều được coi là phủ đủ 2 ca/ngày.
# - Thiếu dữ liệu KHÔNG được coi là doanh số 0; kết quả hiệu suất sẽ đánh dấu tạm tính.

SALES_SPREADSHEET_ID = (os.environ.get('SALES_SPREADSHEET_ID') or '1b1oWOxzuo044l93gXlOUBD_7XTbYhwKaFNu38gv_BQg').strip()
SALES_EXPORT_URL = f"https://docs.google.com/spreadsheets/d/{SALES_SPREADSHEET_ID}/export?format=xlsx"
SALES_CACHE_SECONDS = 300
# Cache phân tích hoàn chỉnh để không phải parse lại toàn bộ workbook mỗi lần bấm 1 điểm.
SALES_ANALYSIS_CACHE_SECONDS = 300
# Một dashboard đang mở giữ snapshot lâu hơn để chuyển trang/xem điểm gần như tức thì.
# Người dùng luôn có nút 🔄 Làm mới để lấy dữ liệu mới ngay lập tức.
SALES_UI_SNAPSHOT_SECONDS = 900
SALES_CLOSE_HOUR = 20
SALES_CLOSE_MINUTE = 30
SALES_MAX_ALERT_POINTS = 5
SALES_TZ = pytz.timezone("Asia/Ho_Chi_Minh")

# Báo cáo đơn Odoo hằng ngày. Đây là báo cáo các đơn ĐÃ XÁC NHẬN trên Odoo,
# không thay thế sell-out ký gửi của các chuỗi điện máy. Chỉ READ từ Odoo.
try:
    ODOO_DAILY_WAREHOUSE_ID = int((os.environ.get("ODOO_DAILY_WAREHOUSE_ID") or "201").strip())
except Exception:
    ODOO_DAILY_WAREHOUSE_ID = 201
try:
    ODOO_DAILY_REPORT_HOUR = max(0, min(23, int((os.environ.get("ODOO_DAILY_REPORT_HOUR") or "21").strip())))
except Exception:
    ODOO_DAILY_REPORT_HOUR = 21
try:
    ODOO_DAILY_REPORT_MINUTE = max(0, min(59, int((os.environ.get("ODOO_DAILY_REPORT_MINUTE") or "15").strip())))
except Exception:
    ODOO_DAILY_REPORT_MINUTE = 15

SALES_CACHE_LOCK = threading.Lock()
SALES_CACHE = {"fetched_at": 0.0, "bytes": None, "sheet_names": []}
SALES_ANALYSIS_CACHE_LOCK = threading.Lock()
SALES_ANALYSIS_CACHE = {}

VALID_SCHEDULE_TOKENS = {"S", "C", "FULL", "N", "NT", "OFF"}
SCHEDULE_WORK_UNITS = {"S": 1, "C": 1, "FULL": 2, "N": 0, "NT": 0, "OFF": 0}


def _sales_now():
    return datetime.now(SALES_TZ)


def _is_blank_cell(value):
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except Exception:
        pass
    return str(value).replace("\xa0", " ").strip() == ""


def _clean_cell_text(value):
    if _is_blank_cell(value):
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()


def _parse_sheet_number(value):
    """Đọc số từ XLSX/chuỗi hiển thị Việt Nam. Đơn vị được giữ nguyên như trong Sheet."""
    if _is_blank_cell(value):
        return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return float(value)
        except Exception:
            return 0.0

    s = _clean_cell_text(value)
    if not s:
        return 0.0
    upper = s.upper()
    if upper in {"-", "–", "—", "N/A", "NA", "#DIV/0!", "#VALUE!"}:
        return 0.0

    s = s.replace("₫", "").replace("đ", "").replace("Đ", "").replace(" ", "")
    if s.endswith("%"):
        s = s[:-1]

    # 220.000 / 10.940.000 là phân tách hàng nghìn theo định dạng Sheet.
    if re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", s):
        s = s.replace(".", "")
    elif re.fullmatch(r"-?\d{1,3}(?:,\d{3})+", s):
        s = s.replace(",", "")
    else:
        # Trường hợp thập phân dùng dấu phẩy.
        if "," in s and "." not in s:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    try:
        return float(s)
    except Exception:
        return 0.0


def _normalize_schedule(value):
    if _is_blank_cell(value):
        return ""
    s = _clean_cell_text(value).upper().replace(".", "")
    s = re.sub(r"\s+", "", s)
    # Chỉ chuẩn hóa những biến thể chắc chắn tương đương, không tự đoán ký hiệu lạ.
    aliases = {
        "FULLDAY": "FULL",
        "FULLCA": "FULL",
        "OFFDAY": "OFF",
    }
    return aliases.get(s, s)


def _sheet_name_for_date(target_dt):
    return f"T{target_dt.month}.{target_dt.year}"


def _match_month_sheet_name(sheet_names, target_dt):
    """Luôn ưu tiên đúng tháng/năm; không âm thầm dùng sheet khác tháng."""
    exact = _sheet_name_for_date(target_dt)
    for name in sheet_names:
        if str(name).strip().lower() == exact.lower():
            return name

    wanted_month, wanted_year = target_dt.month, target_dt.year
    for name in sheet_names:
        m = re.match(r"^\s*T\s*0?(\d{1,2})\s*[\.\-/]\s*(20\d{2})\s*$", str(name), re.IGNORECASE)
        if m and int(m.group(1)) == wanted_month and int(m.group(2)) == wanted_year:
            return name
    return None


def _download_sales_workbook(force=False):
    """READ ONLY: duy nhất requests.get(). Không có endpoint/method ghi Google Sheet."""
    now_ts = time.time()
    with SALES_CACHE_LOCK:
        if (
            not force
            and SALES_CACHE.get("bytes")
            and now_ts - float(SALES_CACHE.get("fetched_at") or 0) < SALES_CACHE_SECONDS
        ):
            return SALES_CACHE["bytes"], list(SALES_CACHE.get("sheet_names", []))

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NSV-Sales-Monitor/1.0)",
        "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/octet-stream,*/*",
    }
    res = requests.get(SALES_EXPORT_URL, headers=headers, timeout=35, allow_redirects=True)
    if res.status_code != 200:
        raise RuntimeError(f"Google Sheet trả HTTP {res.status_code}. Hãy kiểm tra quyền Viewer bằng link.")

    content = res.content or b""
    content_type = (res.headers.get("Content-Type") or "").lower()
    if len(content) < 1000 or not content.startswith(b"PK"):
        # XLSX là file ZIP nên luôn bắt đầu bằng PK. Nếu nhận HTML login/error thì dừng ngay.
        hint = ""
        try:
            preview = content[:500].decode("utf-8", errors="ignore").lower()
            if "<html" in preview or "<!doctype" in preview:
                hint = " Google đang trả trang HTML/đăng nhập thay vì XLSX."
        except Exception:
            pass
        raise RuntimeError(
            "Không đọc được XLSX từ Google Sheet." + hint +
            " Bot chỉ có quyền đọc; Sheet cần cho phép tài khoản/link của bot xem dữ liệu."
        )

    try:
        excel = pd.ExcelFile(io.BytesIO(content), engine="openpyxl")
        names = list(excel.sheet_names)
        excel.close()
    except Exception as e:
        raise RuntimeError(f"File Google Sheet tải về không đọc được: {e}")

    with SALES_CACHE_LOCK:
        SALES_CACHE["fetched_at"] = now_ts
        SALES_CACHE["bytes"] = content
        SALES_CACHE["sheet_names"] = names
    return content, names


def _load_sales_sheet(target_dt=None, force=False):
    target_dt = target_dt or _sales_now()
    content, sheet_names = _download_sales_workbook(force=force)
    sheet_name = _match_month_sheet_name(sheet_names, target_dt)
    if not sheet_name:
        available = [n for n in sheet_names if re.match(r"^\s*T\s*\d{1,2}[\.\-/]20\d{2}", str(n), re.IGNORECASE)]
        raise RuntimeError(
            f"Không tìm thấy sheet đúng tháng {_sheet_name_for_date(target_dt)}. "
            f"Các sheet tháng hiện có: {', '.join(map(str, available[-8:])) or 'không xác định'}. "
            "Bot sẽ không lấy sheet tháng khác để tránh phân tích sai dữ liệu."
        )

    try:
        df = pd.read_excel(
            io.BytesIO(content),
            sheet_name=sheet_name,
            header=None,
            dtype=object,
            engine="openpyxl",
        )
    except Exception as e:
        raise RuntimeError(f"Không đọc được sheet {sheet_name}: {e}")
    return sheet_name, df


def _normalize_name(text):
    s = unicodedata.normalize("NFD", _clean_cell_text(text).lower())
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _is_summary_sales_row(a_value, name):
    n = _normalize_name(name)
    a = _normalize_name(a_value)
    summary_terms = [
        "tong cong", "he thong", "phu trach", "nhom ", "chi nhanh ha noi"
    ]
    if any(term in n for term in summary_terms):
        return True
    if a.startswith("nhom") or a == "cnhn":
        return True
    return False


def _looks_like_store_name(name):
    n = _normalize_name(name)
    terms = [
        "nguyen kim", "aeon", "kohan", "kohnan", "hc ", " hc", "media mart",
        "mediamart", "pico", "dien may", "big c", "go ", "vincom", "showroom",
        "lotte", "mega market", "mm mega", "coop", "emart", "mega ", "fpt",
        "meta ", "livestream", "live tream", "nha sach", "nha phan phoi",
        "me va be", "wundertute", "thanh ly"
    ]
    padded = f" {n} "
    return any(term in padded for term in terms)


def _looks_like_employee_name(name):
    """Nhận diện dòng nhân sự để không bao giờ xếp nhầm thành điểm bán.

    Sheet thực tế có nhiều dòng nhân viên vẫn có Target, thậm chí cột D/E có '-'/0%
    hoặc doanh số phụ. Vì vậy không thể dựa riêng vào C/D/E để phân loại.
    Hàm này chỉ dùng tín hiệu tên người/chức danh rõ ràng; nếu không chắc thì để
    các tín hiệu cấu trúc ở _row_is_store quyết định.
    """
    n = _normalize_name(name)
    if not n:
        return False

    personnel_terms = [
        "cong tac vien", "tuyen pg", "pg moi", "nhan su moi", "nhan vien",
        "dai dien gian hang", "nhom truong", "tang cuong"
    ]
    if any(term in n for term in personnel_terms):
        return True

    # Ngoại lệ thương hiệu: "Nguyễn Kim ..." là tên chuỗi/điểm bán, không phải
    # tên nhân viên dù bắt đầu bằng một họ Việt Nam phổ biến.
    if n.startswith("nguyen kim"):
        return False

    # Các họ phổ biến trong danh sách nhân sự hiện tại. Nếu tên bắt đầu bằng họ
    # người thì vẫn coi là nhân sự ngay cả khi phía sau có hậu tố điểm làm việc
    # như "- HC HB" hoặc "(Livestream)".
    vietnamese_surnames = {
        "nguyen", "tran", "le", "pham", "hoang", "huynh", "phan", "vu", "vo",
        "dang", "bui", "do", "ho", "ngo", "duong", "ly", "truong", "dinh",
        "nông", "nong", "mai", "luu", "lam", "ha", "dao", "doan", "ta",
        "cao", "chau", "ton", "thai", "quach", "trinh", "dinh", "to"
    }
    first = n.split()[0] if n.split() else ""
    return first in vietnamese_surnames and len(n.split()) >= 2


def _a_cell_is_store_code(value):
    """A thường là mã nhóm/điểm (A, B, I, II, III...) còn nhân viên hay là số/null."""
    a = _normalize_name(value)
    if not a or a.startswith("nhom") or a == "cnhn":
        return False
    if re.fullmatch(r"\d+", a):
        return False
    return bool(re.fullmatch(r"[a-z]+", a))


def _is_non_store_metric_name(name):
    """Các dòng bóc tách doanh số bên dưới một đơn vị, không phải tên điểm/chi nhánh."""
    n = _normalize_name(name)
    if not n:
        return False
    prefixes = (
        "so ban ra", "doanh so selin", "doanh so ",
    )
    if n.startswith(prefixes):
        return True
    return n in {"hang thanh ly"}


def _is_numeric_sales_cell(value):
    """True khi ô ngày là số bán/0/'-' chứ không phải ký hiệu ca hoặc chữ lạ."""
    if _is_blank_cell(value):
        return False
    token = _normalize_schedule(value)
    if token in VALID_SCHEDULE_TOKENS:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return True
    txt = _clean_cell_text(value).replace(" ", "")
    if txt in {"-", "–", "—"}:
        return True
    txt = txt.replace("₫", "").replace("đ", "").replace("Đ", "")
    return bool(re.fullmatch(r"-?\d+(?:[\.,]\d+)*", txt))


def _row_day_cells(row, days_in_month):
    values = []
    for day in range(1, days_in_month + 1):
        idx = 5 + day - 1  # F = ngày 1
        values.append(row.iloc[idx] if idx < len(row) else None)
    return values


def _row_is_store(row, days_in_month):
    name = _clean_cell_text(row.iloc[1] if len(row) > 1 else None)
    if not name:
        return False
    a_value = row.iloc[0] if len(row) > 0 else None
    if _is_summary_sales_row(a_value, name):
        return False
    if _is_non_store_metric_name(name):
        return False

    target = _parse_sheet_number(row.iloc[2] if len(row) > 2 else None)
    if target <= 0:
        return False

    daily = _row_day_cells(row, days_in_month)
    schedule_days = sum(1 for v in daily if _normalize_schedule(v) in VALID_SCHEDULE_TOKENS)
    numeric_days = sum(1 for v in daily if _is_numeric_sales_cell(v))
    actual_value = _parse_sheet_number(row.iloc[3] if len(row) > 3 else None)
    ratio_value = _parse_sheet_number(row.iloc[4] if len(row) > 4 else None)
    actual_entered = not _is_blank_cell(row.iloc[3] if len(row) > 3 else None)
    ratio_entered = not _is_blank_cell(row.iloc[4] if len(row) > 4 else None)

    # QUY TẮC QUAN TRỌNG: dòng có lịch ca rõ ràng là nhân sự, kể cả khi cột
    # D/E có '-' hoặc công thức 0%. Đây là nguyên nhân trước đây tên nhân viên
    # như Đặng Thị Nhung bị xếp nhầm thành một điểm bán.
    if schedule_days >= 2 and schedule_days >= numeric_days:
        return False

    # Tên người/chức danh nhân sự không được trở thành điểm bán chỉ vì có Target
    # hoặc có cột D/E được điền. Nếu họ có doanh số riêng, dữ liệu đó vẫn được
    # giữ dưới điểm bán cha để phân tích nhân sự khi cần.
    if _looks_like_employee_name(name):
        return False

    # Mã A/B/C/I/II/III... là tín hiệu cấu trúc mạnh của một điểm/kênh/nhánh.
    if _a_cell_is_store_code(a_value):
        return True

    # Tên thương hiệu/địa điểm rõ ràng là điểm bán, kể cả doanh số hiện bằng 0.
    if _looks_like_store_name(name):
        return True

    # Các dòng còn lại chỉ coi là điểm/đơn vị khi thực sự có dữ liệu bán hoặc
    # tỷ lệ/actual có ý nghĩa. Không dùng việc ô '-' đơn thuần để tạo điểm giả.
    if numeric_days > 0 or actual_value > 0 or ratio_value > 0:
        return True
    if (actual_entered or ratio_entered) and not _looks_like_employee_name(name):
        return True
    return False


def _row_is_employee(row, days_in_month, inside_store):
    if not inside_store:
        return False
    name = _clean_cell_text(row.iloc[1] if len(row) > 1 else None)
    if not name:
        return False

    daily = _row_day_cells(row, days_in_month)
    schedule_count = sum(1 for v in daily if _normalize_schedule(v) in VALID_SCHEDULE_TOKENS)
    target = _parse_sheet_number(row.iloc[2] if len(row) > 2 else None)

    # Ưu tiên nhận diện người trước _row_is_store vì một số nhân viên có D/E='-'
    # hoặc 0% và vẫn có Target cá nhân.
    if schedule_count > 0 or _looks_like_employee_name(name):
        return target > 0 or schedule_count > 0 or not _is_blank_cell(row.iloc[3] if len(row) > 3 else None)

    if _row_is_store(row, days_in_month):
        return False
    return False


def _parse_sales_sheet(df, year, month, sheet_name):
    days_in_month = calendar.monthrange(year, month)[1]
    stores = []
    branch = None
    current_store = None

    for _, row in df.iterrows():
        name = _clean_cell_text(row.iloc[1] if len(row) > 1 else None)
        if not name:
            continue
        a_value = row.iloc[0] if len(row) > 0 else None
        target = _parse_sheet_number(row.iloc[2] if len(row) > 2 else None)
        actual = _parse_sheet_number(row.iloc[3] if len(row) > 3 else None)
        n = _normalize_name(name)

        if "tong cong chi nhanh" in n:
            branch = {
                "name": name,
                "target": target,
                "actual": actual,
                "daily": _row_day_cells(row, days_in_month),
            }
            current_store = None
            continue

        if _is_summary_sales_row(a_value, name):
            current_store = None
            continue

        if _row_is_store(row, days_in_month):
            current_store = {
                "name": name,
                "target": target,
                "actual": actual,
                "daily": _row_day_cells(row, days_in_month),
                "employees": [],
                "source_row": int(getattr(row, "name", 0)) + 1,
            }
            stores.append(current_store)
            continue

        if _row_is_employee(row, days_in_month, current_store is not None):
            current_store["employees"].append({
                "name": name,
                "target": target,
                "schedule": _row_day_cells(row, days_in_month),
                "source_row": int(getattr(row, "name", 0)) + 1,
            })

    return {
        "year": year,
        "month": month,
        "sheet_name": sheet_name,
        "days_in_month": days_in_month,
        "branch": branch,
        "stores": stores,
    }


def _analysis_completed_day(year, month, now=None):
    now = now or _sales_now()
    if (year, month) < (now.year, now.month):
        return calendar.monthrange(year, month)[1]
    if (year, month) > (now.year, now.month):
        return 0

    include_today = (now.hour, now.minute) >= (SALES_CLOSE_HOUR, SALES_CLOSE_MINUTE)
    return max(0, min(now.day if include_today else now.day - 1, calendar.monthrange(year, month)[1]))


def _employee_schedule_meta(employee, days_in_month, completed_day):
    tokens = [_normalize_schedule(v) for v in employee.get("schedule", [])[:days_in_month]]
    if len(tokens) < days_in_month:
        tokens += [""] * (days_in_month - len(tokens))

    valid_days = [i + 1 for i, t in enumerate(tokens) if t in VALID_SCHEDULE_TOKENS]
    first_valid = valid_days[0] if valid_days else None

    # Nếu lịch tháng đã có nhưng nhân viên bắt đầu giữa tháng, không bắt lỗi trước ngày bắt đầu.
    # Nếu hoàn toàn chưa có lịch và có target nhân viên, coi là chưa khai báo lịch tháng.
    if first_valid is None:
        active_start = 1 if employee.get("target", 0) > 0 else None
    else:
        active_start = first_valid

    missing = []
    work_units = 0
    unknown_tokens = []
    if active_start is not None:
        for d in range(active_start, min(completed_day, days_in_month) + 1):
            token = tokens[d - 1]
            if token == "":
                missing.append(d)
            elif token in VALID_SCHEDULE_TOKENS:
                work_units += SCHEDULE_WORK_UNITS[token]
            else:
                unknown_tokens.append((d, token))

    total_planned_work_units = sum(SCHEDULE_WORK_UNITS.get(t, 0) for t in tokens)
    return {
        "tokens": tokens,
        "active_start": active_start,
        "missing_days": missing,
        "work_units_elapsed": work_units,
        "work_units_month": total_planned_work_units,
        "unknown_tokens": unknown_tokens,
    }


def _coverage_for_day(employee_metas, day):
    morning = False
    afternoon = False
    any_known = False
    any_missing = False
    active_count = 0
    working_count = 0

    for meta in employee_metas:
        start = meta.get("active_start")
        if start is None or day < start:
            continue
        active_count += 1
        tokens = meta.get("tokens", [])
        token = tokens[day - 1] if 0 <= day - 1 < len(tokens) else ""
        if token == "":
            any_missing = True
            continue
        if token in VALID_SCHEDULE_TOKENS:
            any_known = True
        if token == "FULL":
            morning = afternoon = True
            working_count += 1
        elif token == "S":
            morning = True
            working_count += 1
        elif token == "C":
            afternoon = True
            working_count += 1

    shifts = int(morning) + int(afternoon)
    return {
        "shifts": shifts,
        "effective_day": shifts / 2.0,
        "known": any_known,
        "has_missing": any_missing,
        "active_count": active_count,
        "working_count": working_count,
    }


def _status_from_metrics(actual, target, pace, pressure, completed_day):
    if target <= 0:
        return "⚪", "Không target", 0
    if actual >= target:
        return "🟢", "Vượt/đạt target", 0
    if completed_day < 3:
        return "⚪", "Chưa đủ dữ liệu", 1

    p = pace if pace is not None else 0.0
    pr = pressure if pressure is not None else float("inf")
    if p >= 0.95 and pr <= 1.35:
        return "🟢", "Đúng tiến độ", 1
    if p >= 0.80 and pr <= 1.75:
        return "🟡", "Chậm nhẹ", 2
    if p >= 0.60 or pr <= 2.50:
        return "🟠", "Nguy cơ", 3
    return "🔴", "Báo động", 4


def _analyze_store(store, year, month, days_in_month, completed_day):
    employee_metas = []
    employee_results = []
    for emp in store.get("employees", []):
        meta = _employee_schedule_meta(emp, days_in_month, completed_day)
        employee_metas.append(meta)
        employee_results.append({
            "name": emp.get("name"),
            "target": emp.get("target", 0),
            **meta,
        })

    coverage = [_coverage_for_day(employee_metas, day) for day in range(1, days_in_month + 1)]
    known_schedule_days = sum(1 for c in coverage if c["known"] or c["has_missing"])
    planned_known_days = sum(1 for c in coverage if c["known"])
    elapsed_eff = sum(c["effective_day"] for c in coverage[:completed_day])
    total_eff = sum(c["effective_day"] for c in coverage)

    # Nếu lịch cả tháng đủ đáng tin, dùng tiến độ theo chính lịch hoạt động của điểm.
    # Nếu lịch chưa được nhập đủ, dùng tiến độ ngày lịch để không vô tình "thưởng" cho việc bỏ trống lịch.
    schedule_plan_usable = (
        bool(employee_metas)
        and planned_known_days >= max(7, int(days_in_month * 0.60))
        and total_eff > 0
    )
    if schedule_plan_usable:
        expected_ratio = min(1.0, elapsed_eff / total_eff) if total_eff else 0.0
        remaining_eff = max(0.0, total_eff - elapsed_eff)
        pace_basis = "lịch ca điểm"
        elapsed_basis = elapsed_eff
    else:
        expected_ratio = min(1.0, completed_day / days_in_month) if days_in_month else 0.0
        remaining_eff = max(0.0, days_in_month - completed_day)
        pace_basis = "ngày trong tháng"
        elapsed_basis = float(max(completed_day, 0))

    target = float(store.get("target") or 0)
    actual = float(store.get("actual") or 0)
    actual_ratio = actual / target if target > 0 else 0.0
    pace = actual_ratio / expected_ratio if expected_ratio > 0 else None

    avg_per_eff_day = actual / elapsed_basis if elapsed_basis > 0 else 0.0
    remaining_target = max(0.0, target - actual)
    required_per_day = remaining_target / remaining_eff if remaining_eff > 0 else (0.0 if remaining_target <= 0 else float("inf"))
    if avg_per_eff_day > 0:
        pressure = required_per_day / avg_per_eff_day
    elif remaining_target <= 0:
        pressure = 0.0
    else:
        pressure = float("inf")
    forecast = actual + avg_per_eff_day * remaining_eff
    if actual >= target:
        forecast = max(forecast, actual)

    missing_schedule = {}
    total_missing_schedule = 0
    total_work_units = 0
    for emp in employee_results:
        if emp["missing_days"]:
            missing_schedule[emp["name"]] = list(emp["missing_days"])
            total_missing_schedule += len(emp["missing_days"])
        total_work_units += emp.get("work_units_elapsed", 0)

    sales_cells = list(store.get("daily", []))[:days_in_month]
    if len(sales_cells) < days_in_month:
        sales_cells += [None] * (days_in_month - len(sales_cells))

    missing_sales = []
    explicit_zero_sales = []
    daily_sales_values = []
    for day in range(1, days_in_month + 1):
        raw = sales_cells[day - 1]
        val = _parse_sheet_number(raw)
        daily_sales_values.append(val)
        if day > completed_day:
            continue
        cov = coverage[day - 1]
        if _is_blank_cell(raw):
            # Chỉ buộc doanh số khi điểm có người làm hoặc lịch người đang hoạt động bị bỏ trống.
            if cov["shifts"] > 0 or (cov["active_count"] > 0 and cov["has_missing"]):
                missing_sales.append(day)
        else:
            txt = _clean_cell_text(raw).upper()
            if val == 0 and txt in {"0", "0.0", "-", "–", "—"}:
                explicit_zero_sales.append(day)

    data_incomplete = bool(missing_sales or total_missing_schedule)
    icon, label, severity = _status_from_metrics(actual, target, pace, pressure, completed_day)

    # Xu hướng 7 ngày so với 7 ngày ngay trước đó. Chỉ lấy ngày đã hoàn tất.
    end = completed_day
    cur_start = max(1, end - 6)
    prev_end = cur_start - 1
    prev_start = max(1, prev_end - 6)
    last7 = sum(daily_sales_values[cur_start - 1:end]) if end >= cur_start else 0.0
    prev7 = sum(daily_sales_values[prev_start - 1:prev_end]) if prev_end >= prev_start else 0.0
    trend_pct = None
    if prev7 > 0:
        trend_pct = (last7 - prev7) / prev7

    return {
        **store,
        "target": target,
        "actual": actual,
        "actual_ratio": actual_ratio,
        "expected_ratio": expected_ratio,
        "pace": pace,
        "pace_basis": pace_basis,
        "elapsed_effective_days": elapsed_eff,
        "total_effective_days": total_eff,
        "remaining_effective_days": remaining_eff,
        "avg_per_day": avg_per_eff_day,
        "required_per_day": required_per_day,
        "pressure": pressure,
        "forecast": forecast,
        "status_icon": icon,
        "status_label": label,
        "severity": severity,
        "employees_detail": employee_results,
        "coverage": coverage,
        "work_units_elapsed": total_work_units,
        "missing_schedule": missing_schedule,
        "missing_schedule_count": total_missing_schedule,
        "missing_sales_days": missing_sales,
        "explicit_zero_sales_days": explicit_zero_sales,
        "data_incomplete": data_incomplete,
        "schedule_plan_usable": schedule_plan_usable,
        "last7_sales": last7,
        "prev7_sales": prev7,
        "trend_pct": trend_pct,
    }


def get_sales_analysis(target_dt=None, force=False):
    """Load + parse + analyze một lần rồi cache kết quả hoàn chỉnh.

    Trước đây chỉ cache bytes XLSX, nên mỗi lần bấm điểm bán vẫn phải chạy
    pd.read_excel + parse toàn bộ sheet. Cache này loại bỏ phần tốn thời gian đó.
    """
    target_dt = target_dt or _sales_now()
    now_dt = _sales_now()
    completed_day = _analysis_completed_day(target_dt.year, target_dt.month, now_dt)
    cache_key = (int(target_dt.year), int(target_dt.month), int(completed_day))
    now_ts = time.time()

    if not force:
        with SALES_ANALYSIS_CACHE_LOCK:
            cached = SALES_ANALYSIS_CACHE.get(cache_key)
            if (
                cached
                and cached.get("analysis") is not None
                and now_ts - float(cached.get("fetched_at") or 0) < SALES_ANALYSIS_CACHE_SECONDS
            ):
                return cached["analysis"]

    sheet_name, df = _load_sales_sheet(target_dt=target_dt, force=force)
    parsed = _parse_sales_sheet(df, target_dt.year, target_dt.month, sheet_name)
    analyzed = [
        _analyze_store(s, target_dt.year, target_dt.month, parsed["days_in_month"], completed_day)
        for s in parsed["stores"]
        if float(s.get("target") or 0) > 0
        and not _looks_like_employee_name(s.get("name", ""))
    ]
    parsed["stores"] = analyzed
    parsed["completed_day"] = completed_day
    parsed["generated_at"] = now_dt.isoformat(timespec="minutes")

    # Precompute các index dùng nhiều lần trong dashboard.
    visible = [
        st for st in analyzed
        if float(st.get("target") or 0) > 0
        and not _looks_like_employee_name(st.get("name", ""))
    ]
    sorted_stores = sorted(visible, key=lambda x: _normalize_name(x.get("name", "")))
    parsed["_sorted_stores"] = sorted_stores
    parsed["_store_name_index"] = {
        _normalize_name(st.get("name", "")): st for st in sorted_stores
        if _normalize_name(st.get("name", ""))
    }

    with SALES_ANALYSIS_CACHE_LOCK:
        # Chỉ giữ vài snapshot gần nhất để RAM không tăng theo thời gian.
        SALES_ANALYSIS_CACHE[cache_key] = {"fetched_at": now_ts, "analysis": parsed}
        if len(SALES_ANALYSIS_CACHE) > 4:
            oldest = sorted(
                SALES_ANALYSIS_CACHE.items(),
                key=lambda kv: float(kv[1].get("fetched_at") or 0)
            )[:-4]
            for key, _ in oldest:
                SALES_ANALYSIS_CACHE.pop(key, None)
    return parsed


def _fmt_sales_amount(value):
    try:
        v = float(value)
    except Exception:
        return "0"
    if v == float("inf"):
        return "∞"
    sign = "-" if v < 0 else ""
    v = abs(v)
    # Sheet đang dùng đơn vị nghìn đồng: 220.000 = 220 triệu; 10.940.000 = 10,94 tỷ.
    if v >= 1_000_000:
        n = v / 1_000_000
        txt = f"{n:.2f}".rstrip("0").rstrip(".").replace(".", ",")
        return f"{sign}{txt} tỷ"
    if v >= 1_000:
        n = v / 1_000
        txt = f"{n:.1f}".rstrip("0").rstrip(".").replace(".", ",")
        return f"{sign}{txt}tr"
    txt = f"{v:.0f}".replace(".", ",")
    return f"{sign}{txt}k"


def _fmt_pct(value, digits=0):
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.{digits}f}%".replace(".", ",")
    except Exception:
        return "—"


def _fmt_pressure(value):
    if value is None:
        return "—"
    try:
        if value == float("inf") or value > 99:
            return ">99x"
        return f"{value:.1f}x".replace(".", ",")
    except Exception:
        return "—"


def _branch_display_name(analysis):
    raw = _clean_cell_text((analysis.get("branch") or {}).get("name"))
    if not raw:
        return "CHI NHÁNH"
    # Sheet đang để "TỔNG CỘNG CHI NHÁNH HÀ NỘI"; giao diện chỉ cần tên chi nhánh.
    name = re.sub(r"^TỔNG\s+CỘNG\s+", "", raw, flags=re.IGNORECASE).strip()
    return name or raw


def _store_employee_label(store, max_names=2):
    names = [
        _clean_cell_text(e.get("name"))
        for e in (store.get("employees_detail") or [])
        if _clean_cell_text(e.get("name"))
    ]
    if not names:
        return ""
    shown = names[:max_names]
    suffix = f" +{len(names)-max_names} NV" if len(names) > max_names else ""
    return " · ".join(shown) + suffix


def _compact_day_ranges(days):
    vals = sorted({int(d) for d in days if d})
    if not vals:
        return ""
    parts = []
    start = prev = vals[0]
    for d in vals[1:]:
        if d == prev + 1:
            prev = d
            continue
        parts.append(str(start) if start == prev else f"{start}–{prev}")
        start = prev = d
    parts.append(str(start) if start == prev else f"{start}–{prev}")
    return ", ".join(parts)


def _progress_bar(ratio, width=12):
    try:
        r = max(0.0, min(float(ratio), 1.0))
    except Exception:
        r = 0.0
    filled = int(round(r * width))
    return "█" * filled + "░" * (width - filled)


def _sales_buttons():
    """Dashboard doanh số chính - chỉ điều hướng, không thay đổi nghiệp vụ tính toán."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Tổng quan", callback_data="sales:summary"),
            InlineKeyboardButton("🚨 Điểm yếu", callback_data="sales:weak"),
        ],
        [
            InlineKeyboardButton("📍 Điểm bán", callback_data="sales:stores:0"),
            InlineKeyboardButton("📈 Xu hướng", callback_data="sales:trend"),
        ],
        [
            InlineKeyboardButton("⚠️ Thiếu dữ liệu", callback_data="sales:missing"),
            InlineKeyboardButton("🔄 Làm mới", callback_data="sales:refresh"),
        ],
        [
            InlineKeyboardButton("🧾 Odoo hôm nay", callback_data="odoo:today"),
            InlineKeyboardButton("🔔 Odoo mỗi ngày", callback_data="odoo:monitor"),
        ],
        [
            InlineKeyboardButton("🔔 Theo dõi Sheet", callback_data="sales:monitor"),
        ],
    ])


def _sales_visible_stores(analysis):
    """Final guardrail: reports may only contain sales points/units, never employee rows."""
    stores = analysis.get("stores") or []
    return [
        st for st in stores
        if float(st.get("target") or 0) > 0
        and not _looks_like_employee_name(st.get("name", ""))
    ]


def _sales_sorted_stores(analysis):
    cached = analysis.get("_sorted_stores")
    if isinstance(cached, list):
        return cached
    stores = sorted(_sales_visible_stores(analysis), key=lambda x: _normalize_name(x.get("name", "")))
    analysis["_sorted_stores"] = stores
    return stores


def _sales_store_list_buttons(analysis, page=0, per_page=10):
    stores = _sales_sorted_stores(analysis)
    total = len(stores)
    max_page = max(0, (total - 1) // per_page) if total else 0
    page = max(0, min(int(page or 0), max_page))
    start = page * per_page
    end = min(start + per_page, total)

    rows = []
    for idx in range(start, end):
        store = stores[idx]
        name = str(store.get("name") or "Điểm bán")
        if len(name) > 31:
            name = name[:28] + "..."
        pct = _fmt_pct(store.get("actual_ratio"), 0)
        icon = store.get("status_icon") or "📍"
        # Nhìn ngay % target trên nút, không cần mở từng điểm chỉ để xem tiến độ.
        label = f"{icon} {name} · {pct}"
        rows.append([InlineKeyboardButton(label, callback_data=f"sales:store:{idx}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Trước", callback_data=f"sales:stores:{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("Sau ➡️", callback_data=f"sales:stores:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton("🏠 Menu doanh số", callback_data="sales:summary"),
        InlineKeyboardButton("🔄 Cập nhật", callback_data="sales:refresh"),
    ])
    return InlineKeyboardMarkup(rows), page, max_page, total


def build_sales_store_page(analysis, page=0, per_page=10):
    """Trang xem nhanh nhiều điểm; mở chi tiết chỉ khi thật sự cần."""
    stores = _sales_sorted_stores(analysis)
    total = len(stores)
    max_page = max(0, (total - 1) // per_page) if total else 0
    page = max(0, min(int(page or 0), max_page))
    start = page * per_page
    end = min(start + per_page, total)
    generated = str(analysis.get("generated_at") or "")
    generated_time = generated[11:16] if len(generated) >= 16 else "—"

    lines = [
        f"📍 <b>DOANH SỐ TỪNG ĐIỂM — {_tg_html(analysis['sheet_name'])}</b>",
        f"<i>Trang {page+1}/{max_page+1} · {total} điểm · dữ liệu {generated_time}</i>",
        "",
    ]
    if not stores:
        lines.append("Không có điểm bán phù hợp trong sheet tháng hiện tại.")
    else:
        for idx in range(start, end):
            st = stores[idx]
            temp = " · ⚠️ tạm tính" if st.get("data_incomplete") else ""
            lines.append(
                f"{st.get('status_icon','⚪')} <b>{idx+1}. {_tg_html(st.get('name','Điểm bán'))}</b>"
            )
            lines.append(
                f"   {_tg_html(_fmt_sales_amount(st.get('actual',0)))} / "
                f"{_tg_html(_fmt_sales_amount(st.get('target',0)))} · "
                f"<b>{_tg_html(_fmt_pct(st.get('actual_ratio'),0))}</b>{temp}"
            )

    lines += [
        "",
        "<i>Bấm tên điểm bên dưới nếu cần phân tích sâu. Chuyển trang/mở điểm dùng snapshot đã tải, không đọc lại Google Sheet mỗi lần.</i>",
    ]
    markup, page, max_page, total = _sales_store_list_buttons(analysis, page=page, per_page=per_page)
    return "\n".join(lines), markup


def _sales_monitor_buttons(chat_id):
    enabled = str(chat_id) in {str(x) for x in _sales_monitor_state().get("subscribers", [])}
    toggle_text = "🔕 Tắt cảnh báo tự động" if enabled else "🔔 Bật cảnh báo tự động"
    toggle_action = "off" if enabled else "on"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_text, callback_data=f"sales:monitor:{toggle_action}")],
        [InlineKeyboardButton("🏠 Menu doanh số", callback_data="sales:summary")],
    ])


def build_sales_summary(analysis):
    branch = analysis.get("branch") or {}
    stores = _sales_visible_stores(analysis)
    day = analysis.get("completed_day", 0)
    days_in_month = analysis.get("days_in_month", 30)
    target = float(branch.get("target") or sum(st["target"] for st in stores))
    actual = float(branch.get("actual") or sum(st["actual"] for st in stores))
    ratio = actual / target if target > 0 else 0.0
    time_elapsed = day / days_in_month if days_in_month else 0.0
    progress_fit = ratio / time_elapsed if time_elapsed > 0 else None

    counts = {"🔴": 0, "🟠": 0, "🟡": 0, "🟢": 0, "⚪": 0}
    incomplete = 0
    for store in stores:
        icon = store.get("status_icon", "⚪")
        counts[icon] = counts.get(icon, 0) + 1
        if store.get("data_incomplete"):
            incomplete += 1

    weak_reliable = sorted(
        [st for st in stores if st.get("severity", 0) >= 3 and not st.get("data_incomplete")],
        key=lambda x: (x.get("severity", 0), -(x.get("pace") or 0)),
        reverse=True,
    )[:3]

    lines = [
        f"📊 <b>DOANH SỐ — {_tg_html(analysis['sheet_name'])}</b>",
        f"<i>Chốt dữ liệu đến {day:02d}/{analysis['month']:02d}</i>",
        "",
        f"🏢 <b>{_tg_html(_branch_display_name(analysis))}</b>",
        f"• Thực hiện: <b>{_tg_html(_fmt_sales_amount(actual))}</b> / {_tg_html(_fmt_sales_amount(target))}",
        f"• Doanh số đã đạt: <b>{_tg_html(_fmt_pct(ratio,1))}</b>",
        f"• Thời gian tháng đã qua: {_tg_html(_fmt_pct(time_elapsed,1))}",
        f"• Mức bám tiến độ: <b>{_tg_html(_fmt_pct(progress_fit,0))}</b>",
        "",
        "📍 <b>TRẠNG THÁI ĐIỂM BÁN</b>",
        f"🔴 Báo động: <b>{counts.get('🔴',0)}</b>   ·   🟠 Nguy cơ: <b>{counts.get('🟠',0)}</b>",
        f"🟡 Chậm nhẹ: <b>{counts.get('🟡',0)}</b>   ·   🟢 Đúng/Vượt: <b>{counts.get('🟢',0)}</b>",
    ]

    if incomplete:
        lines += [
            "",
            f"⚠️ <b>DỮ LIỆU CHƯA ĐỦ:</b> {incomplete} điểm",
            "<i>Các điểm này chỉ được đánh giá tạm tính cho tới khi bổ sung đủ doanh số/lịch ca.</i>",
        ]

    if weak_reliable:
        lines += ["", "🚨 <b>CẦN CHÚ Ý NHẤT</b>"]
        for idx, store in enumerate(weak_reliable, 1):
            lines += [
                f"{store['status_icon']} <b>{idx}. {_tg_html(store['name'])}</b>",
                f"   Đạt <b>{_tg_html(_fmt_pct(store['actual_ratio'],0))}</b> target · Bám tiến độ <b>{_tg_html(_fmt_pct(store['pace'],0))}</b>",
                f"   Cần {_tg_html(_fmt_sales_amount(store['required_per_day']))}/ngày để theo target",
            ]
    return "\n".join(lines)

def _weak_store_sort_key(s):
    pressure = s.get("pressure")
    if pressure is None or pressure == float("inf"):
        pressure_val = 999.0
    else:
        pressure_val = float(pressure)
    return (s.get("severity", 0), 1.0 - min(s.get("pace") or 0, 1.0), min(pressure_val, 999.0))


def build_weak_sales_report(analysis, max_points=SALES_MAX_ALERT_POINTS):
    stores = _sales_visible_stores(analysis)
    reliable = [st for st in stores if st.get("severity", 0) >= 3 and not st.get("data_incomplete")]
    provisional = [st for st in stores if st.get("severity", 0) >= 3 and st.get("data_incomplete")]
    reliable = sorted(reliable, key=_weak_store_sort_key, reverse=True)
    provisional = sorted(provisional, key=_weak_store_sort_key, reverse=True)

    lines = [
        "🚨 <b>CẢNH BÁO DOANH SỐ</b>",
        f"<i>{_tg_html(_branch_display_name(analysis))} · {_tg_html(analysis['sheet_name'])} · đến ngày {analysis['completed_day']:02d}</i>",
    ]
    if not reliable and not provisional:
        lines += ["", "✅ <b>Chưa có điểm bán nào ở mức Nguy cơ/Báo động.</b>"]
        return "\n".join(lines)

    if reliable:
        lines += ["", f"🔴 <b>ĐIỂM CẦN XỬ LÝ: {len(reliable)}</b>"]
        for idx, st in enumerate(reliable[:max_points], 1):
            lines += [
                f"{st['status_icon']} <b>{idx}. {_tg_html(st['name'])}</b>",
                f"   Doanh số: <b>{_tg_html(_fmt_sales_amount(st['actual']))}</b> / {_tg_html(_fmt_sales_amount(st['target']))}  ·  {_tg_html(_fmt_pct(st['actual_ratio'],0))}",
                f"   Bám tiến độ: <b>{_tg_html(_fmt_pct(st['pace'],0))}</b>  ·  TB {_tg_html(_fmt_sales_amount(st['avg_per_day']))}/ngày",
                f"   Cần: <b>{_tg_html(_fmt_sales_amount(st['required_per_day']))}/ngày</b>  ·  Áp lực {_tg_html(_fmt_pressure(st['pressure']))}",
            ]
        if len(reliable) > max_points:
            lines += ["", f"… còn <b>{len(reliable)-max_points}</b> điểm chưa hiển thị. Bấm <b>📍 Điểm bán</b> để xem chi tiết."]

    if provisional:
        lines += ["", f"⚠️ <b>CHƯA ĐỦ DỮ LIỆU: {len(provisional)} điểm</b>", "<i>Đang có tín hiệu yếu nhưng chưa kết luận chính thức.</i>"]
        for st in provisional[:3]:
            miss = len(st.get("missing_sales_days", [])) + st.get("missing_schedule_count", 0)
            lines.append(
                f"• <b>{_tg_html(st['name'])}</b> · {_tg_html(_fmt_pct(st['actual_ratio'],0))} target · thiếu {miss} ô/ngày"
            )
    return "\n".join(lines)

def build_missing_data_report(analysis, max_points=8):
    stores = [st for st in _sales_visible_stores(analysis) if st.get("data_incomplete")]
    lines = [
        "⚠️ <b>THIẾU DỮ LIỆU</b>",
        f"<i>{_tg_html(analysis['sheet_name'])} · kiểm tra đến ngày {analysis['completed_day']:02d}</i>",
    ]
    if not stores:
        lines += ["", "✅ <b>Doanh số và lịch ca hiện đã đầy đủ trong phạm vi kiểm tra.</b>"]
        return "\n".join(lines)

    total_sales_days = sum(len(st.get("missing_sales_days", [])) for st in stores)
    total_schedule = sum(st.get("missing_schedule_count", 0) for st in stores)
    lines += [
        "",
        "📌 <b>TỔNG HỢP</b>",
        f"• Điểm thiếu dữ liệu: <b>{len(stores)}</b>",
        f"• Ngày doanh số còn trống: <b>{total_sales_days}</b>",
        f"• Ô lịch ca còn trống: <b>{total_schedule}</b>",
        "",
        "🧾 <b>ƯU TIÊN BỔ SUNG</b>",
    ]

    for idx, st in enumerate(stores[:max_points], 1):
        lines.append(f"<b>{idx}. {_tg_html(st['name'])}</b>")
        if st.get("missing_sales_days"):
            lines.append(f"   • Doanh số: <code>{_tg_html(_compact_day_ranges(st['missing_sales_days']))}</code>")
        if st.get("missing_schedule"):
            emp_chunks = []
            for emp_name, days in list(st["missing_schedule"].items())[:2]:
                emp_chunks.append(f"{_tg_html(emp_name)} → <code>{_tg_html(_compact_day_ranges(days))}</code>")
            if len(st["missing_schedule"]) > 2:
                emp_chunks.append(f"+{len(st['missing_schedule'])-2} nhân viên khác")
            for part in emp_chunks:
                lines.append(f"   • Lịch: {part}")

    if len(stores) > max_points:
        lines += ["", f"… còn <b>{len(stores)-max_points}</b> điểm chưa hiển thị để giữ báo cáo gọn."]

    lines += [
        "",
        "ℹ️ <i>Chỉ ô trống mới tính là thiếu. Giá trị 0 hoặc '-' được hiểu là đã khai báo không phát sinh doanh số.</i>",
    ]
    return "\n".join(lines)

def _find_store(analysis, query):
    stores = _sales_sorted_stores(analysis)
    if not stores:
        return None, []
    q = _normalize_name(query)
    if not q:
        return None, []

    exactish = [s for s in stores if q in _normalize_name(s["name"]) or _normalize_name(s["name"]) in q]
    if len(exactish) == 1:
        return exactish[0], []
    if len(exactish) > 1:
        return exactish[0], [s["name"] for s in exactish[:5]]

    names = analysis.get("_store_name_index")
    if not isinstance(names, dict):
        names = {_normalize_name(s["name"]): s for s in stores}
        analysis["_store_name_index"] = names
    close = difflib.get_close_matches(q, list(names.keys()), n=5, cutoff=0.40)
    if not close:
        return None, []
    return names[close[0]], [names[x]["name"] for x in close]


def build_store_detail(analysis, store):
    lines = [
        f"📍 <b>{_tg_html(store['name'])}</b>",
        f"<i>{_tg_html(analysis['sheet_name'])} · {store['status_icon']} {_tg_html(store['status_label'])}</i>",
        "",
        "💰 <b>DOANH SỐ</b>",
        f"• Target: <b>{_tg_html(_fmt_sales_amount(store['target']))}</b>",
        f"• Thực hiện: <b>{_tg_html(_fmt_sales_amount(store['actual']))}</b> · {_tg_html(_fmt_pct(store['actual_ratio'],1))}",
        f"• Thực tế: <code>{_progress_bar(store['actual_ratio'])}</code> {_tg_html(_fmt_pct(store['actual_ratio'],1))}",
        f"• Kỳ vọng: <code>{_progress_bar(store['expected_ratio'])}</code> {_tg_html(_fmt_pct(store['expected_ratio'],1))}",
        "",
        "⚡ <b>HIỆU SUẤT</b>",
        f"• Mức bám tiến độ: <b>{_tg_html(_fmt_pct(store['pace'],0))}</b>",
        f"• TB hiện tại: {_tg_html(_fmt_sales_amount(store['avg_per_day']))}/ngày",
        f"• Cần từ nay: <b>{_tg_html(_fmt_sales_amount(store['required_per_day']))}/ngày</b>",
        f"• Áp lực còn lại: {_tg_html(_fmt_pressure(store['pressure']))}",
        f"• Dự báo cuối tháng: <b>~{_tg_html(_fmt_sales_amount(store['forecast']))}</b>",
    ]

    if store.get("schedule_plan_usable"):
        lines += [
            "",
            "🗓 <b>PHỦ CA & CÔNG</b>",
            f"• Phủ ca hiệu dụng: {store['elapsed_effective_days']:.1f}/{store['total_effective_days']:.1f} ngày",
            f"• Công nhân viên đã ghi nhận: <b>{store['work_units_elapsed']}</b>",
        ]
    else:
        lines += [
            "",
            "🗓 <b>PHỦ CA & CÔNG</b>",
            "• Lịch cả tháng chưa đủ để dùng làm mẫu số đáng tin.",
            f"• Tạm tính tiến độ theo ngày trong tháng · Công đã ghi nhận: <b>{store['work_units_elapsed']}</b>",
        ]

    if store.get("employees_detail"):
        lines += ["", f"👥 <b>NHÂN SỰ TẠI ĐIỂM ({len(store['employees_detail'])})</b>"]
        for emp in store["employees_detail"][:4]:
            lines.append(f"• {_tg_html(emp['name'])}: <b>{emp.get('work_units_elapsed',0)} công</b>")
        if len(store["employees_detail"]) > 4:
            lines.append(f"• +{len(store['employees_detail'])-4} nhân sự khác")

    warnings = []
    if store.get("missing_sales_days"):
        warnings.append("Doanh số: " + _tg_html(_compact_day_ranges(store["missing_sales_days"])))
    if store.get("missing_schedule"):
        for emp_name, days in list(store["missing_schedule"].items())[:4]:
            warnings.append(f"Lịch {_tg_html(emp_name)}: {_tg_html(_compact_day_ranges(days))}")
    if warnings:
        lines += ["", "⚠️ <b>DỮ LIỆU CÒN THIẾU</b>"]
        lines.extend(f"• {w}" for w in warnings)
        lines.append("<i>Các chỉ số hiệu suất hiện chỉ là tạm tính.</i>")

    trend = store.get("trend_pct")
    if trend is not None:
        arrow = "↗" if trend > 0.05 else ("↘" if trend < -0.05 else "→")
        lines += [
            "",
            f"{arrow} <b>XU HƯỚNG 7 NGÀY</b>",
            f"• 7 ngày gần nhất: {_tg_html(_fmt_sales_amount(store['last7_sales']))}",
            f"• 7 ngày trước: {_tg_html(_fmt_sales_amount(store['prev7_sales']))} · {trend*100:+.0f}%",
        ]
    return "\n".join(lines)

def build_sales_trend_report(analysis, query=None):
    if query:
        store, suggestions = _find_store(analysis, query)
        if not store:
            return "❌ <b>Không tìm thấy điểm bán phù hợp.</b>"
        return build_store_detail(analysis, store)

    stores = [st for st in _sales_visible_stores(analysis) if st.get("trend_pct") is not None]
    falling = sorted(stores, key=lambda x: x.get("trend_pct", 0))[:5]
    rising = sorted(stores, key=lambda x: x.get("trend_pct", 0), reverse=True)[:3]
    lines = [
        "📈 <b>XU HƯỚNG 7 NGÀY</b>",
        f"<i>{_tg_html(analysis['sheet_name'])}</i>",
    ]
    if falling:
        lines += ["", "📉 <b>GIẢM MẠNH</b>"]
        for idx, st in enumerate(falling, 1):
            lines.append(f"{idx}. <b>{_tg_html(st['name'])}</b> · {st['trend_pct']*100:+.0f}% · 7 ngày {_tg_html(_fmt_sales_amount(st['last7_sales']))}")
    if rising:
        lines += ["", "📈 <b>TĂNG TỐT</b>"]
        for idx, st in enumerate(rising, 1):
            lines.append(f"{idx}. <b>{_tg_html(st['name'])}</b> · {st['trend_pct']*100:+.0f}% · 7 ngày {_tg_html(_fmt_sales_amount(st['last7_sales']))}")
    if not falling and not rising:
        lines += ["", "Chưa đủ dữ liệu hai giai đoạn 7 ngày để so sánh."]
    return "\n".join(lines)

def _sales_monitor_state():
    state = cloud_data.setdefault("sales_monitor", {})
    state.setdefault("subscribers", [])
    state.setdefault("last_state_fingerprint", "")
    state.setdefault("last_close_report_date", "")
    state.setdefault("last_weekly_check", "")
    return state


def _sales_subscribers():
    raw = _sales_monitor_state().get("subscribers", [])
    out = []
    for cid in raw:
        try:
            out.append(int(cid))
        except Exception:
            out.append(cid)
    return out


def _sales_issue_fingerprint(analysis):
    payload = []
    for s in sorted(_sales_visible_stores(analysis), key=lambda x: _normalize_name(x["name"])):
        if s.get("severity", 0) >= 2 or s.get("data_incomplete"):
            payload.append({
                "name": s["name"],
                "status": s.get("status_icon"),
                "missing_sales": s.get("missing_sales_days", []),
                "missing_schedule": s.get("missing_schedule", {}),
            })
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _send_sales_message(bot, chat_id, text, reply_markup=None):
    """Send structured sales HTML without breaking tags across Telegram chunks."""
    lines = str(text or "").splitlines()
    chunks = []
    current = []
    current_len = 0
    for line in lines:
        add_len = len(line) + (1 if current else 0)
        if current and current_len + add_len > 3400:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += add_len
    if current:
        chunks.append("\n".join(current))
    if not chunks:
        chunks = [""]

    for i, chunk in enumerate(chunks):
        await bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup if i == len(chunks) - 1 else None,
        )

async def _get_sales_analysis_async(force=False):
    return await asyncio.to_thread(get_sales_analysis, None, force)


def _sales_ui_snapshot_is_valid(context):
    try:
        snap = context.chat_data.get("_sales_ui_snapshot") or {}
        analysis = snap.get("analysis")
        if not analysis:
            return False
        if time.time() - float(snap.get("saved_at") or 0) > SALES_UI_SNAPSHOT_SECONDS:
            return False
        now = _sales_now()
        if int(analysis.get("year") or 0) != now.year or int(analysis.get("month") or 0) != now.month:
            return False
        return True
    except Exception:
        return False


async def _get_sales_ui_analysis(context, force=False):
    """Snapshot cho UI Telegram: bấm qua lại không load/parsing lại Sheet."""
    if not force and _sales_ui_snapshot_is_valid(context):
        return context.chat_data["_sales_ui_snapshot"]["analysis"]
    analysis = await _get_sales_analysis_async(force=force)
    context.chat_data["_sales_ui_snapshot"] = {
        "saved_at": time.time(),
        "analysis": analysis,
    }
    return analysis



# =====================================================================
# ---> DAILY ODOO CONFIRMED-SALES REPORT (READ ONLY) <---
# =====================================================================
# Định nghĩa trong module này:
# - "Đơn thật trên Odoo" = sale.order có state sale/done.
# - draft/sent/cancel KHÔNG cộng vào giá trị bán.
# - Mặc định lọc warehouse_id=201 (có thể đổi bằng ODOO_DAILY_WAREHOUSE_ID).
# - Không create/write/unlink bất kỳ dữ liệu nào.


def _odoo_daily_state():
    state = cloud_data.setdefault("odoo_daily_report", {})
    state.setdefault("subscribers", [])
    state.setdefault("last_sent_date", "")
    return state


def _odoo_daily_subscribers():
    out = []
    for cid in _odoo_daily_state().get("subscribers", []):
        try:
            out.append(int(cid))
        except Exception:
            out.append(cid)
    return out


def _odoo_value_name(value, fallback="Không xác định"):
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return str(value[1] or fallback)
    if isinstance(value, dict):
        return str(value.get("display_name") or value.get("name") or fallback)
    if value not in (None, False, ""):
        return str(value)
    return fallback


def _fmt_odoo_money(value):
    try:
        amount = float(value or 0)
    except Exception:
        amount = 0.0
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    if amount >= 1_000_000_000:
        txt = f"{amount/1_000_000_000:.2f}".rstrip("0").rstrip(".") + " tỷ"
    elif amount >= 1_000_000:
        txt = f"{amount/1_000_000:.1f}".rstrip("0").rstrip(".") + "tr"
    elif amount >= 1_000:
        txt = f"{amount/1_000:.0f}k"
    else:
        txt = f"{amount:,.0f}đ"
    return sign + txt


def _odoo_local_day_utc_bounds(report_date):
    local_start = SALES_TZ.localize(datetime(report_date.year, report_date.month, report_date.day, 0, 0, 0))
    local_end = local_start + timedelta(days=1)
    utc_start = local_start.astimezone(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S")
    utc_end = local_end.astimezone(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S")
    return utc_start, utc_end


def _odoo_datetime_to_local_text(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
        dt = pytz.UTC.localize(dt).astimezone(SALES_TZ)
        return dt.strftime("%H:%M")
    except Exception:
        return raw


def _read_odoo_daily_orders_sync(report_date, warehouse_id=None):
    uid, models, err = connect_odoo()
    if not uid:
        raise RuntimeError(err or "Không kết nối được Odoo")

    warehouse_id = ODOO_DAILY_WAREHOUSE_ID if warehouse_id is None else warehouse_id
    start_utc, end_utc = _odoo_local_day_utc_bounds(report_date)

    # Đọc schema trước để field custom thiếu/khác giữa các DB không làm hỏng báo cáo.
    field_info = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'sale.order', 'fields_get', [],
        {'attributes': ['string', 'type']}
    ) or {}

    required = ['id', 'name', 'partner_id', 'state', 'date_order', 'amount_total']
    optional = ['amount_untaxed', 'user_id', 'warehouse_id', 'currency_id',
                'x_pos_branch', 'x_channel', 'client_order_ref', 'invoice_status']
    fields = [f for f in required + optional if f in field_info or f == 'id']
    for f in required:
        if f != 'id' and f not in fields:
            fields.append(f)

    domain = [
        ('date_order', '>=', start_utc),
        ('date_order', '<', end_utc),
    ]
    if warehouse_id:
        if 'warehouse_id' not in field_info:
            raise RuntimeError("Model sale.order không có field warehouse_id; dừng để tránh báo cáo sai phạm vi.")
        domain.append(('warehouse_id', '=', int(warehouse_id)))

    orders = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'sale.order', 'search_read',
        [domain],
        {'fields': fields, 'order': 'date_order asc', 'limit': 5000}
    )
    if orders is None:
        raise RuntimeError("Odoo không trả dữ liệu sale.order. Kiểm tra quyền đọc của tài khoản bot.")

    warehouse_name = f"Kho Odoo ID {warehouse_id}" if warehouse_id else "Tất cả kho"
    if warehouse_id:
        try:
            wh = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'stock.warehouse', 'search_read',
                [[('id', '=', int(warehouse_id))]],
                {'fields': ['id', 'name', 'code'], 'limit': 1}
            ) or []
            if not wh:
                raise RuntimeError(f"Không tìm thấy stock.warehouse ID {warehouse_id}; dừng để tránh cộng nhầm kho.")
            warehouse_name = wh[0].get('name') or warehouse_name
            if wh[0].get('code'):
                warehouse_name = f"{warehouse_name} ({wh[0]['code']})"
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Không xác minh được kho Odoo ID {warehouse_id}: {e}")

    confirmed = [o for o in orders if str(o.get('state') or '').lower() in {'sale', 'done'}]
    quotations = [o for o in orders if str(o.get('state') or '').lower() in {'draft', 'sent'}]
    cancelled = [o for o in orders if str(o.get('state') or '').lower() == 'cancel']
    other = [o for o in orders if str(o.get('state') or '').lower() not in {'sale','done','draft','sent','cancel'}]

    total = sum(float(o.get('amount_total') or 0) for o in confirmed)
    untaxed = sum(float(o.get('amount_untaxed') or 0) for o in confirmed if 'amount_untaxed' in o)
    customers = { _odoo_value_name(o.get('partner_id')) for o in confirmed if o.get('partner_id') }

    by_pos = {}
    by_user = {}
    by_channel = {}
    for o in confirmed:
        amount = float(o.get('amount_total') or 0)
        if 'x_pos_branch' in o and o.get('x_pos_branch') not in (None, False, ''):
            pos = _odoo_value_name(o.get('x_pos_branch'))
            by_pos[pos] = by_pos.get(pos, 0.0) + amount
        if o.get('user_id'):
            user = _odoo_value_name(o.get('user_id'))
            by_user[user] = by_user.get(user, 0.0) + amount
        if 'x_channel' in o and o.get('x_channel') not in (None, False, ''):
            channel = _odoo_value_name(o.get('x_channel'))
            by_channel[channel] = by_channel.get(channel, 0.0) + amount

    return {
        'date': report_date,
        'warehouse_id': warehouse_id,
        'warehouse_name': warehouse_name,
        'all_orders': orders,
        'confirmed': confirmed,
        'quotations': quotations,
        'cancelled': cancelled,
        'other': other,
        'total': total,
        'untaxed': untaxed,
        'customer_count': len(customers),
        'by_pos': by_pos,
        'by_user': by_user,
        'by_channel': by_channel,
    }


async def _get_odoo_daily_report_async(report_date=None, warehouse_id=None):
    report_date = report_date or _sales_now().date()
    return await asyncio.to_thread(_read_odoo_daily_orders_sync, report_date, warehouse_id)


def build_odoo_daily_report(data):
    d = data['date']
    confirmed = data['confirmed']
    quotations = data['quotations']
    cancelled = data['cancelled']
    total = float(data.get('total') or 0)
    avg = total / len(confirmed) if confirmed else 0.0

    lines = [
        f"🧾 <b>ĐƠN BÁN ODOO — {d.strftime('%d/%m/%Y')}</b>",
        f"<i>Nguồn trực tiếp Odoo · {_tg_html(data['warehouse_name'])}</i>",
        "",
        "✅ <b>ĐÃ XÁC NHẬN — TÍNH VÀO BÁO CÁO</b>",
        f"• Số đơn: <b>{len(confirmed)}</b>",
        f"• Tổng giá trị đơn: <b>{_tg_html(_fmt_odoo_money(total))}</b>",
        f"• Khách hàng: <b>{data.get('customer_count', 0)}</b>",
        f"• Trung bình/đơn: {_tg_html(_fmt_odoo_money(avg))}",
        "",
        "🚫 <b>KHÔNG CỘNG VÀO SỐ BÁN</b>",
        f"• Nháp / Báo giá: <b>{len(quotations)}</b>",
        f"• Đã hủy: <b>{len(cancelled)}</b>",
    ]

    if data.get('by_pos'):
        lines += ["", "📍 <b>THEO ĐIỂM / POS ODOO</b>"]
        for name, amount in sorted(data['by_pos'].items(), key=lambda x: x[1], reverse=True)[:6]:
            lines.append(f"• {_tg_html(name)}: <b>{_tg_html(_fmt_odoo_money(amount))}</b>")

    if data.get('by_channel'):
        lines += ["", "🏷 <b>THEO KÊNH</b>"]
        for name, amount in sorted(data['by_channel'].items(), key=lambda x: x[1], reverse=True)[:5]:
            lines.append(f"• {_tg_html(name)}: <b>{_tg_html(_fmt_odoo_money(amount))}</b>")

    if data.get('by_user'):
        lines += ["", "👤 <b>THEO NHÂN VIÊN ODOO</b>"]
        for name, amount in sorted(data['by_user'].items(), key=lambda x: x[1], reverse=True)[:6]:
            lines.append(f"• {_tg_html(name)}: <b>{_tg_html(_fmt_odoo_money(amount))}</b>")

    if confirmed:
        lines += ["", "🧾 <b>ĐƠN GIÁ TRỊ LỚN NHẤT</b>"]
        top_orders = sorted(confirmed, key=lambda o: float(o.get('amount_total') or 0), reverse=True)[:6]
        for idx, o in enumerate(top_orders, 1):
            partner = _odoo_value_name(o.get('partner_id'))
            order_name = o.get('name') or f"#{o.get('id','')}"
            t = _odoo_datetime_to_local_text(o.get('date_order'))
            suffix = f" · {t}" if t else ""
            lines.append(
                f"{idx}. <b>{_tg_html(order_name)}</b> · {_tg_html(partner)}\n"
                f"   {_tg_html(_fmt_odoo_money(o.get('amount_total')))}{_tg_html(suffix)}"
            )
    else:
        lines += ["", "ℹ️ <i>Không có đơn sale/done trong ngày này theo phạm vi kho đang theo dõi.</i>"]

    lines += [
        "",
        "ℹ️ <i>“Số bán Odoo” ở đây là tổng amount_total của sale.order đã xác nhận (sale/done). "
        "Nó không đại diện sell-out ký gửi tại HC/Aeon/... nếu giao dịch đó chưa được ghi thành đơn bán trên Odoo.</i>",
    ]
    return "\n".join(lines)


def _odoo_daily_buttons(chat_id):
    enabled = str(chat_id) in {str(x) for x in _odoo_daily_state().get('subscribers', [])}
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🧾 Hôm nay", callback_data="odoo:today"),
            InlineKeyboardButton("↩️ Hôm qua", callback_data="odoo:yesterday"),
        ],
        [InlineKeyboardButton(
            "🔕 Tắt báo cáo hằng ngày" if enabled else "🔔 Bật báo cáo hằng ngày",
            callback_data="odoo:monitor:off" if enabled else "odoo:monitor:on"
        )],
        [InlineKeyboardButton("📊 Về doanh số Sheet", callback_data="sales:summary")],
    ])


async def _send_odoo_daily_message(bot, chat_id, text, reply_markup=None):
    # Report được thiết kế ngắn; fallback text thường nếu Telegram HTML gặp dữ liệu lạ.
    try:
        await bot.send_message(chat_id=chat_id, text=text[:4000], parse_mode="HTML", reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"Gửi Odoo report HTML lỗi, fallback text thường: {e}")
        plain = re.sub(r"<[^>]+>", "", text)
        await bot.send_message(chat_id=chat_id, text=plain[:4000], reply_markup=reply_markup)


async def baocaoodoo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_chat_id(update.effective_chat.id)
    report_date = _sales_now().date()
    args = [str(x).strip().lower() for x in (context.args or [])]
    if args:
        raw = " ".join(args)
        if raw in {"homqua", "hôm qua", "yesterday"}:
            report_date = report_date - timedelta(days=1)
        else:
            for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
                try:
                    report_date = datetime.strptime(raw, fmt).date()
                    break
                except Exception:
                    pass
    msg = await update.message.reply_text("🧾 Đang đọc các đơn bán đã xác nhận trực tiếp từ Odoo...")
    try:
        data = await _get_odoo_daily_report_async(report_date)
        await msg.edit_text(
            build_odoo_daily_report(data),
            parse_mode="HTML",
            reply_markup=_odoo_daily_buttons(update.effective_chat.id),
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Lỗi /baocaoodoo: {e}")
        await msg.edit_text(f"❌ Không đọc được báo cáo đơn Odoo: {e}")


async def theodoiodoo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    args = [str(x).strip().lower() for x in (context.args or [])]
    state = _odoo_daily_state()
    subscribers = [str(x) for x in state.get('subscribers', [])]
    if not args:
        enabled = chat_id in subscribers
        await update.message.reply_text(
            f"🔔 <b>BÁO CÁO ODOO HẰNG NGÀY</b>\n\n"
            f"Trạng thái: <b>{'ĐANG BẬT' if enabled else 'ĐANG TẮT'}</b>\n"
            f"Giờ gửi mặc định: <b>{ODOO_DAILY_REPORT_HOUR:02d}:{ODOO_DAILY_REPORT_MINUTE:02d}</b> (Việt Nam)",
            parse_mode="HTML",
            reply_markup=_odoo_daily_buttons(update.effective_chat.id),
        )
        return
    action = args[0]
    if action in {"on", "bat", "bật", "1"}:
        if chat_id not in subscribers:
            subscribers.append(chat_id)
        state['subscribers'] = subscribers
        await save_cloud_db(context, update.effective_chat.id)
        await update.message.reply_text("✅ Đã bật báo cáo đơn Odoo hằng ngày cho chat này.")
    elif action in {"off", "tat", "tắt", "0"}:
        state['subscribers'] = [x for x in subscribers if x != chat_id]
        await save_cloud_db(context, update.effective_chat.id)
        await update.message.reply_text("✅ Đã tắt báo cáo đơn Odoo hằng ngày cho chat này.")
    else:
        await update.message.reply_text("Dùng: /theodoiodoo on hoặc /theodoiodoo off")


async def _menu_odoo_daily_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    enabled = str(chat_id) in {str(x) for x in _odoo_daily_state().get('subscribers', [])}
    await update.message.reply_text(
        "🔔 <b>BÁO CÁO ODOO HẰNG NGÀY</b>\n\n"
        f"Trạng thái chat này: <b>{'ĐANG BẬT' if enabled else 'ĐANG TẮT'}</b>\n"
        f"Bot đọc Odoo lúc <b>{ODOO_DAILY_REPORT_HOUR:02d}:{ODOO_DAILY_REPORT_MINUTE:02d}</b> mỗi ngày và chỉ tính đơn <code>sale/done</code>.",
        parse_mode="HTML",
        reply_markup=_odoo_daily_buttons(chat_id),
    )


async def odoo_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    chat_id = query.message.chat_id
    try:
        if data.startswith("odoo:monitor:"):
            action = data.rsplit(":", 1)[-1]
            state = _odoo_daily_state()
            subscribers = [str(x) for x in state.get('subscribers', [])]
            cid = str(chat_id)
            if action == "on":
                if cid not in subscribers:
                    subscribers.append(cid)
                state['subscribers'] = subscribers
                text = (
                    "🔔 <b>BÁO CÁO ODOO HẰNG NGÀY: ĐANG BẬT</b>\n\n"
                    f"Bot sẽ gửi báo cáo lúc <b>{ODOO_DAILY_REPORT_HOUR:02d}:{ODOO_DAILY_REPORT_MINUTE:02d}</b> mỗi ngày."
                )
            else:
                state['subscribers'] = [x for x in subscribers if x != cid]
                text = "🔕 <b>BÁO CÁO ODOO HẰNG NGÀY: ĐANG TẮT</b>"
            await save_cloud_db(context, chat_id)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=_odoo_daily_buttons(chat_id))
            return

        if data == "odoo:monitor":
            enabled = str(chat_id) in {str(x) for x in _odoo_daily_state().get('subscribers', [])}
            text = (
                "🔔 <b>BÁO CÁO ODOO HẰNG NGÀY</b>\n\n"
                f"Trạng thái: <b>{'ĐANG BẬT' if enabled else 'ĐANG TẮT'}</b>\n"
                f"Giờ gửi: <b>{ODOO_DAILY_REPORT_HOUR:02d}:{ODOO_DAILY_REPORT_MINUTE:02d}</b>"
            )
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=_odoo_daily_buttons(chat_id))
            return

        report_date = _sales_now().date()
        if data == "odoo:yesterday":
            report_date -= timedelta(days=1)
        elif data != "odoo:today":
            return
        await query.edit_message_text("🧾 Đang đọc đơn đã xác nhận từ Odoo...")
        report = await _get_odoo_daily_report_async(report_date)
        await query.edit_message_text(
            build_odoo_daily_report(report),
            parse_mode="HTML",
            reply_markup=_odoo_daily_buttons(chat_id),
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Lỗi Odoo daily callback: {e}")
        try:
            await query.edit_message_text(f"❌ Không đọc được báo cáo Odoo: {e}")
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ Không đọc được báo cáo Odoo: {e}")


async def odoo_daily_report_job(context: ContextTypes.DEFAULT_TYPE):
    subscribers = _odoo_daily_subscribers()
    if not subscribers:
        return
    now = _sales_now()
    today_key = now.strftime("%Y-%m-%d")
    state = _odoo_daily_state()
    if state.get('last_sent_date') == today_key:
        return
    try:
        data = await _get_odoo_daily_report_async(now.date())
        text = build_odoo_daily_report(data)
        for cid in subscribers:
            try:
                await _send_odoo_daily_message(context.bot, cid, text, _odoo_daily_buttons(cid))
            except Exception as e:
                logger.error(f"Lỗi gửi Odoo daily report cho {cid}: {e}")
        state['last_sent_date'] = today_key
        await save_cloud_db()
    except Exception as e:
        logger.error(f"Lỗi job báo cáo Odoo hằng ngày: {e}")

async def doanhso_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_chat_id(update.effective_chat.id)
    msg = await update.message.reply_text("📊 Đang đọc Google Sheet và tính tiến độ...")
    try:
        analysis = await _get_sales_ui_analysis(context, force=False)
        await msg.edit_text(build_sales_summary(analysis), reply_markup=_sales_buttons(), parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Lỗi /doanhso: {e}")
        await msg.edit_text(f"❌ Không đọc được dữ liệu doanh số: {e}")


async def canhbao_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_chat_id(update.effective_chat.id)
    msg = await update.message.reply_text("🚨 Đang tính các điểm cần cảnh báo...")
    try:
        analysis = await _get_sales_ui_analysis(context, force=False)
        await msg.edit_text(build_weak_sales_report(analysis), reply_markup=_sales_buttons(), parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Lỗi /canhbao: {e}")
        await msg.edit_text(f"❌ Không đọc được dữ liệu doanh số: {e}")


async def thieudulieu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_chat_id(update.effective_chat.id)
    msg = await update.message.reply_text("⚠️ Đang rà soát ô trống doanh số và lịch ca...")
    try:
        analysis = await _get_sales_ui_analysis(context, force=False)
        await msg.edit_text(build_missing_data_report(analysis), reply_markup=_sales_buttons(), parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Lỗi /thieudulieu: {e}")
        await msg.edit_text(f"❌ Không đọc được dữ liệu doanh số: {e}")


async def diemban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_chat_id(update.effective_chat.id)
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Ví dụ: /diemban thanh hoa hoặc /diemban aeon long bien")
        return
    msg = await update.message.reply_text(f"📍 Đang phân tích điểm '{query}'...")
    try:
        analysis = await _get_sales_ui_analysis(context, force=False)
        store, suggestions = _find_store(analysis, query)
        if not store:
            await msg.edit_text("❌ Không tìm thấy điểm bán phù hợp trong sheet tháng hiện tại.")
            return
        text = build_store_detail(analysis, store)
        if len(suggestions) > 1:
            text += "\n\nGần khớp: " + "; ".join(suggestions[:4])
        await msg.edit_text(text, reply_markup=_sales_buttons(), parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Lỗi /diemban: {e}")
        await msg.edit_text(f"❌ Không đọc được dữ liệu doanh số: {e}")


async def xuhuong_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_chat_id(update.effective_chat.id)
    query = " ".join(context.args).strip()
    msg = await update.message.reply_text("📈 Đang tính xu hướng 7 ngày...")
    try:
        analysis = await _get_sales_ui_analysis(context, force=False)
        await msg.edit_text(build_sales_trend_report(analysis, query or None), reply_markup=_sales_buttons(), parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Lỗi /xuhuong: {e}")
        await msg.edit_text(f"❌ Không đọc được dữ liệu doanh số: {e}")


async def theodoidoanhso_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_chat_id(update.effective_chat.id)
    chat_id = str(update.effective_chat.id)
    args = [str(x).strip().lower() for x in context.args]
    state = _sales_monitor_state()
    subscribers = [str(x) for x in state.get("subscribers", [])]

    if not args:
        enabled = chat_id in subscribers
        await update.message.reply_text(
            f"🔔 Cảnh báo doanh số tự động cho chat này: {'ĐANG BẬT' if enabled else 'ĐANG TẮT'}\n"
            "Dùng /theodoidoanhso on hoặc /theodoidoanhso off."
        )
        return

    action = args[0]
    if action in {"on", "bat", "bật", "1"}:
        if chat_id not in subscribers:
            subscribers.append(chat_id)
        state["subscribers"] = subscribers
        await save_cloud_db(context, update.effective_chat.id)
        await update.message.reply_text(
            "✅ Đã bật theo dõi doanh số cho chat này.\n"
            "Bot chỉ ĐỌC Google Sheet và sẽ gửi cảnh báo khi trạng thái thay đổi, cuối ngày hoặc khi thiếu dữ liệu."
        )
    elif action in {"off", "tat", "tắt", "0"}:
        state["subscribers"] = [x for x in subscribers if x != chat_id]
        await save_cloud_db(context, update.effective_chat.id)
        await update.message.reply_text("✅ Đã tắt cảnh báo doanh số tự động cho chat này.")
    else:
        await update.message.reply_text("Dùng: /theodoidoanhso on hoặc /theodoidoanhso off")


async def _edit_sales_dashboard(query, context, text, reply_markup):
    """Edit one dashboard message using Telegram HTML; send new chunks only when needed."""
    try:
        if len(text) <= 3900:
            await query.edit_message_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return
    except Exception as e:
        logger.debug(f"Không edit được dashboard doanh số, chuyển sang gửi mới: {e}")
    await _send_sales_message(context.bot, query.message.chat_id, text, reply_markup)

async def sales_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    chat_id = query.message.chat_id

    try:
        # Bật/tắt cảnh báo ngay bằng nút - dùng cùng sales_monitor state cũ.
        if data.startswith("sales:monitor:"):
            action = data.rsplit(":", 1)[-1]
            state = _sales_monitor_state()
            subscribers = [str(x) for x in state.get("subscribers", [])]
            cid = str(chat_id)
            if action == "on":
                if cid not in subscribers:
                    subscribers.append(cid)
                state["subscribers"] = subscribers
                status_text = (
                    "🔔 THEO DÕI DOANH SỐ: ĐANG BẬT\n\n"
                    "Bot sẽ chỉ ĐỌC Google Sheet và gửi cảnh báo khi trạng thái thay đổi, "
                    "khi thiếu dữ liệu hoặc ở báo cáo cuối ngày."
                )
            else:
                state["subscribers"] = [x for x in subscribers if x != cid]
                status_text = "🔕 THEO DÕI DOANH SỐ: ĐANG TẮT\n\nBot sẽ không tự động gửi cảnh báo vào chat này."
            await save_cloud_db(context, chat_id)
            await _edit_sales_dashboard(query, context, status_text, _sales_monitor_buttons(chat_id))
            return

        if data == "sales:monitor":
            enabled = str(chat_id) in {str(x) for x in _sales_monitor_state().get("subscribers", [])}
            text = (
                f"🔔 THEO DÕI DOANH SỐ\n\nTrạng thái chat này: {'ĐANG BẬT' if enabled else 'ĐANG TẮT'}\n"
                "Bot chỉ đọc Google Sheet; không có quyền ghi hoặc sửa dữ liệu."
            )
            await _edit_sales_dashboard(query, context, text, _sales_monitor_buttons(chat_id))
            return

        force = data == "sales:refresh"
        analysis = await _get_sales_ui_analysis(context, force=force)

        if data in {"sales:summary", "sales:refresh"}:
            text = build_sales_summary(analysis)
            markup = _sales_buttons()
        elif data == "sales:weak":
            text = build_weak_sales_report(analysis)
            markup = _sales_buttons()
        elif data == "sales:missing":
            text = build_missing_data_report(analysis)
            markup = _sales_buttons()
        elif data == "sales:trend":
            text = build_sales_trend_report(analysis)
            markup = _sales_buttons()
        elif data.startswith("sales:stores:"):
            try:
                page = int(data.rsplit(":", 1)[-1])
            except Exception:
                page = 0
            text, markup = build_sales_store_page(analysis, page=page)
        elif data.startswith("sales:storetrend:"):
            try:
                idx = int(data.rsplit(":", 1)[-1])
            except Exception:
                idx = -1
            stores = _sales_sorted_stores(analysis)
            if idx < 0 or idx >= len(stores):
                text = "❌ Không tìm thấy điểm bán. Bấm Điểm bán để tải lại danh sách."
                markup = _sales_buttons()
            else:
                store = stores[idx]
                text = build_sales_trend_report(analysis, store.get('name'))
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📍 Chi tiết điểm", callback_data=f"sales:store:{idx}")],
                    [InlineKeyboardButton("⬅️ Danh sách điểm bán", callback_data="sales:stores:0")],
                    [InlineKeyboardButton("🏠 Menu doanh số", callback_data="sales:summary"),
                     InlineKeyboardButton("🔄 Làm mới", callback_data="sales:refresh")],
                ])
        elif data.startswith("sales:store:"):
            try:
                idx = int(data.rsplit(":", 1)[-1])
            except Exception:
                idx = -1
            stores = _sales_sorted_stores(analysis)
            if idx < 0 or idx >= len(stores):
                text = "❌ Không tìm thấy điểm bán. Bấm Điểm bán để tải lại danh sách."
                markup = _sales_buttons()
            else:
                text = build_store_detail(analysis, stores[idx])
                # Giữ nút quay lại danh sách + menu chính để không cần gõ lệnh.
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📈 Xu hướng điểm này", callback_data=f"sales:storetrend:{idx}")],
                    [InlineKeyboardButton("⬅️ Danh sách điểm bán", callback_data="sales:stores:0")],
                    [InlineKeyboardButton("🏠 Menu doanh số", callback_data="sales:summary"),
                     InlineKeyboardButton("🔄 Làm mới", callback_data="sales:refresh")],
                ])
        else:
            return

        await _edit_sales_dashboard(query, context, text, markup)
    except Exception as e:
        logger.error(f"Lỗi sales callback: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Không đọc được dữ liệu doanh số: {e}")


def _build_auto_sales_report(analysis, include_summary=False):
    weak = build_weak_sales_report(analysis)
    missing = build_missing_data_report(analysis)
    if include_summary:
        summary = build_sales_summary(analysis)
        return f"{summary}\n\n{weak}\n\n{missing}"
    # Giữ gọn cho cảnh báo giữa ngày.
    if any(s.get("data_incomplete") for s in analysis.get("stores", [])):
        return f"{weak}\n\n{missing}"
    return weak


async def sales_auto_monitor_job(context: ContextTypes.DEFAULT_TYPE):
    subscribers = _sales_subscribers()
    if not subscribers:
        return
    try:
        analysis = await _get_sales_analysis_async(force=True)
        now = _sales_now()
        state = _sales_monitor_state()
        fingerprint = _sales_issue_fingerprint(analysis)
        is_close = (now.hour, now.minute) >= (SALES_CLOSE_HOUR, SALES_CLOSE_MINUTE)
        today_key = now.strftime("%Y-%m-%d")

        should_send = False
        include_summary = False
        if is_close and state.get("last_close_report_date") != today_key:
            should_send = True
            include_summary = True
            state["last_close_report_date"] = today_key
        elif fingerprint != state.get("last_state_fingerprint"):
            should_send = True

        state["last_state_fingerprint"] = fingerprint
        if not should_send:
            return

        text = _build_auto_sales_report(analysis, include_summary=include_summary)
        for cid in subscribers:
            try:
                await _send_sales_message(context.bot, cid, text, _sales_buttons())
            except Exception as e:
                logger.error(f"Lỗi gửi cảnh báo doanh số cho {cid}: {e}")
        await save_cloud_db()
    except Exception as e:
        logger.error(f"Lỗi job theo dõi doanh số: {e}")


def _collect_weekly_missing(analysis, day_set):
    items = []
    for s in analysis.get("stores", []):
        sales_days = sorted(set(s.get("missing_sales_days", [])) & set(day_set))
        schedules = {}
        for emp, days in s.get("missing_schedule", {}).items():
            matched = sorted(set(days) & set(day_set))
            if matched:
                schedules[emp] = matched
        if sales_days or schedules:
            items.append({"name": s["name"], "sales": sales_days, "schedules": schedules})
    return items


def build_weekly_missing_report(now=None):
    now = now or _sales_now()
    this_monday = (now - timedelta(days=now.weekday())).date()
    prev_monday = this_monday - timedelta(days=7)
    prev_sunday = this_monday - timedelta(days=1)

    dates = []
    d = prev_monday
    while d <= prev_sunday:
        dates.append(d)
        d += timedelta(days=1)

    by_month = {}
    for d in dates:
        by_month.setdefault((d.year, d.month), []).append(d.day)

    all_items = []
    for (year, month), days in sorted(by_month.items()):
        target_dt = SALES_TZ.localize(datetime(year, month, 1, 12, 0))
        try:
            analysis = get_sales_analysis(target_dt=target_dt, force=False)
        except Exception as e:
            all_items.append({"name": f"T{month}.{year}", "error": str(e)})
            continue
        for item in _collect_weekly_missing(analysis, days):
            item["sheet"] = analysis["sheet_name"]
            all_items.append(item)

    lines = [f"📅 KIỂM TRA DỮ LIỆU TUẦN {prev_monday.strftime('%d/%m')}–{prev_sunday.strftime('%d/%m')}"]
    valid = [x for x in all_items if not x.get("error")]
    errors = [x for x in all_items if x.get("error")]
    if not valid and not errors:
        lines.append("✅ Không phát hiện dữ liệu còn thiếu trong tuần trước.")
        return "\n".join(lines)

    if valid:
        lines.append(f"⚠️ {len(valid)} điểm còn thiếu dữ liệu:")
        for item in valid[:10]:
            parts = []
            if item.get("sales"):
                parts.append("DS " + _compact_day_ranges(item["sales"]))
            if item.get("schedules"):
                schedule_count = sum(len(v) for v in item["schedules"].values())
                parts.append(f"lịch {schedule_count} ô")
            lines.append(f"• {item['name']} ({item.get('sheet','')}): " + " · ".join(parts))
        if len(valid) > 10:
            lines.append(f"… còn {len(valid)-10} điểm.")
    for err in errors:
        lines.append(f"❌ {err['name']}: {err['error']}")
    return "\n".join(lines)


async def sales_weekly_missing_job(context: ContextTypes.DEFAULT_TYPE):
    subscribers = _sales_subscribers()
    if not subscribers:
        return
    now = _sales_now()
    week_key = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    state = _sales_monitor_state()
    if state.get("last_weekly_check") == week_key:
        return
    try:
        text = await asyncio.to_thread(build_weekly_missing_report, now)
        for cid in subscribers:
            try:
                await _send_sales_message(context.bot, cid, text, _sales_buttons())
            except Exception as e:
                logger.error(f"Lỗi gửi kiểm tra tuần cho {cid}: {e}")
        state["last_weekly_check"] = week_key
        await save_cloud_db()
    except Exception as e:
        logger.error(f"Lỗi job kiểm tra dữ liệu tuần: {e}")

# ---------------- MAIN INITIALIZATION ----------------
def main():
    if not TELEGRAM_TOKEN or not ODOO_URL_RAW or not ODOO_DB or not ODOO_USERNAME or not ODOO_PASSWORD:
        logger.error("Thiếu cấu hình môi trường (token, url, db, user, pass).")
        return

    load_cloud_db() # Tải dữ liệu JSONBin

    logger.info("Groq model: %s | Số API key đã nạp: %s", GROQ_MODEL, len(AI_KEYS))
    if not AI_KEYS:
        logger.warning("Chưa có Groq API key. Các nghiệp vụ rule/Odoo vẫn chạy; chat AI sẽ báo không khả dụng.")

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
        logger.info("đã xóa webhook cũ (nếu có).")
    except Exception as e:
        logger.warning(f"Lỗi xóa webhook: {e}")

    # --- ĐĂNG KÝ LUỒNG CONVERSATION CHO LÊN ĐƠN HÀNG ---
    lendon_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("lendon", start_lendon_command),
            MessageHandler(filters.Regex(r"^🧾 Lên đơn Odoo$"), start_lendon_command),
        ],
        states={
            LENDON_CUSTOMER: [MessageHandler(filters.TEXT & ~filters.COMMAND, lendon_customer_handler)],
            LENDON_REF: [MessageHandler(filters.TEXT & ~filters.COMMAND, lendon_ref_handler)],
            LENDON_PRODUCTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, lendon_products_handler)]
        },
        fallbacks=[CommandHandler("cancel", cancel_lendon_conversation)]
    )
    application.add_handler(lendon_conv_handler)
    
    # --- ĐĂNG KÝ LUỒNG CONVERSATION CHO CHUYỂN KHO ---
    chuyenkho_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("chuyenkho", start_chuyenkho_command),
            MessageHandler(filters.Regex(r"^🔄 Chuyển kho$"), start_chuyenkho_command),
        ],
        states={
            CK_PRODUCTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ck_products_handler)]
        },
        fallbacks=[CommandHandler("cancel", cancel_chuyenkho_conversation)]
    )
    application.add_handler(chuyenkho_conv_handler)

    # --- ĐĂNG KÝ BỘ ĐIỀU HƯỚNG NÚT BẤM CALLBACK FORM (Gộp Kho và POS) ---
    application.add_handler(CallbackQueryHandler(lendon_dynamic_selection_callback, pattern=r"^(selectwh_|selectpos_|back_to_form|set_wh|set_chan|set_pos|search_wh|search_pos|submit_order|cancel_lendon)"))
    application.add_handler(CallbackQueryHandler(chuyenkho_callback_handler, pattern=r"^(ck_|selectck_)"))

    # --- ĐĂNG KÝ CÁC LỆNH COMMAND CŨ (Giữ nguyên vẹn) ---
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("keohang", excel_report_command))
    application.add_handler(CommandHandler("checkpo", checkpo_command))
    application.add_handler(CommandHandler("baocaongay", daily_report_command))
    application.add_handler(CommandHandler("dotonkho", dotonkho_command))  
    application.add_handler(CommandHandler("baodanh", baodanh_command))  

    # --- SALES PERFORMANCE MONITOR (CHỈ ĐỌC GOOGLE SHEET) ---
    application.add_handler(CommandHandler("doanhso", doanhso_command))
    application.add_handler(CommandHandler("canhbao", canhbao_command))
    application.add_handler(CommandHandler("diemban", diemban_command))
    application.add_handler(CommandHandler("xuhuong", xuhuong_command))
    application.add_handler(CommandHandler("thieudulieu", thieudulieu_command))
    application.add_handler(CommandHandler("theodoidoanhso", theodoidoanhso_command))
    application.add_handler(CommandHandler("baocaoodoo", baocaoodoo_command))
    application.add_handler(CommandHandler("odoo", baocaoodoo_command))
    application.add_handler(CommandHandler("theodoiodoo", theodoiodoo_command))
    application.add_handler(CallbackQueryHandler(odoo_callback_handler, pattern=r"^odoo:"))
    application.add_handler(CallbackQueryHandler(sales_callback_handler, pattern=r"^sales:"))
    
    application.add_handler(MessageHandler(filters.Document.ALL, handle_po_file))
    
    # Bộ bắt văn bản thô rà soát kho/POS mở rộng hoặc bóc tách cổng cũ
    async def global_text_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if context.user_data.get('waiting_custom_wh'):
            await lendon_warehouse_search_text(update, context)
        elif context.user_data.get('waiting_custom_pos'):
            await lendon_pos_search_text(update, context)
        elif context.user_data.get('waiting_ck_src_kw'):
            await ck_warehouse_search_text(update, context, 'src')
        elif context.user_data.get('waiting_ck_dest_kw'):
            await ck_warehouse_search_text(update, context, 'dest')
        elif context.user_data.get('menu_input_mode'):
            await handle_menu_input(update, context)
        else:
            await menu_text_handler(update, context)
            
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, global_text_filter))

    # --- ĐĂNG KÝ JOB QUEUE (TỰ ĐỘNG LÊN CƠN) ---
    if application.job_queue:
        application.job_queue.run_repeating(auto_troll_message, interval=7200, first=60)
        # Memory chỉ là lớp giao tiếp. Đồng bộ định kỳ để các câu hỏi Odoo/text
        # cũng được giữ lại qua lần sleep/restart của Render Free mà không làm
        # chậm từng nghiệp vụ bằng một request JSONBin ngay tại mỗi lệnh.
        application.job_queue.run_repeating(flush_ai_memory_job, interval=600, first=120)

        # Cảnh báo doanh số theo giờ Việt Nam.
        # 11:00 và 16:00 chỉ gửi khi trạng thái/missing thay đổi; 20:30 gửi báo cáo cuối ngày.
        application.job_queue.run_daily(sales_auto_monitor_job, time=dt_time(hour=11, minute=0, tzinfo=SALES_TZ))
        application.job_queue.run_daily(sales_auto_monitor_job, time=dt_time(hour=16, minute=0, tzinfo=SALES_TZ))
        application.job_queue.run_daily(sales_auto_monitor_job, time=dt_time(hour=20, minute=30, tzinfo=SALES_TZ))
        # Thứ Hai 09:00 quét lại dữ liệu tuần trước (python-telegram-bot v20+: 1 = Monday).
        application.job_queue.run_daily(sales_weekly_missing_job, time=dt_time(hour=9, minute=0, tzinfo=SALES_TZ), days=(1,))
        # Báo cáo đơn bán Odoo đã xác nhận (sale/done) - độc lập với Google Sheet.
        application.job_queue.run_daily(
            odoo_daily_report_job,
            time=dt_time(hour=ODOO_DAILY_REPORT_HOUR, minute=ODOO_DAILY_REPORT_MINUTE, tzinfo=SALES_TZ),
        )

        logger.info("Đã kích hoạt chế độ Auto Troll mỗi 2 tiếng (Tỷ lệ 30%).")
        logger.info("Đã kích hoạt đồng bộ AI memory lên JSONBin mỗi 10 phút.")
        logger.info("Đã kích hoạt Sales Monitor READ-ONLY lúc 11:00, 16:00, 20:30 và kiểm tra tuần vào Thứ Hai 09:00.")
        logger.info(
            "Đã kích hoạt Odoo Daily Report READ-ONLY lúc %02d:%02d (warehouse_id=%s).",
            ODOO_DAILY_REPORT_HOUR, ODOO_DAILY_REPORT_MINUTE, ODOO_DAILY_WAREHOUSE_ID
        )
    else:
        logger.warning("JobQueue chưa khả dụng. Cần cài đặt python-telegram-bot[job-queue]")

    logger.info("Bot started!")
    application.run_polling()


if __name__ == "__main__":
    main()