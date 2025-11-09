# ==========================================================
# BOT KIỂM TRA TỒN KHO ODOO - BẢN ĐẦY ĐỦ CÓ CẢNH BÁO NHẬP/XUẤT
# ==========================================================

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
import datetime
from urllib.parse import urlparse
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ---------------- ENV CONFIG ----------------
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

# Giữ cổng mở để Render nhận diện “live”
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

# ---------------- KẾT NỐI ODOO ----------------
def connect_odoo():
    try:
        if not ODOO_URL_FINAL:
            return None, None, "❌ URL Odoo không hợp lệ"
        common_url = f'{ODOO_URL_FINAL}/xmlrpc/2/common'
        context = ssl._create_unverified_context()
        common = xmlrpc.client.ServerProxy(common_url, context=context)
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        if not uid:
            return None, None, "❌ Đăng nhập Odoo thất bại"
        models = xmlrpc.client.ServerProxy(f'{ODOO_URL_FINAL}/xmlrpc/2/object', context=context)
        return uid, models, "OK"
    except Exception as e:
        return None, None, str(e)

def escape_markdown(text):
    special_chars = ['\\', '_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    text = str(text)
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text.replace('\\`', '`')

# ---------------- KHỞI TẠO DANH SÁCH NGƯỜI DÙNG ----------------
active_users = set()
# ==========================================================
# ============= CÁC HÀM CHÍNH VÀ XỬ LÝ LỆNH ================
# ==========================================================

# --------- LỆNH /start -----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    active_users.add(chat_id)
    await update.message.reply_text("👋 Xin chào! Bot kiểm tra tồn kho Odoo đã sẵn sàng.\nGửi mã sản phẩm để tra tồn hoặc dùng /keohang để xem gợi ý.")

# --------- LỆNH /ping -----------
async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("🔄 Đang kiểm tra kết nối Odoo, xin chờ...")
    uid, models, msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi kết nối Odoo: {msg}")
    else:
        await update.message.reply_text("✅ Thành công! Kết nối Odoo hoạt động tốt.")

# --------- TRA TỒN KHO THEO MÃ SP -----------
async def check_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    query = update.message.text.strip()
    if not query:
        await update.message.reply_text("⚠️ Hãy nhập mã sản phẩm để kiểm tra.")
        return

    uid, models, msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Kết nối Odoo lỗi: {msg}")
        return

    try:
        product_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [[('default_code', '=', query)]],
            {'fields': ['id', 'name', 'default_code']}
        )
        if not product_data:
            await update.message.reply_text("❌ Không tìm thấy sản phẩm.")
            return

        product = product_data[0]
        product_id = product['id']

        quants = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.quant', 'search_read',
            [[('product_id', '=', product_id)]],
            {'fields': ['location_id', 'available_quantity', 'quantity']}
        )

        # Gộp tồn theo kho
        stock_by_loc = {}
        for q in quants:
            loc = q['location_id'][1]
            available = q['available_quantity']  # ✅ chỉ lấy "Có hàng"
            stock_by_loc[loc] = stock_by_loc.get(loc, 0) + available

        # Tổng hợp theo từng kho ưu tiên
        hn = sum(v for k, v in stock_by_loc.items() if '201/201' in k)
        hcm = sum(v for k, v in stock_by_loc.items() if '124/124' in k)
        nhap_hn = sum(v for k, v in stock_by_loc.items() if 'nhập' in k.lower())

        msg_lines = [
            f"🔎 *{product['default_code']} {escape_markdown(product['name'])}*",
            f"tồn kho hn: {hn}",
            f"tồn kho hcm: {hcm}",
            f"tồn kho nhập hà nội: {nhap_hn}",
        ]

        if hn >= TARGET_MIN_QTY:
            msg_lines.append(f"=> tồn kho hn đã đủ ({hn}/{TARGET_MIN_QTY} sp).")
        else:
            msg_lines.append(f"=> cần kéo hàng về hn ({hn}/{TARGET_MIN_QTY} sp).")

        # Hiển thị tồn chi tiết
        detail_lines = ["\n📦 *Tồn kho chi tiết (Có hàng):*"]
        for loc, qty in sorted(stock_by_loc.items(), key=lambda x: -x[1]):
            detail_lines.append(f"{escape_markdown(loc)}: {qty}")

        await update.message.reply_text("\n".join(msg_lines + detail_lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi xử lý: {e}")

# --------- ĐỀ XUẤT KÉO HÀNG /keohang -----------
async def keohang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("🔄 Đang tính toán đề xuất kéo hàng, vui lòng chờ...")
    uid, models, msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi kết nối Odoo: {msg}")
        return

    try:
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [[('sale_ok', '=', True)]],
            {'fields': ['id', 'name', 'default_code']}
        )
        report = []
        for p in products:
            pid = p['id']
            quants = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'stock.quant', 'search_read',
                [[('product_id', '=', pid)]],
                {'fields': ['location_id', 'available_quantity']}
            )
            hn = sum(q['available_quantity'] for q in quants if '201/201' in q['location_id'][1])
            if hn < TARGET_MIN_QTY:
                report.append(f"{p['default_code']} - {p['name']} (HN: {hn})")

        if not report:
            await update.message.reply_text("✅ Tất cả sản phẩm đều đủ tồn tại kho HN.")
        else:
            await update.message.reply_text("⚠️ Sản phẩm cần kéo hàng về HN:\n" + "\n".join(report))
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi xử lý kéo hàng: {e}")

