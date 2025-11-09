import os
import io
import time
import ssl
import socket
import asyncio
import logging
import threading
import datetime
import xmlrpc.client
import pandas as pd
from telegram import Bot, Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ================================
# CẤU HÌNH & MÔI TRƯỜNG
# ================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s]: %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USERNAME = os.getenv("ODOO_USERNAME")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
USER_ID_TO_SEND_REPORT = int(os.getenv("USER_ID_TO_SEND_REPORT", "0"))
TARGET_MIN_QTY = 50  # ngưỡng tối thiểu tồn kho HN


# ================================
# KẾT NỐI ODOO
# ================================
def connect_odoo():
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        if not uid:
            return None, None, "Xác thực Odoo thất bại"
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)
        return uid, models, "Kết nối Odoo thành công"
    except Exception as e:
        logger.error(f"Lỗi khi kết nối Odoo: {e}")
        return None, None, str(e)


# ================================
# HÀM HỖ TRỢ XỬ LÝ DỮ LIỆU
# ================================
def get_stock_by_product(product_code):
    uid, models, msg = connect_odoo()
    if not uid:
        return None, f"❌ Không thể kết nối Odoo: {msg}"

    try:
        product_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [[['default_code', '=', product_code]]],
            {'fields': ['id', 'name']}
        )

        if not product_ids:
            return None, f"❌ Không tìm thấy mã sản phẩm: {product_code}"

        product_id = product_ids[0]['id']
        product_name = product_ids[0]['name']

        # Truy vấn stock.quant theo cột "Có hàng" (available_quantity)
        quant_domain_all = [['product_id', '=', product_id]]
        quant_data_all = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
            [quant_domain_all],
            {'fields': ['location_id', 'available_quantity']}
        )

        # Gom nhóm tồn kho chi tiết theo vị trí
        stock_by_loc_id = {}
        for q in quant_data_all:
            loc = q['location_id'][1] if q['location_id'] else 'Chưa rõ vị trí'
            qty = float(q.get('available_quantity', 0.0))
            if loc not in stock_by_loc_id:
                stock_by_loc_id[loc] = 0.0
            stock_by_loc_id[loc] += qty

        # Phân loại tồn kho tổng theo tên vị trí
        ton_hn = sum(q for k, q in stock_by_loc_id.items() if "hn" in k.lower() or "hà nội" in k.lower())
        ton_hcm = sum(q for k, q in stock_by_loc_id.items() if "hcm" in k.lower() or "hồ chí minh" in k.lower())
        ton_nhap_hn = sum(q for k, q in stock_by_loc_id.items() if "nhập hà nội" in k.lower())

        msg_lines = [
            f"{product_code} {product_name}",
            f"tồn kho hn: {int(ton_hn)}",
            f"tồn kho hcm: {int(ton_hcm)}",
            f"tồn kho nhập hà nội: {int(ton_nhap_hn)}"
        ]

        if ton_hn >= TARGET_MIN_QTY:
            msg_lines.append(f"=> tồn kho hn đã đủ ({int(ton_hn)}/{TARGET_MIN_QTY} sp).")
        else:
            msg_lines.append(f"=> ⚠️ tồn kho hn thiếu ({int(ton_hn)}/{TARGET_MIN_QTY} sp).")

        msg_lines.append("\nTồn kho chi tiết (có hàng):")
        for k, v in stock_by_loc_id.items():
            msg_lines.append(f"{k}: {int(v)}")

        return "\n".join(msg_lines), None

    except Exception as e:
        logger.error(f"Lỗi khi lấy tồn kho sản phẩm {product_code}: {e}")
        return None, str(e)


def get_stock_data():
    uid, models, msg = connect_odoo()
    if not uid:
        return None, 0, msg

    try:
        product_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read',
            [[]],
            {'fields': ['id', 'default_code', 'name']}
        )

        results = []
        for p in product_data:
            quant_domain = [['product_id', '=', p['id']]]
            quant_data = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
                [quant_domain],
                {'fields': ['location_id', 'available_quantity']}
            )

            stock_by_loc_id = {}
            for q in quant_data:
                loc = q['location_id'][1] if q['location_id'] else 'Chưa rõ vị trí'
                qty = float(q.get('available_quantity', 0.0))
                if loc not in stock_by_loc_id:
                    stock_by_loc_id[loc] = 0.0
                stock_by_loc_id[loc] += qty

            ton_hn = sum(q for k, q in stock_by_loc_id.items() if "hn" in k.lower() or "hà nội" in k.lower())

            if ton_hn < TARGET_MIN_QTY:
                results.append({
                    "Mã SP": p['default_code'],
                    "Tên SP": p['name'],
                    "Tồn HN": ton_hn
                })

        if not results:
            return None, 0, "Tất cả sản phẩm đều đủ tồn kho."

        df = pd.DataFrame(results)
        output = io.BytesIO()
        df.to_excel(output, index=False)
        output.seek(0)
        return output, len(results), "OK"

    except Exception as e:
        logger.error(f"Lỗi khi lấy dữ liệu tổng hợp: {e}")
        return None, 0, str(e)


