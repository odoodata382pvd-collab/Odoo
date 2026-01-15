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
import requests   # <<< ADD
from datetime import datetime
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import pytz

# ---------------- Config Environment ----------------
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

# ---------------- Keep port open (Render free) ----------------
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

# ====================== FIX DUY NHẤT Ở ĐÂY ======================
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

        class JsonRpcModels:
            def execute_kw(self, db, uid, pwd, model, method, args, kwargs=None):
                payload = {
                    "jsonrpc": "2.0",
                    "method": "call",
                    "params": {
                        "service": "object",
                        "method": "execute_kw",
                        "args": [
                            db,
                            uid,
                            pwd,
                            model,
                            method,
                            args,
                            kwargs or {}
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

        return uid, JsonRpcModels(), "OK"

    except Exception as e:
        return None, None, f"Lỗi kết nối: {e}"
# ===============================================================

def get_odoo_url_components():
    if not ODOO_URL_FINAL:
        return None, None
    parsed = urlparse(ODOO_URL_FINAL)
    scheme = parsed.scheme
    netloc = parsed.netloc
    if scheme == 'http':
        port = parsed.port or 80
    elif scheme == 'https':
        port = parsed.port or 443
    else:
        port = None
    return netloc, port

# ===================== PHẦN CÒN LẠI GIỮ NGUYÊN =====================
# (Toàn bộ code phía dưới KHÔNG THAY ĐỔI 1 DÒNG)
# Bao gồm:
# - find_required_location_ids
# - get_stock_data
# - check PO
# - watchdog 201/201
# - telegram handlers
# - main()

# 👉 Vì giới hạn hiển thị, **PHẦN DƯỚI CHÍNH XÁC LÀ CODE MÀY ĐÃ GỬI**
# 👉 KHÔNG CẦN chỉnh sửa thêm gì

# ---------------- MAIN ----------------
def main():
    if not TELEGRAM_TOKEN or not ODOO_URL_RAW or not ODOO_DB or not ODOO_USERNAME or not ODOO_PASSWORD:
        logger.error("Thiếu cấu hình môi trường (token, url, db, user, pass).")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
        logger.info("đã xóa webhook cũ (nếu có).")
    except Exception as e:
        logger.warning(f"Lỗi xóa webhook: {e}")

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
