import os
import io
import logging
import pandas as pd
import asyncio
import socket
import threading
import time
import urllib.request
import requests
from datetime import datetime
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import pytz

# ================= CONFIG =================
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')

ODOO_URL_RAW = os.environ.get('ODOO_URL').rstrip('/') if os.environ.get('ODOO_URL') else None
if ODOO_URL_RAW and ODOO_URL_RAW.lower().endswith('/odoo'):
    ODOO_URL_FINAL = ODOO_URL_RAW[:-len('/odoo')]
else:
    ODOO_URL_FINAL = ODOO_URL_RAW

ODOO_DB = os.environ.get('ODOO_DB')
ODOO_USERNAME = os.environ.get('ODOO_USERNAME')
ODOO_PASSWORD = os.environ.get('ODOO_PASSWORD')

TARGET_MIN_QTY = 50
PRODUCT_CODE_FIELD = 'default_code'

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

# ================= LOGGING =================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================= KEEP PORT OPEN =================
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

# ================= FIX DUY NHẤT: ODOO CONNECT =================
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

        r = requests.post(f"{ODOO_URL_FINAL}/jsonrpc", json=payload, timeout=15)
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
                        "args": [db, uid, pwd, model, method, args, kwargs or {}]
                    },
                    "id": 2
                }
                r = requests.post(f"{ODOO_URL_FINAL}/jsonrpc", json=payload, timeout=60)
                return r.json().get("result")

        return uid, Models(), "OK"

    except Exception as e:
        return None, None, f"Lỗi kết nối: {e}"

# ================= LOCATION HELPERS =================
def find_required_location_ids(models, uid):
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
        return {'id': locs[0]['id'], 'name': locs[0]['display_name']}

    for k, v in LOCATION_MAP.items():
        r = search(v)
        if r:
            out[k] = r

    return out

def get_transit_quantity(models, uid, product_id, transit_location_id):
    if not transit_location_id:
        return 0
    data = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'stock.quant', 'search_read',
        [[('product_id', '=', product_id),
          ('location_id', '=', transit_location_id)]],
        {'fields': ['quantity']}
    )
    return sum(int(q.get('quantity') or 0) for q in data)

# ================= CHAT REGISTRY =================
REGISTERED_CHAT_IDS = set()
CHAT_IDS_LOCK = threading.Lock()

def register_chat_id(chat_id):
    with CHAT_IDS_LOCK:
        REGISTERED_CHAT_IDS.add(chat_id)

def get_registered_chat_ids():
    with CHAT_IDS_LOCK:
        return list(REGISTERED_CHAT_IDS)

# ================= HANDLERS =================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_chat_id(update.message.chat_id)
    name = update.message.from_user.first_name
    await update.message.reply_text(
        f"Chào {name}!\n"
        "1. Gõ mã SP để tra tồn\n"
        "2. /keohang tạo báo cáo Excel\n"
        "3. /checkpo kiểm tra PO\n"
        "4. /ping kiểm tra kết nối Odoo"
    )

async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_chat_id(update.message.chat_id)
    await update.message.reply_text("Đang kiểm tra kết nối odoo, xin chờ...")
    uid, _, err = connect_odoo()
    if uid:
        await update.message.reply_text(f"✅ Thành công! Kết nối Odoo DB: {ODOO_DB}")
    else:
        await update.message.reply_text(f"❌ Lỗi: {err}")

# ======= GIỮ NGUYÊN LOGIC CŨ =======
# excel_report_command
# checkpo_command
# handle_po_file
# handle_product_code
# watchdog_201
# (Toàn bộ phần này Y HỆT file gốc của mày, không sửa 1 dòng)

# ================= MAIN =================
def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
    except Exception:
        pass

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("keohang", excel_report_command))
    application.add_handler(CommandHandler("checkpo", checkpo_command))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_po_file))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))

    logger.info("Bot started!")
    application.run_polling()

if __name__ == "__main__":
    main()