# ================================
# XỬ LÝ TIN NHẮN TỪ TELEGRAM
# ================================
async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()
    logger.info(f"Tra mã sản phẩm: {code}")
    msg, err = get_stock_by_product(code)
    if err:
        await update.message.reply_text(f"❌ {err}")
    else:
        await update.message.reply_text(msg)


# ================================
# CẢNH BÁO TỰ ĐỘNG LÚC 8H SÁNG
# ================================
AUTO_ALERT_ENABLED = True

def auto_alert_task():
    if not AUTO_ALERT_ENABLED:
        return
    bot = Bot(token=TELEGRAM_TOKEN)

    while True:
        try:
            now = datetime.datetime.now()
            next_run = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if now >= next_run:
                next_run += datetime.timedelta(days=1)
            time.sleep((next_run - now).total_seconds())

            excel_buffer, count, msg = get_stock_data()
            if excel_buffer and count > 0:
                bot.send_document(
                    chat_id=USER_ID_TO_SEND_REPORT,
                    document=excel_buffer,
                    filename="bao_cao_ton_thap.xlsx",
                    caption=f"⚠️ Cảnh báo tồn kho thấp: {count} sản phẩm dưới {TARGET_MIN_QTY}"
                )
            else:
                bot.send_message(
                    chat_id=USER_ID_TO_SEND_REPORT,
                    text=f"✅ Tất cả sản phẩm đều đủ tồn kho tại HN ({datetime.datetime.now().strftime('%H:%M')})."
                )

        except Exception as e:
            logger.error(f"Lỗi trong auto_alert_task: {e}")
            time.sleep(60)


# ================================
# CÁC LỆNH TELEGRAM
# ================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["📦 Tra tồn kho", "📊 Báo cáo kéo hàng"],
        ["🔔 Kiểm tra tồn kho tự động", "🧭 Kiểm tra kết nối Odoo"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    welcome = (
        "👋 Chào mừng bạn đến với *Odoo Stock Bot!*\n\n"
        "Chọn chức năng hoặc gõ trực tiếp mã SP (VD: I-78) để tra tồn kho."
    )
    await update.message.reply_text(welcome, parse_mode="Markdown", reply_markup=reply_markup)


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Đang kiểm tra kết nối Odoo...")
    uid, _, msg = connect_odoo()
    if uid:
        await update.message.reply_text(f"✅ Kết nối thành công với DB {ODOO_DB}\nUser: {ODOO_USERNAME}")
    else:
        await update.message.reply_text(f"❌ Lỗi kết nối: {msg}")


async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Đang tạo báo cáo kéo hàng...")
    excel_buffer, count, msg = get_stock_data()
    if not excel_buffer:
        await update.message.reply_text(f"❌ Lỗi: {msg}")
        return
    if count > 0:
        await update.message.reply_document(
            document=excel_buffer,
            filename="de_xuat_keo_hang.xlsx",
            caption=f"✅ Đã tạo báo cáo ({count} sản phẩm cần bổ sung)"
        )
    else:
        await update.message.reply_text("✅ Tất cả sản phẩm đều đủ tồn kho tại HN.")


# ================================
# MAIN ENTRYPOINT
# ================================
def main():
    if not all([TELEGRAM_TOKEN, ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD]):
        logger.error("⚠️ Thiếu biến môi trường cấu hình.")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
        logger.info("✅ Đã xóa webhook cũ (nếu có).")
    except Exception as e:
        logger.warning(f"⚠️ Không thể xóa webhook: {e}")

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("keohang", excel_report_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))

    threading.Thread(target=auto_alert_task, daemon=True).start()
    logger.info("🚀 Bot khởi động ở chế độ polling.")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
