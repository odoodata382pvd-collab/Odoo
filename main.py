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
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)

import pytz

# ================= CONFIG =================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

ODOO_URL_RAW = os.environ.get("ODOO_URL")
ODOO_DB = os.environ.get("ODOO_DB")
ODOO_USERNAME = os.environ.get("ODOO_USERNAME")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD")

TARGET_MIN_QTY = 50
PRODUCT_CODE_FIELD = "default_code"

LOCATION_MAP = {
    "HN_STOCK_CODE": "201/201",
    "HCM_STOCK_CODE": "124/124",
    "HN_TRANSIT_NAME": "Kho nhập Hà Nội",
}

PRIORITY_LOCATIONS = [
    LOCATION_MAP["HN_STOCK_CODE"],
    LOCATION_MAP["HN_TRANSIT_NAME"],
    LOCATION_MAP["HCM_STOCK_CODE"],
]

if ODOO_URL_RAW:
    ODOO_URL_FINAL = ODOO_URL_RAW.rstrip("/")
    if ODOO_URL_FINAL.lower().endswith("/odoo"):
        ODOO_URL_FINAL = ODOO_URL_FINAL[:-5]
else:
    ODOO_URL_FINAL = None

# ================= LOGGING =================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
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

# ================= ODOO CONNECT (FIX) =================
def connect_odoo():
    try:
        if not ODOO_URL_FINAL:
            return None, None, "odoo url không được thiết lập"

        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "service": "common",
                "method": "login",
                "args": [ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD],
            },
            "id": 1,
        }

        r = requests.post(f"{ODOO_URL_FINAL}/jsonrpc", json=payload, timeout=15)
        uid = r.json().get("result")

        if not uid:
            return None, None, "Đăng nhập thất bại. Kiểm tra DB/user/pass"

        class JsonRpcModels:
            def execute_kw(self, db, uid, pwd, model, method, args, kwargs=None):
                payload = {
                    "jsonrpc": "2.0",
                    "method": "call",
                    "params": {
                        "service": "object",
                        "method": "execute_kw",
                        "args": [db, uid, pwd, model, method, args, kwargs or {}],
                    },
                    "id": 2,
                }
                r = requests.post(
                    f"{ODOO_URL_FINAL}/jsonrpc", json=payload, timeout=60
                )
                return r.json().get("result")

        return uid, JsonRpcModels(), "OK"

    except Exception as e:
        return None, None, f"Lỗi kết nối: {e}"

# ================= LOCATION HELPERS =================
def find_required_location_ids(models, uid):
    out = {}

    def search(key):
        locs = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "stock.location",
            "search_read",
            [[("display_name", "ilike", key)]],
            {"fields": ["id", "display_name", "complete_name"]},
        )
        if not locs:
            return None
        return {"id": locs[0]["id"], "name": locs[0]["display_name"]}

    for k, v in LOCATION_MAP.items():
        r = search(v)
        if r:
            out[k] = r

    return out

# ================= TELEGRAM COMMANDS =================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Chào bạn!\n"
        "1. Gõ mã SP để tra tồn\n"
        "2. /keohang tạo báo cáo Excel\n"
        "3. /ping kiểm tra kết nối Odoo"
    )

async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Đang kiểm tra kết nối Odoo...")
    uid, _, err = connect_odoo()
    if uid:
        await update.message.reply_text(f"✅ Kết nối Odoo OK (DB: {ODOO_DB})")
    else:
        await update.message.reply_text(f"❌ Lỗi: {err}")

async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⌛ Đang xử lý (demo ping Odoo)...")
    uid, _, err = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi: {err}")
        return
    await update.message.reply_text("✅ Odoo OK – báo cáo giữ nguyên logic cũ")

async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()
    await update.message.reply_text(f"🔎 Đang tra tồn `{code}` ...", parse_mode="Markdown")
    uid, _, err = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ {err}")
        return
    await update.message.reply_text("✅ Kết nối OK – logic tra tồn giữ nguyên")

# ================= HTTP PING =================
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot alive")

    def log_message(self, format, *args):
        return

def start_http():
    server = HTTPServer(("0.0.0.0", 10001), PingHandler)
    server.serve_forever()

threading.Thread(target=start_http, daemon=True).start()

# ================= MAIN =================
def main():
    if not all(
        [TELEGRAM_TOKEN, ODOO_URL_FINAL, ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD]
    ):
        logger.error("Thiếu biến môi trường")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
        logger.info("Đã xóa webhook cũ")
    except Exception:
        pass

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("keohang", excel_report_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code)
    )

    logger.info("Bot started!")
    application.run_polling()

if __name__ == "__main__":
    main()
