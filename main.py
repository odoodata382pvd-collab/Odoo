# main.py - Bản mở rộng có thêm cảnh báo nhập/xuất kho 201/201 mỗi 5 phút
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
from urllib.parse import urlparse
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ---------------- Config & Env ----------------
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
ODOO_URL_RAW = os.environ.get('ODOO_URL').rstrip('/') if os.environ.get('ODOO_URL') else None
if ODOO_URL_RAW and ODOO_URL_RAW.lower().endswith('/odoo'):
    ODOO_URL_FINAL = ODOO_URL_RAW[:-len('/odoo')]
else:
    ODOO_URL_FINAL = ODOO_URL_RAW

ODOO_DB = os.environ.get('ODOO_DB')
ODOO_USERNAME = os.environ.get('ODOO_USERNAME')
ODOO_PASSWORD = os.environ.get('ODOO_PASSWORD')
USER_ID_TO_SEND_REPORT = os.environ.get('USER_ID_TO_SEND_REPORT')

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

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

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
        common_url = f'{ODOO_URL_FINAL}/xmlrpc/2/common'
        context = ssl._create_unverified_context()
        common = xmlrpc.client.ServerProxy(common_url, context=context)
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        if not uid:
            return None, None, "Đăng nhập thất bại. kiểm tra lại user/pass/db."
        models = xmlrpc.client.ServerProxy(f'{ODOO_URL_FINAL}/xmlrpc/2/object', context=context)
        return uid, models, "OK"
    except Exception as e:
        return None, None, str(e)

def find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD):
    location_ids = {}
    def search_location(name_code):
        loc_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read',
            [[('display_name', 'ilike', name_code)]],
            {'fields': ['id', 'display_name', 'complete_name']}
        )
        if not loc_data:
            return None
        preferred_loc = next((l for l in loc_data if name_code.lower() in l['display_name'].lower()), loc_data[0])
        return {'id': preferred_loc['id'], 'name': preferred_loc['display_name']}
    hn_stock = search_location(LOCATION_MAP['HN_STOCK_CODE'])
    if hn_stock: location_ids['HN_STOCK'] = hn_stock
    hcm_stock = search_location(LOCATION_MAP['HCM_STOCK_CODE'])
    if hcm_stock: location_ids['HCM_STOCK'] = hcm_stock
    hn_transit = search_location(LOCATION_MAP['HN_TRANSIT_NAME'])
    if hn_transit: location_ids['HN_TRANSIT'] = hn_transit
    return location_ids

def escape_markdown(text):
    special_chars = ['\\', '_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    text = str(text)
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text.replace('\\`', '`')
# ================== AUTO MOVE ALERT - START ==================
import datetime

def auto_move_alert_task():
    """Theo dõi phiếu chuyển hàng có mã 201/IN hoặc 201/OUT liên quan kho Hà Nội"""
    logger.info("🔁 Bắt đầu theo dõi phiếu chuyển kho 201/201 Kho Hà Nội (5 phút/lần)")
    last_check = datetime.datetime.utcnow() - datetime.timedelta(minutes=5)
    bot = Bot(token=TELEGRAM_TOKEN)
    notified = set()

    while True:
        try:
            uid, models, msg = connect_odoo()
            if not uid:
                logger.error(f"[MOVE ALERT] Không kết nối được Odoo: {msg}")
                time.sleep(300)
                continue

            now = datetime.datetime.utcnow()
            domain = [
                ("state", "=", "done"),
                ("write_date", ">=", (last_check - datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")),
            ]
            pickings = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "stock.picking", "search_read",
                [domain],
                {"fields": ["name", "location_id", "location_dest_id", "move_ids_without_package", "write_date"]}
            )

            for picking in pickings:
                name = picking.get("name", "")
                if not name.startswith("201/OUT") and not name.startswith("201/IN"):
                    continue

                if name in notified:
                    continue
                notified.add(name)

                source = picking.get("location_id", ["", ""])[1]
                dest = picking.get("location_dest_id", ["", ""])[1]
                moves = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    "stock.move", "read",
                    [picking["move_ids_without_package"]],
                    {"fields": ["product_id", "product_uom_qty"]}
                )

                for mv in moves:
                    product_name = mv.get("product_id", ["", ""])[1]
                    product_id = mv.get("product_id", ["", ""])[0]
                    qty = mv.get("product_uom_qty", 0)

                    # 🔹 Lấy tồn kho "Có hàng" của sản phẩm tại kho 201/201
                    try:
                        stock_data = models.execute_kw(
                            ODOO_DB, uid, ODOO_PASSWORD,
                            "stock.quant", "search_read",
                            [[
                                ("product_id", "=", product_id),
                                ("location_id.complete_name", "ilike", "201/201")
                            ]],
                            {"fields": ["available_quantity"]}
                        )
                        current_stock = sum(q["available_quantity"] for q in stock_data)
                    except Exception as e:
                        logger.error(f"[MOVE ALERT] Không lấy được tồn kho hiện tại: {e}")
                        current_stock = 0

                    if name.startswith("201/OUT"):
                        direction = f"🔻 *Xuất khỏi kho 201/201 Kho Hà Nội*"
                        to_loc = dest
                    else:
                        direction = f"🔺 *Nhập vào kho 201/201 Kho Hà Nội*"
                        to_loc = source

                    # ✅ Nội dung tin nhắn đầy đủ
                    text = (
                        f"📦 *Cập nhật chuyển kho*\n"
                        f"Phiếu: `{name}`\n"
                        f"{direction}\n\n"
                        f"*Tên SP:* {product_name}\n"
                        f"*Số lượng:* {qty}\n"
                        f"*Địa điểm đích:* {to_loc}\n"
                        f"*Tồn còn lại tại kho 201/201:* {current_stock}"
                    )

                    try:
                        for chat_id in active_users:
                            bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
                    except Exception as e:
                        logger.error(f"[MOVE ALERT] Gửi cảnh báo lỗi: {e}")

            last_check = now
            time.sleep(300)  # 5 phút

        except Exception as e:
            logger.error(f"[MOVE ALERT] Lỗi vòng lặp: {e}")
            time.sleep(300)
# ================== AUTO MOVE ALERT - END ==================
# ================== AUTO MOVE ALERT - PART 3 ==================
# Bắt đầu luồng theo dõi nhập/xuất kho 201/201 mỗi 5 phút
threading.Thread(target=auto_move_alert_task, daemon=True).start()
# ==============================================================