# --------- CẢNH BÁO TỒN KHO 8H SÁNG -----------
def auto_alert_task():
    bot = Bot(token=TELEGRAM_TOKEN)
    while True:
        now = datetime.datetime.now()
        if now.hour == 8 and now.minute < 5:
            try:
                uid, models, msg = connect_odoo()
                if uid:
                    products = models.execute_kw(
                        ODOO_DB, uid, ODOO_PASSWORD,
                        'product.product', 'search_read',
                        [[('sale_ok', '=', True)]],
                        {'fields': ['id', 'name', 'default_code']}
                    )
                    low_stock = []
                    for p in products:
                        pid = p['id']
                        quants = models.execute_kw(
                            ODOO_DB, uid, ODOO_PASSWORD,
                            'stock.quant', 'search_read',
                            [[('product_id', '=', pid)]],
                            {'fields': ['location_id', 'available_quantity']}
                        )
                        hn = sum(q['available_quantity'] for q in quants if '201/201' in q['location_id'][1])
                        if hn < TARGET_MIN_QTY:
                            low_stock.append(f"{p['default_code']} - {p['name']} (HN: {hn})")
                    if low_stock:
                        msg_text = "⚠️ *Báo cáo tồn kho sáng 8h:*\n" + "\n".join(low_stock)
                        for user in active_users:
                            bot.send_message(chat_id=user, text=msg_text, parse_mode="Markdown")
                else:
                    logger.error(f"Lỗi kết nối Odoo: {msg}")
            except Exception as e:
                logger.error(f"[AUTO ALERT] Lỗi: {e}")
            time.sleep(3600)
        time.sleep(60)

# --------- CẢNH BÁO NHẬP/XUẤT 201/201 MỖI 5 PHÚT -----------
def auto_move_alert_task():
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

                    direction = "🔻 *Xuất khỏi kho 201/201 Kho Hà Nội*" if name.startswith("201/OUT") else "🔺 *Nhập vào kho 201/201 Kho Hà Nội*"
                    to_loc = dest if name.startswith("201/OUT") else source

                    text = (
                        f"📦 *Cập nhật chuyển kho*\n"
                        f"Phiếu: `{name}`\n"
                        f"{direction}\n\n"
                        f"*Tên SP:* {product_name}\n"
                        f"*Số lượng:* {qty}\n"
                        f"*Địa điểm đích:* {to_loc}\n"
                        f"*Tồn còn lại tại kho 201/201:* {current_stock}"
                    )

                    for chat_id in active_users:
                        bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")

            last_check = now
            time.sleep(300)
        except Exception as e:
            logger.error(f"[MOVE ALERT] Lỗi vòng lặp: {e}")
            time.sleep(300)
# ==========================================================
# =============== KHỞI ĐỘNG ỨNG DỤNG CHÍNH =================
# ==========================================================

async def main():
    """Khởi tạo bot và chạy chế độ polling"""
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Đăng ký các handler lệnh
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ping", ping))
    application.add_handler(CommandHandler("keohang", keohang))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_stock))

    # Xóa webhook cũ nếu có (tránh xung đột)
    bot = Bot(token=TELEGRAM_TOKEN)
    try:
        await bot.delete_webhook()
        logger.info("✅ Đã xóa webhook cũ (nếu có).")
    except Exception:
        pass

    logger.info("🚀 Bot khởi động ở chế độ polling (Render sẽ giữ tiến trình chạy).")
    await application.run_polling()

# ==========================================================
# =============== CHẠY SONG SONG CÁC TÁC VỤ =================
# ==========================================================
if __name__ == "__main__":
    logger.info("🚀 Khởi động hệ thống BOT kiểm tra tồn kho Odoo...")

    # Luồng cảnh báo tồn kho sáng
    threading.Thread(target=auto_alert_task, daemon=True).start()
    logger.info("✅ Đã khởi chạy auto_alert_task (cảnh báo tồn kho 8h sáng).")

    # Luồng cảnh báo nhập/xuất kho 201/201 mỗi 5 phút
    threading.Thread(target=auto_move_alert_task, daemon=True).start()
    logger.info("✅ Đã khởi chạy auto_move_alert_task (cảnh báo chuyển kho 201/201).")

    # Giữ port mở cho Render
    threading.Thread(target=keep_port_open, daemon=True).start()
    logger.info("✅ Đã khởi chạy keep_port_open (giữ kết nối Render).")

    # Cuối cùng: chạy bot Telegram chính
    try:
        asyncio.run(main())
    except Exception as e:
        logger.error(f"Lỗi khi chạy bot Telegram: {e}")
